"""Utilities for turning long-term chat history into a cleaner MEMORY.md."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .distiller import Distiller
from ..config import CatoConfig
from ..platform import get_data_dir

if TYPE_CHECKING:
    from .memory import MemorySystem

logger = logging.getLogger(__name__)

AUTO_START = "<!-- CATO_AUTO_MEMORY_START -->"
AUTO_END = "<!-- CATO_AUTO_MEMORY_END -->"
DEFAULT_SYNC_INTERVAL_SEC = 900
_TOOL_XML_RE = re.compile(r"<(?:minimax:tool_call|tool_call|invoke)\b.*?</(?:minimax:tool_call|tool_call|invoke)>", re.DOTALL)
_TAG_RE = re.compile(r"</?[^>]+>")
_FOOTER_RE = re.compile(r"\[\$[\d.]+ this call \|[^\]]+\]")
_LOW_SIGNAL_FRAGMENTS = (
    "hello there",
    "hi there",
    "unauthorized",
    "i'm claude code",
    "what do you want it to be",
    "document.getelementbyid",
    "console shows saved config",
    "i need to be straight with you",
)
_PREFERENCE_PATTERNS: list[tuple[re.Pattern[str], str, str, float]] = [
    (
        re.compile(r"\bplain english\b|\bnon[- ]coder english\b", re.IGNORECASE),
        "user.communication_style",
        "Ben prefers plain-English, non-coder explanations.",
        0.95,
    ),
    (
        re.compile(r"\boutcome[- ]first\b", re.IGNORECASE),
        "user.response_priority",
        "Ben prefers outcome-first answers.",
        0.9,
    ),
    (
        re.compile(r"\btelegram\b.*\beasier\b|\bprefer\b.*\btelegram\b", re.IGNORECASE),
        "user.primary_channel",
        "Telegram is the preferred human-facing channel.",
        0.85,
    ),
]
_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bcall me ([A-Z][a-z]+)\b", re.IGNORECASE),
    re.compile(r"\bmy name is ([A-Z][a-z]+)\b", re.IGNORECASE),
    re.compile(r"\bi am ([A-Z][a-z]+)\b", re.IGNORECASE),
]


def _session_root(agent_id: str, data_dir: Optional[Path] = None) -> Path:
    root = (data_dir or get_data_dir()) / agent_id / "sessions"
    return root


def _memory_file(workspace_dir: Path) -> Path:
    return workspace_dir.expanduser().resolve() / "MEMORY.md"


def extract_user_facts(text: str) -> list[tuple[str, str, float]]:
    """Extract a small set of durable user facts from direct statements."""
    facts: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for pattern, key, value, confidence in _PREFERENCE_PATTERNS:
        if pattern.search(text):
            seen.add(key)
            facts.append((key, value, confidence))
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = match.group(1).strip()
        key = "user.name"
        if key not in seen:
            facts.append((key, f"The user's name is {name}.", 0.8))
            seen.add(key)
        break
    return facts


def _read_turns(transcript_path: Path) -> list[dict]:
    turns: list[dict] = []
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return turns
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = rec.get("role")
        content = _clean_memory_text(rec.get("content") or "")
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    return turns


def _clean_memory_text(text: str) -> str:
    text = _TOOL_XML_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _FOOTER_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _display_line(text: str, max_chars: int = 220) -> str:
    cleaned = _clean_memory_text(text)
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


def _is_memory_worthy(text: str) -> bool:
    cleaned = _display_line(text, max_chars=500)
    if len(cleaned) < 24:
        return False
    lowered = cleaned.lower()
    if cleaned.endswith("?"):
        return False
    if any(fragment in lowered for fragment in _LOW_SIGNAL_FRAGMENTS):
        return False
    return True


def backfill_transcript_learning(
    memory: MemorySystem,
    transcript_path: Path,
    session_id: str,
    block_size: int = 8,
) -> tuple[int, int]:
    """
    Distill new transcript turns and capture direct user preference facts.

    Returns ``(facts_added, distillations_added)``.
    """
    turns = _read_turns(transcript_path)
    if not turns:
        return 0, 0

    facts_added = 0
    for turn in turns:
        if turn["role"] != "user":
            continue
        for key, value, confidence in extract_user_facts(turn["content"]):
            memory.store_fact(key, value, confidence=confidence, source_session=session_id)
            facts_added += 1

    last_turn_end = memory.latest_distilled_turn_end(session_id)
    start = last_turn_end + 1
    distillations_added = 0

    distiller = Distiller()
    while len(turns) - start >= block_size:
        block = turns[start:start + block_size]
        result = distiller.distill(session_id=session_id, turns=block, turn_start=start)
        if result is None:
            break
        memory.store_distillation(result)
        distillations_added += 1
        start += block_size

    return facts_added, distillations_added


def build_auto_memory_section(memory: MemorySystem) -> str:
    """Render the auto-maintained section from structured memory."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    facts = memory.load_top_facts(n=12)
    distillations = memory.load_recent_distillations(limit=6)
    corrections = memory.load_recent_corrections(limit=5)

    lines = [
        "## Auto-Maintained Memory",
        f"_Last refreshed: {now}_",
        "",
    ]

    if facts:
        lines.extend(["### Durable Facts", ""])
        for fact in facts:
            lines.append(f"- {fact['value']}")
        lines.append("")

    if distillations:
        lines.extend(["### Recent Learned Summaries", ""])
        for item in distillations:
            summary = _display_line(item["summary"])
            if not summary or not _is_memory_worthy(summary):
                continue
            decision = next(
                (d for d in item["decisions"] if _is_memory_worthy(d)),
                "",
            )
            fact = next((f for f in item["key_facts"] if _is_memory_worthy(f)), "")
            lines.append(f"- {summary}")
            if decision:
                lines.append(f"  Decision: {_display_line(decision)}")
            elif fact:
                lines.append(f"  Key fact: {_display_line(fact)}")
        lines.append("")

    if corrections:
        lines.extend(["### Repeated Corrections", ""])
        for item in corrections:
            wrong = _display_line(item["wrong_approach"])
            right = _display_line(item["correct_approach"])
            if not wrong or not right:
                continue
            if len(wrong) < 20 or len(right) < 20:
                continue
            lines.append(f"- Avoid: {wrong}")
            lines.append(f"  Prefer: {right}")
        lines.append("")

    if len(lines) == 3:
        lines.extend(
            [
                "### Auto-Maintained Memory",
                "",
                "- No durable learnings have been promoted yet.",
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def merge_memory_markdown(existing_text: str, auto_section: str) -> str:
    """Preserve manual content and replace only the auto-managed section."""
    block = f"{AUTO_START}\n{auto_section.rstrip()}\n{AUTO_END}\n"
    if AUTO_START in existing_text and AUTO_END in existing_text:
        prefix, remainder = existing_text.split(AUTO_START, 1)
        _, suffix = remainder.split(AUTO_END, 1)
        merged = prefix.rstrip() + "\n\n" + block + suffix.lstrip()
        return merged.strip() + "\n"

    base = existing_text.rstrip()
    if base:
        return f"{base}\n\n{block}"
    return block


def sync_memory_markdown(memory: MemorySystem, workspace_dir: Path) -> Path:
    """Refresh the auto-managed portion of MEMORY.md and reindex it."""
    workspace_dir = workspace_dir.expanduser().resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    memory_path = _memory_file(workspace_dir)
    existing_text = ""
    if memory_path.exists():
        existing_text = memory_path.read_text(encoding="utf-8", errors="replace")
    merged = merge_memory_markdown(existing_text, build_auto_memory_section(memory))
    if merged != existing_text:
        memory_path.write_text(merged, encoding="utf-8")
        source_key = str(memory_path)
        memory.delete_by_source(source_key)
        memory.store(merged, source_file=source_key)
        logger.info("Synced MEMORY.md at %s", memory_path)
    return memory_path


class MemoryUpkeepService:
    """Periodic task that promotes learned context into MEMORY.md."""

    def __init__(self, config: CatoConfig, interval_sec: int = DEFAULT_SYNC_INTERVAL_SEC) -> None:
        self._cfg = config
        self._interval_sec = max(120, int(interval_sec))

    def run_once_sync(self) -> dict[str, int | str]:
        from .memory import MemorySystem

        agent_id = getattr(self._cfg, "agent_name", "cato")
        workspace_dir = self._cfg.workspace_path()
        data_dir = get_data_dir()
        sessions_dir = _session_root(agent_id, data_dir=data_dir)
        memory = MemorySystem(agent_id=agent_id, memory_dir=data_dir / "memory")

        facts_added = 0
        distillations_added = 0
        if sessions_dir.exists():
            for transcript_path in sorted(sessions_dir.glob("*.jsonl")):
                session_id = transcript_path.stem.replace("_", ":")
                facts, distills = backfill_transcript_learning(
                    memory=memory,
                    transcript_path=transcript_path,
                    session_id=session_id,
                )
                facts_added += facts
                distillations_added += distills

        memory_path = sync_memory_markdown(memory, workspace_dir)
        return {
            "facts_added": facts_added,
            "distillations_added": distillations_added,
            "memory_path": str(memory_path),
        }

    async def run_forever(self) -> None:
        while True:
            try:
                await time_async_sleep(self._interval_sec)
                await self.run_once()
            except Exception as exc:
                logger.error("Memory upkeep failed: %s", exc, exc_info=True)

    async def run_once(self) -> dict[str, int | str]:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run_once_sync)


async def time_async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
