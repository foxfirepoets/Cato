"""
cato/core/memory.py — Hybrid memory system for CATO.

Combines BM25 keyword search with sentence-transformer semantic embeddings.
Storage backend: SQLite at ~/.cato/memory/<agent_id>.db.
Chunking: ~400 tokens per chunk with 80-token overlap.
Ranking: 0.4 * bm25_score + 0.6 * semantic_score.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from ..platform import get_data_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_DIR = get_data_dir() / "memory"
_CHUNK_TOKENS = 400
_CHUNK_OVERLAP_TOKENS = 80
_MODEL_NAME = "all-MiniLM-L6-v2"

# ANN index threshold: use hnswlib when chunk count exceeds this value
ANN_THRESHOLD = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    source_file TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source ON chunks(source_file);
CREATE TABLE IF NOT EXISTS distilled_summaries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    turn_start     INTEGER NOT NULL,
    turn_end       INTEGER NOT NULL,
    summary        TEXT NOT NULL,
    key_facts      TEXT NOT NULL,
    decisions      TEXT NOT NULL,
    open_questions TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 0.75,
    created_at     TEXT NOT NULL,
    embedding      BLOB
);
CREATE INDEX IF NOT EXISTS idx_distill_session ON distilled_summaries(session_id);
CREATE TABLE IF NOT EXISTS chunk_usage (
    chunk_id      TEXT PRIMARY KEY,
    chunk_text    TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    avg_score     REAL NOT NULL DEFAULT 0.0,
    last_used     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    key              TEXT PRIMARY KEY,
    value            TEXT NOT NULL,
    confidence       REAL DEFAULT 1.0,
    source_session   TEXT,
    last_reinforced  REAL,
    decay_factor     REAL DEFAULT 0.95,
    created_at       REAL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at REAL
);
CREATE TABLE IF NOT EXISTS kg_nodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL,
    label          TEXT NOT NULL UNIQUE,
    embedding      BLOB,
    source_session TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_label ON kg_nodes(label);
CREATE TABLE IF NOT EXISTS kg_edges (
    from_id       INTEGER NOT NULL REFERENCES kg_nodes(id),
    to_id         INTEGER NOT NULL REFERENCES kg_nodes(id),
    relation_type TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    source_session TEXT,
    created_at    REAL NOT NULL,
    PRIMARY KEY (from_id, to_id, relation_type)
);
CREATE TABLE IF NOT EXISTS corrections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type        TEXT NOT NULL,
    wrong_approach   TEXT NOT NULL,
    correct_approach TEXT NOT NULL,
    context_hash     TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    timestamp        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_hash ON corrections(context_hash);
CREATE TABLE IF NOT EXISTS skill_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content      TEXT NOT NULL,
    timestamp    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_name ON skill_versions(skill_name);
"""

_FACTS_MIGRATION_VERSION = 1


