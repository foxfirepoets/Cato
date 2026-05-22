"""
cato/tools/web_search.py — Web-Search-Plus multi-engine search tool.

Provides async search across multiple engines with:
- Query classification (code / news / academic / general)
- Engine-specific fallback chains
- Heuristic confidence scoring + cross-engine agreement boosting
- Result caching via core/memory.py
- Rate-limit handling (429 → 60s cooldown per provider)
- All API keys loaded from Vault — never hardcoded

Supported engines:
    DDG Instant Answer API  — no key needed
    Brave Search            — vault key: brave_api_key
    Exa                     — vault key: exa_api_key
    Tavily                  — vault key: tavily_api_key
    arXiv Atom XML          — no key needed
    Semantic Scholar        — optional vault key: semantic_scholar_api_key
    PubMed / NCBI Entrez    — no key needed (3 req/sec limit enforced)
    SearXNG (self-hosted)   — config key: searxng_url
    Perplexity              — vault key: perplexity_api_key (--depth deep only)
"""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import quote_plus

from cato.integrations.http_client import _assert_safe_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source_engine: str
    confidence: float
    rank: int
    published_date: str = ""


QueryType = Literal["code", "news", "academic", "general"]
DepthType = Literal["normal", "deep"]

# ---------------------------------------------------------------------------
# Query classifier
# ---------------------------------------------------------------------------

_CODE_KEYWORDS = frozenset({
    "github", "stackoverflow", "docs", "api", "function",
    "library", "error", "bug", "python", "javascript", "typescript",
    "npm", "pip", "import", "class", "def ", "async", "await",
})
_ACADEMIC_KEYWORDS = frozenset({
    "arxiv", "paper", "doi", "cite", "journal", "study", "et al",
    "abstract", "research", "thesis", "publication", "review",
})
_NEWS_KEYWORDS = frozenset({
    "today", "latest", "breaking", "announced", "release", "launches",
    "just in", "new report", "update",
})


def classify_query(query: str) -> QueryType:
    """
    Return the most appropriate search category for *query*.

    Priority: code > academic > news > general
    """
    q_lower = query.lower()
    for kw in _CODE_KEYWORDS:
        if kw in q_lower:
            return "code"
    for kw in _ACADEMIC_KEYWORDS:
        if kw in q_lower:
            return "academic"
    for kw in _NEWS_KEYWORDS:
        if kw in q_lower:
            return "news"
    return "general"


# ---------------------------------------------------------------------------
# Fallback chains per query type
# ---------------------------------------------------------------------------

