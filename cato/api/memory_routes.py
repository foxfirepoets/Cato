"""
cato/api/memory_routes.py — Semantic memory search API endpoints.

Endpoints for searching indexed memory files using semantic similarity.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Global search engine instance (initialized on first request)
_search_engine = None


def _get_search_engine(*, initialize: bool = True):
    """Return the cached search engine, optionally creating it on first use."""
    global _search_engine
    if _search_engine is None and initialize:
        try:
            from cato.core.semantic_search import SemanticSearchEngine
            _search_engine = SemanticSearchEngine()
            logger.info("Initialized semantic search engine")
        except Exception as e:
            logger.error(f"Failed to initialize search engine: {e}")
            raise
    return _search_engine


async def search_memory(request: web.Request) -> web.Response:
    """GET /api/memory/search?q=<query>&top_k=4 — Search indexed memory files."""
    try:
        query = request.query.get("q", "").strip()
        top_k = request.query.get("top_k", "4")

        if not query:
            return web.json_response({
                "success": False,
                "error": "Query parameter 'q' is required"
            }, status=400)

        try:
            top_k = int(top_k)
        except ValueError:
            top_k = 4

        engine = _get_search_engine()

        # Load memory file if not yet indexed
        from cato.config import CatoConfig
        config = CatoConfig.load()
        workspace_dir = Path(config.workspace_dir or Path.home() / ".cato" / "workspace").expanduser()
        memory_path = workspace_dir / "MEMORY.md"

        if memory_path.exists() and not engine.chunks:
            engine.load_memory_file(memory_path)
            logger.debug(f"Loaded memory file for search: {memory_path}")

        # Perform search
        results = engine.search(query, top_k=top_k)

        return web.json_response({
            "success": True,
            "query": query,
            "results": results,
            "count": len(results),
            "stats": engine.stats()
        })
    except Exception as e:
        logger.exception(f"Error searching memory: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def index_memory(request: web.Request) -> web.Response:
    """POST /api/memory/index — Re-index memory file (clear and reload)."""
    try:
        engine = _get_search_engine()
        engine.clear()

        from cato.config import CatoConfig
        config = CatoConfig.load()
        workspace_dir = Path(config.workspace_dir or Path.home() / ".cato" / "workspace").expanduser()
        memory_path = workspace_dir / "MEMORY.md"

        if not memory_path.exists():
            return web.json_response({
                "success": False,
                "error": f"Memory file not found: {memory_path}"
            }, status=404)

        count = engine.load_memory_file(memory_path)

        return web.json_response({
            "success": True,
            "chunks_indexed": count,
            "stats": engine.stats()
        })
    except Exception as e:
        logger.exception(f"Error indexing memory: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def memory_stats(request: web.Request) -> web.Response:
    """GET /api/memory/stats — facts count + KG node/edge counts plus semantic search stats."""
    import asyncio as _asyncio
    import sqlite3
    try:
        # Count facts/KG nodes/edges from SQLite memory DBs
        def _count() -> dict:
            facts = 0
            kg_nodes = 0
            kg_edges = 0
            mem_dir = Path.home() / ".cato" / "memory"
            if mem_dir.exists():
                for db_path in mem_dir.glob("*.db"):
                    try:
                        conn = sqlite3.connect(str(db_path))
                        for tbl in ("facts", "kg_nodes", "kg_edges"):
                            try:
                                n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                                if tbl == "facts":
                                    facts += n
                                elif tbl == "kg_nodes":
                                    kg_nodes += n
                                elif tbl == "kg_edges":
                                    kg_edges += n
                            except Exception:
                                pass
                        conn.close()
                    except Exception:
                        pass
            return {"facts": facts, "kg_nodes": kg_nodes, "kg_edges": kg_edges}

        loop = _asyncio.get_running_loop()
        db_stats = await loop.run_in_executor(None, _count)

        engine = _get_search_engine(initialize=False)
        semantic_stats: dict = {"chunks_indexed": 0, "model": "all-MiniLM-L6-v2"}
        if engine is not None:
            semantic_stats = engine.stats()

        return web.json_response({
            "success": True,
            **db_stats,
            "stats": semantic_stats,
        })
    except Exception as e:
        logger.exception(f"Error getting stats: {e}")
        return web.json_response({
            "success": False,
            "facts": 0,
            "kg_nodes": 0,
            "kg_edges": 0,
            "stats": {"chunks_indexed": 0, "model": "all-MiniLM-L6-v2"},
            "error": str(e)
        }, status=500)


def register_routes(app: web.Application) -> None:
    """Register memory search routes with the aiohttp Application."""
    app.router.add_get("/api/memory/search", search_memory)
    app.router.add_post("/api/memory/index", index_memory)
    app.router.add_get("/api/memory/stats", memory_stats)
    logger.info("Memory search routes registered")