def _apply_facts_migration(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add any missing columns to the facts table."""
    # Run CREATE TABLE IF NOT EXISTS for facts + migrations (already in schema)
    # Now add columns that may be missing in pre-existing DBs
    columns_to_add = [
        ("confidence",      "REAL DEFAULT 1.0"),
        ("source_session",  "TEXT"),
        ("last_reinforced", "REAL"),
        ("decay_factor",    "REAL DEFAULT 0.95"),
        ("created_at",      "REAL"),
    ]
    for col_name, col_def in columns_to_add:
        try:
            conn.execute(f"ALTER TABLE facts ADD COLUMN {col_name} {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            # Column already exists — idempotent
            pass

    # Track migration
    already = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (_FACTS_MIGRATION_VERSION,),
    ).fetchone()
    if not already:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (_FACTS_MIGRATION_VERSION, time.time()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# MemorySystem
# ---------------------------------------------------------------------------

class MemorySystem:
    """
    Hybrid long-term memory using BM25 + sentence-transformer embeddings.

    Usage::

        mem = MemorySystem(agent_id="my-agent")
        mem.store("The capital of France is Paris.", source_file="MEMORY.md")
        results = mem.search("France capital city", top_k=3)
        for r in results:
            print(r)
    """

    def __init__(
        self,
        agent_id: str = "default",
        memory_dir: Optional[Path] = None,
    ) -> None:
        self._agent_id = agent_id
        self._dir = (memory_dir or _MEMORY_DIR).expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

        self._db_path = self._dir / f"{agent_id}.db"
        self._write_lock = threading.Lock()
        self._conn = self._open_db()

        # Lazy-load sentence transformer (heavy — only once per process)
        self._embed_model: Optional[SentenceTransformer] = None

        # ANN index (hnswlib) — built lazily when chunk count > ANN_THRESHOLD
        self._ann_index: Optional[object] = None
        self._ann_index_ids: list[int] = []   # maps HNSW internal id -> SQLite row id
        self._ann_dirty: bool = True           # True when index needs rebuild

    # ------------------------------------------------------------------
    # Lazy embedding model
    # ------------------------------------------------------------------

    def _get_embed_model(self) -> Optional[SentenceTransformer]:
        """
        Lazy-load the sentence-transformer model with a 3-attempt retry loop.

        Sets a Windows-safe cache path before instantiation and creates the
        directory unconditionally.  On all-attempts-failed, sets
        ``self._embed_model`` to ``None`` and logs a warning so the daemon
        continues in degraded (BM25-only) mode.

        Returns ``None`` when the model could not be loaded.
        """
        if self._embed_model is not None:
            return self._embed_model

        # --- resolve a Windows-safe cache directory ---------------------------
        if sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            hf_cache = str(Path(local_app_data) / "cato" / "hf_cache")
        else:
            hf_cache = str(Path.home() / ".cache" / "cato" / "hf")

        os.makedirs(hf_cache, exist_ok=True)
        os.environ["HF_HOME"] = hf_cache
        os.environ["SENTENCE_TRANSFORMERS_HOME"] = hf_cache

        # --- 3-attempt retry loop ---------------------------------------------
        _MAX_ATTEMPTS = 3
        _RETRY_SLEEP = 2  # seconds between attempts

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            logger.info(
                "Loading embedding model %s (attempt %d/%d, cache=%s) ...",
                _MODEL_NAME, attempt, _MAX_ATTEMPTS, hf_cache,
            )
            try:
                self._embed_model = SentenceTransformer(_MODEL_NAME)
                logger.info(
                    "Embedding model %s loaded successfully on attempt %d.",
                    _MODEL_NAME, attempt,
                )
                return self._embed_model
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "Embedding model load attempt %d/%d failed: %s",
                    attempt, _MAX_ATTEMPTS, exc,
                )
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_SLEEP)

        # All attempts exhausted — degrade gracefully
        logger.warning(
            "All %d attempts to load embedding model %s failed (last error: %s). "
            "Semantic search is disabled; BM25 keyword search still works.",
            _MAX_ATTEMPTS, _MODEL_NAME, last_exc,
        )
        self._embed_model = None
        return None

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _apply_facts_migration(conn)
        # Hard dependency check: facts table must exist (required by Knowledge Graph)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='facts'"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "MemorySystem requires the 'facts' table (Skill 2 / Mem0). "
                "Ensure the schema has been applied correctly before using this class."
            )
        return conn

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize_simple(text: str) -> list[str]:
        """Rough word-level tokeniser — used only for chunk sizing."""
        return text.split()

    def _chunk_text(self, text: str) -> list[str]:
        """
        Split *text* into overlapping chunks of ~_CHUNK_TOKENS words.

        Overlap of _CHUNK_OVERLAP_TOKENS words is kept between consecutive
        chunks to preserve context across boundaries.
        """
        words = self._tokenize_simple(text)
        if len(words) <= _CHUNK_TOKENS:
            return [text] if text.strip() else []

        chunks: list[str] = []
        start = 0
        step = _CHUNK_TOKENS - _CHUNK_OVERLAP_TOKENS
        while start < len(words):
            end = min(start + _CHUNK_TOKENS, len(words))
            chunk = " ".join(words[start:end])
            if chunk.strip():
                chunks.append(chunk)
            if end >= len(words):
                break
            start += step

        return chunks

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[bytes]:
        """
        Return embedding blobs (numpy float32 arrays serialised as bytes).

        Returns a list of zero-filled blobs (384-dim float32) when the model is
        unavailable so callers can proceed without crashing.
        """
        model = self._get_embed_model()
        if model is None:
            # Degraded mode: return zero vectors so storage/search still work
            # (semantic scores will all be 0.0; BM25 will carry the ranking)
            zero = np.zeros(384, dtype=np.float32).tobytes()
            return [zero] * len(texts)
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.astype(np.float32).tobytes() for v in vecs]

    @staticmethod
    def _bytes_to_vec(blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(self, content: str, source_file: str = "") -> int:
        """
        Chunk *content* and store each chunk with its embedding.

        Returns the number of chunks written.
        """
        chunks = self._chunk_text(content)
        if not chunks:
            return 0

        blobs = self._embed(chunks)
        now = self._now_iso()
        rows = [
            (chunk, blob, source_file, now, now)
            for chunk, blob in zip(chunks, blobs)
        ]
        with self._write_lock:
            self._conn.executemany(
                "INSERT INTO chunks (content, embedding, source_file, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        self._ann_dirty = True  # Invalidate ANN index on new writes
        logger.debug("Stored %d chunks from %s", len(chunks), source_file or "<inline>")
        return len(chunks)

    # ------------------------------------------------------------------
    # ANN index (hnswlib opt-in — P2-10)
    # ------------------------------------------------------------------

    def _build_ann_index_if_needed(self) -> None:
        """
        Build an hnswlib ANN index if chunk_count > ANN_THRESHOLD and
        hnswlib is importable.

        Falls back silently to brute-force if hnswlib is not installed.
        The index is rebuilt whenever _ann_dirty is True.
        """
        if not self._ann_dirty:
            return

        count = self.chunk_count()
        if count <= ANN_THRESHOLD:
            return  # Still small enough for brute-force

        try:
            import hnswlib  # type: ignore[import]
        except ImportError:
            logger.debug("hnswlib not installed — using brute-force search (chunk_count=%d)", count)
            return

        try:
            rows = self._conn.execute(
                "SELECT id, embedding FROM chunks ORDER BY id"
            ).fetchall()

            if not rows:
                return

            # Determine embedding dimension from first row
            first_vec = self._bytes_to_vec(rows[0]["embedding"])
            dim = first_vec.shape[0]

            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=len(rows), ef_construction=200, M=16)

            ids: list[int] = []
            vecs = []
            for i, row in enumerate(rows):
                ids.append(row["id"])
                vecs.append(self._bytes_to_vec(row["embedding"]))

            import numpy as _np
            index.add_items(_np.array(vecs, dtype=_np.float32), list(range(len(ids))))
            index.set_ef(50)

            self._ann_index = index
            self._ann_index_ids = ids
            self._ann_dirty = False
            logger.info("ANN index built with %d chunks (dim=%d)", len(ids), dim)

        except Exception as exc:
            logger.warning("Failed to build ANN index: %s — falling back to brute-force", exc)
            self._ann_index = None

    def _search_embeddings(self, query_vec: "np.ndarray", top_k: int) -> list[dict]:
        """
        Route embedding search to HNSW ANN index or brute-force cosine scan.

        Returns list of dicts with keys: id, content, score.
        """
        self._build_ann_index_if_needed()

        rows = self._conn.execute(
            "SELECT id, content, embedding FROM chunks"
        ).fetchall()

        if not rows:
            return []

        if self._ann_index is not None and len(rows) > ANN_THRESHOLD:
            # HNSW fast path
            try:
                labels, distances = self._ann_index.knn_query(
                    query_vec.reshape(1, -1), k=min(top_k, len(self._ann_index_ids))
                )
                results = []
                for label, dist in zip(labels[0], distances[0]):
                    if label < len(self._ann_index_ids):
                        row_id = self._ann_index_ids[label]
                        row = self._conn.execute(
                            "SELECT content FROM chunks WHERE id = ?", (row_id,)
                        ).fetchone()
                        if row:
                            results.append({"id": row_id, "content": row["content"], "score": float(1 - dist)})
                return results
            except Exception as exc:
                logger.warning("ANN search failed: %s — falling back to brute-force", exc)

        # Brute-force fallback
        contents = [r["content"] for r in rows]
        embeddings = [self._bytes_to_vec(r["embedding"]) for r in rows]
        import numpy as _np
        scores = _np.array([self._cosine(query_vec, e) for e in embeddings])
        top_indices = _np.argsort(scores)[::-1][:top_k]
        return [
            {"id": rows[i]["id"], "content": contents[i], "score": float(scores[i])}
            for i in top_indices
        ]

    def search(self, query: str, top_k: int = 5) -> list[str]:
        """
        Hybrid BM25 + semantic search.  Returns top_k chunk strings.

        Scoring: 0.4 * normalised_bm25 + 0.6 * cosine_similarity.
        Routes to HNSW ANN index when chunk_count > ANN_THRESHOLD.
        """
        rows = self._conn.execute(
            "SELECT id, content, embedding FROM chunks"
        ).fetchall()

        if not rows:
            return []

        contents = [r["content"] for r in rows]
        embeddings = [self._bytes_to_vec(r["embedding"]) for r in rows]

        # BM25
        tokenized_corpus = [c.lower().split() for c in contents]
        bm25 = BM25Okapi(tokenized_corpus)
        query_tokens = query.lower().split()
        bm25_scores_raw = bm25.get_scores(query_tokens)

        # Normalise BM25 to [0, 1]
        bm25_max = float(np.max(bm25_scores_raw)) if np.max(bm25_scores_raw) > 0 else 1.0
        bm25_scores = bm25_scores_raw / bm25_max

        # Semantic
        _sem_model = self._get_embed_model()
        if _sem_model is not None:
            q_vec = _sem_model.encode(
                [query], normalize_embeddings=True, show_progress_bar=False
            )[0].astype(np.float32)
            sem_scores = np.array([self._cosine(q_vec, e) for e in embeddings])
        else:
            # Degraded mode: semantic scores all zero; fall back to BM25-only ranking
            sem_scores = np.zeros(len(contents), dtype=np.float32)

        # Combined
        combined = 0.4 * bm25_scores + 0.6 * sem_scores
        top_indices = np.argsort(combined)[::-1][:top_k]

        return [contents[i] for i in top_indices]

    def flush_to_disk(self, content: str, date_str: str) -> Path:
        """
        Write *content* to the daily memory log file for *date_str* (YYYY-MM-DD).

        Appends if the file already exists so multiple flush calls accumulate.
        Returns the path written to.
        """
        out_path = self._dir / f"{date_str}.md"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        entry = f"\n\n<!-- flushed at {ts} -->\n{content.strip()}\n"
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        logger.debug("Flushed %d chars to %s", len(content), out_path)
        return out_path

    def load_workspace_files(self, workspace_dir: Path) -> int:
        """
        Index all .md files in *workspace_dir* that have not yet been stored.

        Compares source_file paths to avoid re-indexing unchanged files.
        Returns the number of new chunks written.
        """
        workspace_dir = workspace_dir.expanduser().resolve()
        md_files = sorted(workspace_dir.glob("**/*.md"))

        # Fetch already-indexed paths
        existing = {
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT source_file FROM chunks"
            ).fetchall()
        }

        total_chunks = 0
        for md_file in md_files:
            path_key = str(md_file)
            if path_key in existing:
                logger.debug("Skipping already-indexed %s", md_file.name)
                continue
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                n = self.store(content, source_file=path_key)
                total_chunks += n
                logger.info("Indexed %s: %d chunks", md_file.name, n)
            except OSError as exc:
                logger.warning("Could not read %s: %s", md_file, exc)

        return total_chunks

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def astore(self, content: str, source_file: str = "") -> int:
        """Async wrapper around :meth:`store`."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.store, content, source_file)

    async def asearch(self, query: str, top_k: int = 5) -> list[str]:
        """Async wrapper around :meth:`search`."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.search, query, top_k)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def delete_by_source(self, source_file: str) -> int:
        """Delete all chunks for a given source file. Returns deleted count."""
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM chunks WHERE source_file = ?", (source_file,)
            )
            self._conn.commit()
        return cur.rowcount

    def chunk_count(self) -> int:
        """Return total number of stored chunks."""
        return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # ------------------------------------------------------------------
    # Distillation support
    # ------------------------------------------------------------------

    def store_distillation(self, result: "DistillationResult") -> int:
        """
        Persist a :class:`DistillationResult` to the ``distilled_summaries`` table.

        Returns the SQLite rowid of the inserted row.
        """
        # Import here to avoid circular dependency with distiller module
        from .distiller import DistillationResult  # noqa: F401

        key_facts_json = json.dumps(result.key_facts)
        decisions_json = json.dumps(result.decisions)
        open_questions_json = json.dumps(result.open_questions)

        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO distilled_summaries
                    (session_id, turn_start, turn_end, summary,
                     key_facts, decisions, open_questions,
                     confidence, created_at, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.session_id,
                    result.turn_start,
                    result.turn_end,
                    result.summary,
                    key_facts_json,
                    decisions_json,
                    open_questions_json,
                    result.confidence,
                    result.created_at,
                    result.embedding,
                ),
            )
            self._conn.commit()
        return cur.lastrowid

    def search_distilled(
        self,
        query: str,
        session_id: str | None = None,
        top_k: int = 3,
    ) -> list[dict]:
        """
        Cosine similarity search over distilled summary embeddings.

        Args:
            query: Search query string.
            session_id: If provided, restrict search to this session.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys: id, session_id, turn_start, turn_end,
            summary, key_facts, decisions, open_questions, confidence, score,
            source_file.
        """
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM distilled_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM distilled_summaries"
            ).fetchall()

        if not rows:
            return []

        # Build query embedding
        _sem_model = self._get_embed_model()
        if _sem_model is not None:
            q_vec: Optional[np.ndarray] = _sem_model.encode(
                [query], normalize_embeddings=True, show_progress_bar=False
            )[0].astype(np.float32)
        else:
            # Degraded mode: no semantic ranking; all rows score 0.0
            q_vec = None

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            if q_vec is None or row["embedding"] is None:
                score = 0.0
            else:
                row_vec = self._bytes_to_vec(row["embedding"])
                score = self._cosine(q_vec, row_vec)
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        results = []
        for score, row in top:
            sid = row["session_id"]
            ts = row["turn_start"]
            te = row["turn_end"]
            results.append(
                {
                    "id": row["id"],
                    "session_id": sid,
                    "turn_start": ts,
                    "turn_end": te,
                    "summary": row["summary"],
                    "key_facts": json.loads(row["key_facts"]),
                    "decisions": json.loads(row["decisions"]),
                    "open_questions": json.loads(row["open_questions"]),
                    "confidence": row["confidence"],
                    "score": score,
                    "source_file": f"distill:{sid}:{ts}-{te}",
                }
            )
        return results

    def latest_distilled_turn_end(self, session_id: str) -> int:
        """Return the highest distilled turn_end for *session_id*, or -1."""
        row = self._conn.execute(
            "SELECT MAX(turn_end) AS max_turn_end FROM distilled_summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or row["max_turn_end"] is None:
            return -1
        return int(row["max_turn_end"])

    def load_recent_distillations(
        self,
        limit: int = 6,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """Return the most recent distilled summaries, newest first."""
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM distilled_summaries WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM distilled_summaries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "turn_start": row["turn_start"],
                "turn_end": row["turn_end"],
                "summary": row["summary"],
                "key_facts": json.loads(row["key_facts"]),
                "decisions": json.loads(row["decisions"]),
                "open_questions": json.loads(row["open_questions"]),
                "confidence": row["confidence"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def load_recent_corrections(self, limit: int = 6) -> list[dict]:
        """Return the most recent correction records, newest first."""
        rows = self._conn.execute(
            "SELECT id, task_type, wrong_approach, correct_approach, context_hash, "
            "session_id, timestamp FROM corrections ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # Mem0: Fact store
    # ------------------------------------------------------------------

    def store_fact(
        self,
        key: str,
        value: str,
        confidence: float = 1.0,
        source_session: Optional[str] = None,
    ) -> None:
        """
        UPSERT a fact.

        If *key* already exists: reinforce confidence (min 1.0) and update
        last_reinforced timestamp.
        If *key* is new: insert with given confidence.
        """
        now = time.time()
        with self._write_lock:
            existing = self._conn.execute(
                "SELECT confidence FROM facts WHERE key = ?", (key,)
            ).fetchone()
            if existing is not None:
                new_conf = min(1.0, existing["confidence"] + 0.1)
                self._conn.execute(
                    "UPDATE facts SET value = ?, confidence = ?, last_reinforced = ?, source_session = ? WHERE key = ?",
                    (value, new_conf, now, source_session, key),
                )
            else:
                self._conn.execute(
                    "INSERT INTO facts (key, value, confidence, source_session, last_reinforced, decay_factor, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 0.95, ?)",
                    (key, value, confidence, source_session, now, now),
                )
            self._conn.commit()

    def load_top_facts(self, n: int = 50) -> list[dict]:
        """Return top *n* facts ordered by recency then confidence."""
        rows = self._conn.execute(
            "SELECT key, value, confidence, source_session, last_reinforced, decay_factor, created_at"
            " FROM facts ORDER BY last_reinforced DESC, confidence DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def apply_decay(self, sessions_since_reinforced: int) -> int:
        """
        Decay confidence for facts that haven't been reinforced recently.

        Facts whose last_reinforced timestamp is older than
        *sessions_since_reinforced* seconds ago are multiplied by decay_factor.
        Returns number of rows updated.
        """
        threshold = time.time() - sessions_since_reinforced
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE facts SET confidence = confidence * decay_factor"
                " WHERE last_reinforced < ?",
                (threshold,),
            )
            self._conn.commit()
        return cur.rowcount

    def forget_fact(self, key: str) -> bool:
        """Delete a fact by key. Returns True if found and deleted, False otherwise."""
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM facts WHERE key = ?", (key,))
            self._conn.commit()
        return cur.rowcount > 0

    def forget_all_facts(self) -> int:
        """Delete all facts. Returns count of deleted rows."""
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM facts")
            self._conn.commit()
        return cur.rowcount

    def fact_count(self) -> int:
        """Return total number of stored facts."""
        return self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    # ------------------------------------------------------------------
    # Knowledge Graph (Skill 9 — Cognee)
    # ------------------------------------------------------------------

    def add_node(
        self,
        type: str,
        label: str,
        embedding: Optional[bytes] = None,
        source_session: Optional[str] = None,
    ) -> int:
        """
        INSERT OR IGNORE a knowledge graph node (deduplicates by label).

        Returns the node id.
        """
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO kg_nodes (type, label, embedding, source_session, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (type, label, embedding, source_session, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM kg_nodes WHERE label = ?", (label,)
            ).fetchone()
        return int(row["id"])

    def seed_nodes_from_facts(self, session_id: Optional[str] = None) -> int:
        """
        Read facts table and create kg_nodes for each unique key.

        Returns count of nodes created.
        """
        rows = self._conn.execute("SELECT key FROM facts").fetchall()
        count = 0
        for row in rows:
            label = str(row["key"])
            now = time.time()
            with self._write_lock:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO kg_nodes (type, label, embedding, source_session, created_at)"
                    " VALUES (?, ?, NULL, ?, ?)",
                    ("concept", label, session_id, now),
                )
                self._conn.commit()
            if cur.rowcount > 0:
                count += 1
        return count

    def extract_and_add_nodes(
        self,
        text: str,
        session_id: Optional[str] = None,
    ) -> list[int]:
        """
        Heuristic entity extraction from *text*.

        Detected patterns:
        - File paths  (.py, .ts, .json, .yaml, .yml, .toml, .md, .js, .tsx, .go, .rs)
        - @mentions
        - CamelCase words >= 8 chars
        - ALL_CAPS words >= 4 chars

        Returns list of node ids.
        """
        ids: list[int] = []
        seen: set[str] = set()

        # File paths
        file_re = re.compile(
            r"\b[\w./\\-]+\.(?:py|ts|tsx|js|json|yaml|yml|toml|md|go|rs|sh|txt|csv)\b"
        )
        for m in file_re.finditer(text):
            label = m.group(0)
            if label not in seen:
                seen.add(label)
                ids.append(self.add_node("file", label, source_session=session_id))

        # @mentions
        mention_re = re.compile(r"@([A-Za-z0-9_]+)")
        for m in mention_re.finditer(text):
            label = m.group(1)
            if label not in seen:
                seen.add(label)
                ids.append(self.add_node("person", label, source_session=session_id))

        # CamelCase words >= 8 chars
        camel_re = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]*)+)\b")
        for m in camel_re.finditer(text):
            label = m.group(1)
            if len(label) >= 8 and label not in seen:
                seen.add(label)
                ids.append(self.add_node("concept", label, source_session=session_id))

        # ALL_CAPS words >= 4 chars (not already matched as file path)
        caps_re = re.compile(r"\b([A-Z_]{4,})\b")
        for m in caps_re.finditer(text):
            label = m.group(1)
            if label not in seen and not label.startswith("_") and not label.endswith("_"):
                seen.add(label)
                ids.append(self.add_node("concept", label, source_session=session_id))

        return ids

    def add_edge(
        self,
        from_label: str,
        to_label: str,
        relation_type: str = "co_mentioned",
        weight: float = 1.0,
        source_session: Optional[str] = None,
    ) -> bool:
        """
        Add (or reinforce) a directed edge between two nodes.

        Auto-creates nodes with type "concept" if they do not exist.
        On conflict, weight is reinforced: weight = weight + 1.0.
        Returns True if an edge was inserted or updated.
        """
        from_id = self.add_node("concept", from_label, source_session=source_session)
        to_id = self.add_node("concept", to_label, source_session=source_session)
        now = time.time()
        with self._write_lock:
            existing = self._conn.execute(
                "SELECT weight FROM kg_edges WHERE from_id=? AND to_id=? AND relation_type=?",
                (from_id, to_id, relation_type),
            ).fetchone()
            if existing is not None:
                new_weight = existing["weight"] + 1.0
                self._conn.execute(
                    "UPDATE kg_edges SET weight=? WHERE from_id=? AND to_id=? AND relation_type=?",
                    (new_weight, from_id, to_id, relation_type),
                )
            else:
                self._conn.execute(
                    "INSERT INTO kg_edges (from_id, to_id, relation_type, weight, source_session, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (from_id, to_id, relation_type, weight, source_session, now),
                )
            self._conn.commit()
        return True

    def extract_and_add_edges(
        self,
        text: str,
        session_id: Optional[str] = None,
    ) -> None:
        """
        Detect co-occurring entity pairs in the same sentence and create/reinforce
        co_mentioned edges between them.
        """
        # Split into sentences
        sentences = re.split(r"[.!?]\s+|\n", text)
        entity_re = re.compile(
            r"(?:"
            r"[\w./\\-]+\.(?:py|ts|tsx|js|json|yaml|yml|toml|md|go|rs|sh|txt|csv)"
            r"|@[A-Za-z0-9_]+"
            r"|[A-Z][a-z]+(?:[A-Z][a-z]*)+"
            r"|[A-Z_]{4,}"
            r")"
        )
        for sentence in sentences:
            entities = [m.group(0).lstrip("@") for m in entity_re.finditer(sentence)]
            # Deduplicate while preserving order
            seen: list[str] = []
            for e in entities:
                if e not in seen:
                    seen.append(e)
            entities = seen
            for i in range(len(entities)):
                for j in range(i + 1, len(entities)):
                    self.add_edge(
                        entities[i], entities[j],
                        relation_type="co_mentioned",
                        source_session=session_id,
                    )

    def query_graph(self, start_label: str, depth: int = 3) -> list[dict]:
        """
        Multi-hop graph traversal from *start_label* up to *depth* hops.

        Returns list of dicts with keys: label, type, relation_type, weight, depth.
        Uses a recursive CTE for efficiency.
        """
        sql = """
WITH RECURSIVE graph(from_id, to_id, relation_type, weight, depth) AS (
    SELECT e.from_id, e.to_id, e.relation_type, e.weight, 1
    FROM kg_edges e
    JOIN kg_nodes n ON n.id = e.from_id
    WHERE n.label = ?
    UNION ALL
    SELECT e.from_id, e.to_id, e.relation_type, e.weight, g.depth + 1
    FROM kg_edges e
    JOIN graph g ON g.to_id = e.from_id
    WHERE g.depth < ?
)
SELECT DISTINCT n.label, n.type, g.relation_type, g.weight, g.depth
FROM graph g JOIN kg_nodes n ON n.id = g.to_id
ORDER BY g.depth, g.weight DESC
"""
        rows = self._conn.execute(sql, (start_label, depth)).fetchall()
        return [dict(r) for r in rows]

    def related_concepts(self, label: str, max_hops: int = 2) -> list[dict]:
        """
        Return nodes within *max_hops* of *label*, ranked by edge weight descending.
        """
        results = self.query_graph(label, depth=max_hops)
        # Sort by weight desc, then depth asc
        results.sort(key=lambda r: (-r["weight"], r["depth"]))
        return results

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()