_FALLBACK_CHAINS: dict[QueryType, list[str]] = {
    "code":     ["exa", "brave", "searxng", "ddg_api"],
    "news":     ["tavily", "brave", "searxng", "ddg_api"],
    "academic": ["semantic_scholar", "arxiv", "exa"],
    "general":  ["brave", "searxng", "ddg_api"],
}


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool:
    """
    Multi-engine web search with fallback chains, confidence scoring, and caching.

    Usage::

        tool = WebSearchTool(vault=vault)
        results = await tool.search("Python asyncio tutorial", query_type="code")
    """

    def __init__(
        self,
        vault: Any = None,
        searxng_url: str = "",
        cache_ttl_seconds: int = 3600,
        memory: Any = None,
    ) -> None:
        self._vault = vault
        self._searxng_url = searxng_url
        self._cache_ttl = cache_ttl_seconds
        self._memory = memory

        # Per-provider rate-limit cooldown: provider_name -> cooldown_until (epoch)
        self._rate_limited_until: dict[str, float] = {}

        # PubMed rate limiter: 3 req/sec
        self._pubmed_semaphore = asyncio.Semaphore(3)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def search(
        self,
        query: str,
        query_type: Optional[QueryType] = None,
        depth: DepthType = "normal",
        max_results: int = 10,
    ) -> list[SearchResult]:
        """
        Search using the best engine chain for the query type.

        Returns results sorted by confidence descending.
        """
        qt: QueryType = query_type or classify_query(query)
        chain = list(_FALLBACK_CHAINS.get(qt, _FALLBACK_CHAINS["general"]))

        # Add perplexity at the front for deep searches
        if depth == "deep":
            chain = ["perplexity"] + chain

        all_results: list[SearchResult] = []

        for provider in chain:
            if self._is_rate_limited(provider):
                logger.debug("Skipping rate-limited provider: %s", provider)
                continue
            try:
                results = await self._search_provider(provider, query)
                all_results.extend(results)
                if results:
                    break  # got results — stop chain
            except Exception as exc:
                logger.warning("Provider %s failed: %s", provider, exc)
                continue

        # If primary chain yielded nothing, try ddg_api as last resort
        if not all_results:
            try:
                all_results = await self._search_ddg_api(query)
            except Exception:
                pass

        # Cross-engine agreement boost
        boosted = self._cross_engine_agreement(all_results)

        # Sort by confidence descending, slice to max_results
        boosted.sort(key=lambda r: r.confidence, reverse=True)
        return boosted[:max_results]

    # ------------------------------------------------------------------ #
    # Provider dispatch                                                   #
    # ------------------------------------------------------------------ #

    async def _search_provider(self, provider: str, query: str) -> list[SearchResult]:
        """Dispatch to the right backend.  Marks provider rate-limited on 429."""
        dispatch = {
            "ddg_api":          self._search_ddg_api,
            "brave":            self._search_brave,
            "exa":              self._search_exa,
            "tavily":           self._search_tavily,
            "arxiv":            self._search_arxiv,
            "semantic_scholar": self._search_semantic_scholar,
            "pubmed":           self._search_pubmed,
            "searxng":          self._search_searxng_default,
            "perplexity":       self._search_perplexity,
        }
        fn = dispatch.get(provider)
        if fn is None:
            return []
        try:
            return await fn(query)
        except _RateLimitError:
            self._mark_rate_limited(provider)
            return []

    # ------------------------------------------------------------------ #
    # Rate-limit helpers                                                  #
    # ------------------------------------------------------------------ #

    def _is_rate_limited(self, provider: str) -> bool:
        until = self._rate_limited_until.get(provider, 0.0)
        return time.time() < until

    def _mark_rate_limited(self, provider: str, cooldown: float = 60.0) -> None:
        self._rate_limited_until[provider] = time.time() + cooldown
        logger.warning("Provider %s rate-limited for %.0fs", provider, cooldown)

    # ------------------------------------------------------------------ #
    # Vault helper                                                        #
    # ------------------------------------------------------------------ #

    def _vault_get(self, key: str) -> Optional[str]:
        if self._vault is None:
            return None
        try:
            return self._vault.get(key)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Search backends                                                     #
    # ------------------------------------------------------------------ #

    async def _search_ddg_api(self, query: str) -> list[SearchResult]:
        """DuckDuckGo Instant Answer JSON API — no key needed."""
        try:
            import aiohttp
        except ImportError:
            return []

        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("ddg_api")
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)

        results: list[SearchResult] = []
        # AbstractText
        if data.get("AbstractText") and data.get("AbstractURL"):
            results.append(SearchResult(
                title=data.get("Heading", query),
                url=data["AbstractURL"],
                snippet=data["AbstractText"][:300],
                source_engine="ddg_api",
                confidence=self._heuristic_confidence(query, data["AbstractURL"], 0, data["AbstractText"]),
                rank=0,
                published_date="",
            ))
        # RelatedTopics
        for i, topic in enumerate(data.get("RelatedTopics", [])[:9]):
            if not isinstance(topic, dict):
                continue
            text = topic.get("Text", "")
            first_url = topic.get("FirstURL", "")
            if text and first_url:
                results.append(SearchResult(
                    title=text[:80],
                    url=first_url,
                    snippet=text[:300],
                    source_engine="ddg_api",
                    confidence=self._heuristic_confidence(query, first_url, i + 1, text),
                    rank=i + 1,
                    published_date="",
                ))
        return results

    async def _search_brave(self, query: str) -> list[SearchResult]:
        """Brave Search API — vault key: brave_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("brave_api_key")
        if not api_key:
            logger.debug("No brave_api_key in vault — skipping Brave")
            return []

        url = f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count=10"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("brave")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("web", {}).get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", "")[:300],
                source_engine="brave",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("description", "")
                ),
                rank=i,
                published_date=item.get("page_age", ""),
            ))
        return results

    async def _search_exa(self, query: str) -> list[SearchResult]:
        """Exa AI search — vault key: exa_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("exa_api_key")
        if not api_key:
            logger.debug("No exa_api_key in vault — skipping Exa")
            return []

        url = "https://api.exa.ai/search"
        payload = {"query": query, "numResults": 10, "useAutoprompt": True}
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 429:
                    raise _RateLimitError("exa")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=(item.get("text") or item.get("highlights", [""])[0])[:300],
                source_engine="exa",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("text", "")
                ),
                rank=i,
                published_date=item.get("publishedDate", ""),
            ))
        return results

    async def _search_tavily(self, query: str) -> list[SearchResult]:
        """Tavily Search API — vault key: tavily_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("tavily_api_key")
        if not api_key:
            logger.debug("No tavily_api_key in vault — skipping Tavily")
            return []

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 10,
            "include_answer": False,
        }
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("tavily")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", "")[:300],
                source_engine="tavily",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("content", "")
                ),
                rank=i,
                published_date=item.get("published_date", ""),
            ))
        return results

    async def _search_arxiv(self, query: str) -> list[SearchResult]:
        """arXiv Atom XML API — no key needed."""
        try:
            import aiohttp
        except ImportError:
            return []

        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=all:{quote_plus(query)}&start=0&max_results=10"
        )
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("arxiv")
                if resp.status != 200:
                    return []
                text = await resp.text()

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        results: list[SearchResult] = []
        for i, entry in enumerate(entries[:10]):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            id_el = entry.find("atom:id", ns)
            published_el = entry.find("atom:published", ns)

            title = (title_el.text or "").strip() if title_el is not None else ""
            snippet = (summary_el.text or "").strip()[:300] if summary_el is not None else ""
            url_val = (id_el.text or "").strip() if id_el is not None else ""
            pub = (published_el.text or "").strip() if published_el is not None else ""

            results.append(SearchResult(
                title=title,
                url=url_val,
                snippet=snippet,
                source_engine="arxiv",
                confidence=self._heuristic_confidence(query, url_val, i, snippet),
                rank=i,
                published_date=pub,
            ))
        return results

    async def _search_semantic_scholar(self, query: str) -> list[SearchResult]:
        """Semantic Scholar API — optional vault key: semantic_scholar_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("semantic_scholar_api_key")
        headers: dict[str, str] = {}
        if api_key:
            headers["x-api-key"] = api_key

        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={quote_plus(query)}&limit=10&fields=title,abstract,externalIds,year,url"
        )
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 429:
                    raise _RateLimitError("semantic_scholar")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, paper in enumerate(data.get("data", [])[:10]):
            paper_url = paper.get("url", "")
            if not paper_url:
                ids = paper.get("externalIds", {}) or {}
                doi = ids.get("DOI", "")
                if doi:
                    paper_url = f"https://doi.org/{doi}"
            results.append(SearchResult(
                title=paper.get("title", ""),
                url=paper_url,
                snippet=(paper.get("abstract") or "")[:300],
                source_engine="semantic_scholar",
                confidence=self._heuristic_confidence(
                    query, paper_url, i, paper.get("abstract", "") or ""
                ),
                rank=i,
                published_date=str(paper.get("year", "")),
            ))
        return results

    async def _search_pubmed(self, query: str) -> list[SearchResult]:
        """NCBI PubMed Entrez API — no key needed; 3 req/sec rate limit enforced."""
        try:
            import aiohttp
        except ImportError:
            return []

        # Step 1: esearch to get PMIDs
        async with self._pubmed_semaphore:
            esearch_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                f"?db=pubmed&term={quote_plus(query)}&retmax=10&retmode=json&usehistory=y"
            )
            _assert_safe_url(esearch_url)  # raises ValueError on private/metadata IPs
            async with aiohttp.ClientSession() as session:
                async with session.get(esearch_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        raise _RateLimitError("pubmed")
                    if resp.status != 200:
                        return []
                    esearch_data = await resp.json()

            pmids: list[str] = esearch_data.get("esearchresult", {}).get("idlist", [])
            if not pmids:
                return []

            # Step 2: efetch summaries
            efetch_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                f"?db=pubmed&id={','.join(pmids)}&retmode=json"
            )
            _assert_safe_url(efetch_url)  # raises ValueError on private/metadata IPs
            async with aiohttp.ClientSession() as session:
                async with session.get(efetch_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        raise _RateLimitError("pubmed")
                    if resp.status != 200:
                        return []
                    efetch_data = await resp.json()

        results: list[SearchResult] = []
        result_set = efetch_data.get("result", {})
        for i, pmid in enumerate(pmids[:10]):
            doc = result_set.get(pmid, {})
            if not doc:
                continue
            title = doc.get("title", "")
            pub_date = doc.get("pubdate", "")
            url_val = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            results.append(SearchResult(
                title=title,
                url=url_val,
                snippet=title[:300],
                source_engine="pubmed",
                confidence=self._heuristic_confidence(query, url_val, i, title),
                rank=i,
                published_date=pub_date,
            ))
        return results

    async def _search_searxng_default(self, query: str) -> list[SearchResult]:
        """SearXNG with the configured instance URL."""
        return await self._search_searxng(query, self._searxng_url)

    async def _search_searxng(self, query: str, instance_url: str) -> list[SearchResult]:
        """SearXNG self-hosted instance — skipped if instance_url is empty."""
        if not instance_url:
            return []
        try:
            import aiohttp
        except ImportError:
            return []

        url = f"{instance_url.rstrip('/')}/search?q={quote_plus(query)}&format=json"
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs (especially for user-supplied instance_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("searxng")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", "")[:300],
                source_engine="searxng",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("content", "")
                ),
                rank=i,
                published_date=item.get("publishedDate", ""),
            ))
        return results

    async def _search_perplexity(self, query: str) -> list[SearchResult]:
        """Perplexity API — vault key: perplexity_api_key.  Used for deep searches."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("perplexity_api_key")
        if not api_key:
            logger.debug("No perplexity_api_key in vault — skipping Perplexity")
            return []

        url = "https://api.perplexity.ai/chat/completions"
        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": query}],
            "return_citations": True,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        _assert_safe_url(url)  # raises ValueError on private/metadata IPs
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 429:
                    raise _RateLimitError("perplexity")
                if resp.status != 200:
                    return []
                data = await resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations: list[str] = data.get("citations", [])

        results: list[SearchResult] = []
        for i, cite_url in enumerate(citations[:10]):
            results.append(SearchResult(
                title=f"Perplexity citation {i + 1}",
                url=cite_url,
                snippet=content[:300] if i == 0 else "",
                source_engine="perplexity",
                confidence=self._heuristic_confidence(query, cite_url, i, content),
                rank=i,
                published_date="",
            ))
        # If no citations, wrap the answer itself
        if not citations and content:
            results.append(SearchResult(
                title="Perplexity answer",
                url="https://www.perplexity.ai/",
                snippet=content[:300],
                source_engine="perplexity",
                confidence=0.75,
                rank=0,
                published_date="",
            ))
        return results

    # ------------------------------------------------------------------ #
    # Confidence scoring                                                  #
    # ------------------------------------------------------------------ #

    def _heuristic_confidence(
        self, query: str, url: str, rank: int, snippet: str
    ) -> float:
        """
        Compute a base confidence score in [0.0, 1.0].

        Factors:
        - Rank decay: higher-ranked results get higher base score
        - Domain authority bonus: .edu / .gov / .org
        - Freshness bonus: snippet mentions year >= 2023
        - Snippet relevance: keyword overlap
        """
        # Rank decay: rank 0 → 0.85, rank 9 → ~0.40
        base = max(0.40, 0.85 - rank * 0.05)

        # Domain authority
        low_url = url.lower()
        if ".edu" in low_url or ".gov" in low_url:
            base = min(1.0, base + 0.10)
        elif ".org" in low_url:
            base = min(1.0, base + 0.05)

        # Freshness: recent year mentions in snippet
        import re
        if re.search(r"202[3-9]|2030", snippet):
            base = min(1.0, base + 0.05)

        # Keyword overlap: simple token match
        query_tokens = set(query.lower().split())
        snippet_tokens = set(snippet.lower().split())
        overlap = len(query_tokens & snippet_tokens)
        if overlap:
            base = min(1.0, base + min(0.10, overlap * 0.02))

        return round(base, 4)

    def _cross_engine_agreement(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        Boost confidence for URLs appearing in 2+ engines.

        Normalises URLs (strips query string / trailing slash) for comparison.
        """
        from urllib.parse import urlparse

        def _norm(url: str) -> str:
            try:
                p = urlparse(url)
                return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()
            except Exception:
                return url.lower()

        url_count: dict[str, int] = {}
        for r in results:
            norm = _norm(r.url)
            url_count[norm] = url_count.get(norm, 0) + 1

        boosted: list[SearchResult] = []
        for r in results:
            norm = _norm(r.url)
            cnt = url_count.get(norm, 1)
            new_conf = min(1.0, r.confidence + 0.05 * (cnt - 1))
            boosted.append(SearchResult(
                title=r.title,
                url=r.url,
                snippet=r.snippet,
                source_engine=r.source_engine,
                confidence=new_conf,
                rank=r.rank,
                published_date=r.published_date,
            ))
        return boosted


# ---------------------------------------------------------------------------
# Internal exception
# ---------------------------------------------------------------------------

class _RateLimitError(Exception):
    """Raised internally when a provider returns HTTP 429."""
