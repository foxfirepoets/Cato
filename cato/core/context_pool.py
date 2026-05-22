"""
cato/core/context_pool.py — Automated Context A/B Testing (Self-Optimizing).

Phase G — Step 8: Every 10th turn, test a challenger context pool with the
bottom-20% of chunks removed. If equal or better confidence over 3 consecutive
turns, promote challenger to champion.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# CANONICAL PATH: chunk_usage is also defined in memory.py for per-agent DBs.
# When ContextPool is constructed with a memory instance, it uses memory._db_path.
# When constructed without a memory instance, it defaults to context_pool.db.
# This split means queries in different contexts may read from different files.
# Always construct ContextPool with a memory instance to ensure consistency.
# Schema for chunk_usage table (also referenced in memory.py _SCHEMA extension)
_CHUNK_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_usage (
    chunk_id      TEXT PRIMARY KEY,
    chunk_text    TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    avg_score     REAL NOT NULL DEFAULT 0.0,
    last_used     REAL NOT NULL
);
"""


@dataclass
class ABTestState:
    """State tracker for A/B test between champion and challenger context pools."""

    consecutive_successes: int = 0
    consecutive_failures: int = 0
    total_ab_turns: int = 0
    total_promotions: int = 0


class ContextPool:
    """
    Self-optimizing context pool using A/B testing.

    Tracks chunk usage statistics in SQLite. Every 10th turn, tests a
    challenger pool that excludes the bottom 20% of chunks by avg_score.
    After 3 consecutive challenger successes, promotes challenger to champion.

    Usage::

        pool = ContextPool(memory, db_path=Path("~/.cato/memory/pool.db"))
        pool.record_usage("chunk-id-1", "chunk text ...", confidence=0.91)
        champions = pool.get_champion_chunks(top_k=5)
        if pool.should_run_ab_test(turn_number=10):
            challengers = pool.get_challenger_chunks(top_k=5)
    """

    def __init__(
        self,
        memory,
        db_path: Optional[Path] = None,
    ) -> None:
        self._memory = memory
        self._ab_state = ABTestState()

        if memory is None:
            logger.warning(
                "[ContextPool] No memory instance provided. chunk_usage will write to "
                "context_pool.db, which may diverge from agent memory DB. "
                "Pass a memory instance for consistency."
            )

        # Connect to same db as memory if possible, otherwise use provided path
        if db_path is not None:
            self._db_path = db_path.expanduser().resolve()
        elif hasattr(memory, "_db_path"):
            self._db_path = memory._db_path
        else:
            from ..platform import get_data_dir
            data_dir = get_data_dir() / "memory"
            data_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = data_dir / "context_pool.db"

        self._conn = self._open_db()

    # ------------------------------------------------------------------
    # DB setup
    # ------------------------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_CHUNK_USAGE_SCHEMA)
        conn.commit()
        return conn

    # ------------------------------------------------------------------
    # Chunk usage tracking
    # ------------------------------------------------------------------

    def record_usage(
        self,
        chunk_id: str,
        chunk_text: str,
        confidence: float,
        success_threshold: float = 0.80,
    ) -> None:
        """
        Record a chunk being used in a turn.

        Increments use_count unconditionally. If confidence >= success_threshold,
        increments success_count. Recalculates avg_score = success_count / use_count.

        Args:
            chunk_id:          Unique identifier for the chunk.
            chunk_text:        The text content of the chunk.
            confidence:        Model confidence for this turn.
            success_threshold: Minimum confidence to count as a success.
        """
        now = time.time()
        is_success = 1 if confidence >= success_threshold else 0

        # Upsert: insert or update existing row
        existing = self._conn.execute(
            "SELECT use_count, success_count FROM chunk_usage WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()

        if existing is None:
            new_use_count = 1
            new_success_count = is_success
            new_avg = float(new_success_count) / new_use_count
            self._conn.execute(
                """
                INSERT INTO chunk_usage (chunk_id, chunk_text, use_count, success_count, avg_score, last_used)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, chunk_text, new_use_count, new_success_count, new_avg, now),
            )
        else:
            new_use_count = existing["use_count"] + 1
            new_success_count = existing["success_count"] + is_success
            new_avg = float(new_success_count) / new_use_count
            self._conn.execute(
                """
                UPDATE chunk_usage
                SET use_count = ?, success_count = ?, avg_score = ?, last_used = ?,
                    chunk_text = ?
                WHERE chunk_id = ?
                """,
                (new_use_count, new_success_count, new_avg, now, chunk_text, chunk_id),
            )

        self._conn.commit()
        logger.debug(
            "Recorded usage for chunk %s: use_count=%d, success_count=%d, avg_score=%.3f",
            chunk_id, new_use_count, new_success_count, new_avg,
        )

    # ------------------------------------------------------------------
    # Champion / Challenger selection
    # ------------------------------------------------------------------

    def get_champion_chunks(self, top_k: int = 10) -> list[dict]:
        """
        Return top-k chunks by avg_score, filtered to use_count >= 3.

        Returns list of dicts with keys: chunk_id, chunk_text, avg_score,
        use_count, success_count.
        """
        rows = self._conn.execute(
            """
            SELECT chunk_id, chunk_text, use_count, success_count, avg_score
            FROM chunk_usage
            WHERE use_count >= 3
            ORDER BY avg_score DESC
            LIMIT ?
            """,
            (top_k,),
        ).fetchall()

        return [
            {
                "chunk_id": row["chunk_id"],
                "chunk_text": row["chunk_text"],
                "use_count": row["use_count"],
                "success_count": row["success_count"],
                "avg_score": row["avg_score"],
            }
            for row in rows
        ]

    def get_challenger_chunks(self, top_k: int = 10) -> list[dict]:
        """
        Return top-k chunks by avg_score, excluding bottom 20% by avg_score.

        Filters use_count >= 3 and then computes the 20th-percentile avg_score
        cutoff, excluding chunks at or below that cutoff.

        Returns list of dicts with keys: chunk_id, chunk_text, avg_score,
        use_count, success_count.
        """
        # Fetch all qualifying chunks to compute percentile
        all_rows = self._conn.execute(
            """
            SELECT chunk_id, chunk_text, use_count, success_count, avg_score
            FROM chunk_usage
            WHERE use_count >= 3
            ORDER BY avg_score ASC
            """,
        ).fetchall()

        if not all_rows:
            return []

        scores = [row["avg_score"] for row in all_rows]
        percentile_20 = self._percentile_20(scores)

        # Exclude bottom 20% by avg_score
        filtered = [row for row in all_rows if row["avg_score"] > percentile_20]

        # Sort descending and take top_k
        filtered.sort(key=lambda r: r["avg_score"], reverse=True)
        top = filtered[:top_k]

        return [
            {
                "chunk_id": row["chunk_id"],
                "chunk_text": row["chunk_text"],
                "use_count": row["use_count"],
                "success_count": row["success_count"],
                "avg_score": row["avg_score"],
            }
            for row in top
        ]

    @staticmethod
    def _percentile_20(values: list[float]) -> float:
        """Compute the 20th-percentile value from a sorted list of floats."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        # Index for 20th percentile (floor)
        idx = max(0, int(n * 0.20) - 1)
        return sorted_vals[idx]

    # ------------------------------------------------------------------
    # A/B test scheduling
    # ------------------------------------------------------------------

    def should_run_ab_test(self, turn_number: int) -> bool:
        """
        Return True when turn_number is a multiple of 10 (every 10th turn).

        Args:
            turn_number: Current turn number (1-indexed or 0-indexed both work
                         at multiples of 10).
        """
        return turn_number % 10 == 0

    # ------------------------------------------------------------------
    # A/B state management
    # ------------------------------------------------------------------

    def record_ab_result(
        self,
        turn_is_challenger: bool,
        confidence: float,
        success_threshold: float = 0.80,
    ) -> None:
        """
        Record the result of a challenger turn.

        If challenger turn succeeded (confidence >= threshold):
            - consecutive_successes += 1
            - consecutive_failures reset to 0
        If challenger turn failed:
            - consecutive_successes reset to 0
            - consecutive_failures += 1

        If consecutive_successes >= 3: promote (total_promotions += 1, reset counts).

        Args:
            turn_is_challenger: Whether this turn used the challenger pool.
            confidence:         Model confidence for this turn.
            success_threshold:  Minimum confidence for success.
        """
        self._ab_state.total_ab_turns += 1

        if not turn_is_challenger:
            return

        succeeded = confidence >= success_threshold
        if succeeded:
            self._ab_state.consecutive_successes += 1
            self._ab_state.consecutive_failures = 0
        else:
            self._ab_state.consecutive_successes = 0
            self._ab_state.consecutive_failures += 1

        # Auto-promote after 3 consecutive successes
        if self._ab_state.consecutive_successes >= 3:
            self._ab_state.total_promotions += 1
            self._ab_state.consecutive_successes = 0
            self._ab_state.consecutive_failures = 0
            logger.info(
                "Challenger promoted to champion (total promotions: %d)",
                self._ab_state.total_promotions,
            )

    def should_promote(self) -> bool:
        """
        Return True if the challenger has earned promotion (>= 3 consecutive successes).

        Note: After recording a promotion in record_ab_result, consecutive_successes
        is reset to 0, so this reflects the current in-progress streak.
        """
        return self._ab_state.consecutive_successes >= 3

    def get_ab_stats(self) -> dict:
        """
        Return current A/B test state as a dict.

        Returns dict with keys matching ABTestState fields:
            consecutive_successes, consecutive_failures,
            total_ab_turns, total_promotions.
        """
        return {
            "consecutive_successes": self._ab_state.consecutive_successes,
            "consecutive_failures": self._ab_state.consecutive_failures,
            "total_ab_turns": self._ab_state.total_ab_turns,
            "total_promotions": self._ab_state.total_promotions,
        }

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()
