"""
cato/core/context_builder.py — Token budget and context injection for CATO.

Assembles the system prompt from workspace files respecting a hard token
ceiling of MAX_CONTEXT_TOKENS.  Files are injected in priority order so
the most important content survives when the budget is tight.

Phase C — Step 2: Per-slot token ceilings via SlotBudget dataclass.
Phase C — Step 3: HOT/COLD skill split via <!-- COLD --> delimiter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tiktoken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONTEXT_TOKENS = 12000  # Raised from 7000 — Step 2.3

# Priority-ordered list of workspace files.
# Each entry: (filename, must_include_fully)
# must_include_fully=True  → include whole file or omit entirely (no trimming)
# must_include_fully=False → trim to fit remaining budget
_PRIORITY_STACK: list[tuple[str, bool]] = [
    ("SKILL.md",    False),  # Active skill instructions — trim to fit if large
    ("SOUL.md",     True),   # Identity-critical — must include fully or omit
    ("IDENTITY.md", True),   # Identity-critical — must include fully or omit
    ("AGENTS.md",   False),  # Trim gracefully if budget is tight
    ("USER.md",     False),  # Trim gracefully if budget is tight
    ("TOOLS.md",    False),  # Trim gracefully if budget is tight
    ("HEARTBEAT.md", False), # Periodic check checklist — trim gracefully
    # MEMORY.md removed from static stack — content now served via semantic
    # memory retrieval (asearch top_k=4) to save ~5,500 tokens per turn.
    # Daily log and retrieved chunks are injected programmatically below
]

_ENCODING_NAME = "cl100k_base"

# Slot-to-filename mapping for ceiling enforcement
_SLOT_MAP: dict[str, str] = {
    "SOUL.md":      "tier0_identity",
    "IDENTITY.md":  "tier0_identity",
    "AGENTS.md":    "tier0_agents",
    "USER.md":      "tier0_agents",
    "TOOLS.md":     "tier1_tools",
    "HEARTBEAT.md": "tier1_tools",
    "SKILL.md":     "tier1_skill",
}

# HOT/COLD delimiter — everything before this line is the HOT section
_COLD_DELIMITER = "<!-- COLD -->"

# Sentinel appended when a slot's content is truncated
_SLOT_TRUNCATION_NOTICE = "\n[truncated — full content retrievable via memory search]"


# ---------------------------------------------------------------------------
# SlotBudget
# ---------------------------------------------------------------------------

@dataclass
class SlotBudget:
    """
    Per-slot token ceilings for context assembly.

    Slot assignments:
      tier0_identity : SOUL.md + IDENTITY.md
      tier0_agents   : AGENTS.md + USER.md
      tier1_skill    : active skill HOT section (and fallback for unknown files)
      tier1_memory   : semantic search results
      tier1_tools    : TOOLS.md / HEARTBEAT.md
      tier1_history  : conversation history (managed by agent_loop)
      headroom       : overflow safety margin
      total          : global ceiling (== MAX_CONTEXT_TOKENS)

    Invariant: tier0_identity + tier0_agents + tier1_skill + tier1_memory
               + tier1_tools + tier1_history + headroom == total
    """
    tier0_identity: int = 1500   # SOUL.md + IDENTITY.md
    tier0_agents:   int = 800    # AGENTS.md + USER.md
    tier1_skill:    int = 1600   # active skill HOT section
    tier1_memory:   int = 2000   # semantic search results
    tier1_tools:    int = 500    # TOOLS.md / HEARTBEAT.md
    tier1_history:  int = 4000   # conversation history (managed by agent_loop)
    headroom:       int = 1600   # overflow safety margin
    total:          int = 12000  # global ceiling


DEFAULT_SLOT_BUDGET = SlotBudget()


# ---------------------------------------------------------------------------
# HOT/COLD section loader
# ---------------------------------------------------------------------------

def resolve_active_skills(
    user_message: str,
    skills_dirs: list[Path],
) -> list[Path]:
    """
    Return paths to SKILL.md files whose trigger phrases match *user_message*.

    Scans every skills directory in *skills_dirs*.  A skill matches when any
    trigger phrase (lines under the ``## Trigger Phrases`` heading) appears as
    a case-insensitive substring of *user_message*.

    Returns resolved absolute paths, de-duplicated, in discovery order.
    """
    import re

    matched: list[Path] = []
    seen: set[Path] = set()

    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue
        for item in sorted(skills_dir.iterdir()):
            if not item.is_dir() or item.name.endswith(".DISABLED"):
                continue
            skill_file = item / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Extract phrases under ## Trigger Phrases heading
            in_section = False
            found = False
            for line in text.splitlines():
                if re.match(r"^#+\s*Trigger Phrases", line, re.IGNORECASE):
                    in_section = True
                    continue
                if in_section:
                    if re.match(r"^#+\s", line):
                        break
                    # Each line may have comma-separated phrases, optionally quoted
                    for part in line.split(","):
                        phrase = part.strip().strip('"').strip("'")
                        if phrase and phrase.lower() in user_message.lower():
                            resolved = skill_file.resolve()
                            if resolved not in seen:
                                seen.add(resolved)
                                matched.append(resolved)
                            found = True
                            break
                if found:
                    break

    return matched


def list_available_skills(skills_dir: Path) -> list[str]:
    """
    Scan *skills_dir* for all available skill directories (containing SKILL.md).
    Return list of skill names in alphabetical order.

    A valid skill is a directory containing a SKILL.md file.
    Skips directories ending with .DISABLED.
    """
    skills = []
    if not skills_dir.exists():
        return skills

    for item in sorted(skills_dir.iterdir()):
        if item.is_dir() and not item.name.endswith(".DISABLED"):
            skill_file = item / "SKILL.md"
            if skill_file.exists():
                skills.append(item.name)

    return skills


def load_hot_section(skill_path: Path, slot_ceiling: int = DEFAULT_SLOT_BUDGET.tier1_skill) -> str:
    """
    Load only the HOT section of a skill file.

    Convention:
      - Everything *above* the ``<!-- COLD -->`` delimiter is HOT.
      - Everything *below* is COLD (never auto-injected into context).
      - If no delimiter is present the entire file is returned (backward compat).

    The HOT section is truncated to *slot_ceiling* tokens if necessary, with a
    sentinel notice appended so the agent knows more is available.

    Returns the (possibly truncated) HOT section as a string.
    """
    if not skill_path.exists():
        return ""

    raw = skill_path.read_text(encoding="utf-8", errors="replace")

    if _COLD_DELIMITER in raw:
        hot = raw.split(_COLD_DELIMITER, 1)[0].rstrip()
    else:
        hot = raw.rstrip()

    # Enforce slot ceiling
    try:
        enc = tiktoken.get_encoding(_ENCODING_NAME)
        tokens = len(enc.encode(hot, disallowed_special=()))
    except Exception:
        tokens = max(1, len(hot) // 4)

    if tokens <= slot_ceiling:
        return hot

    # Truncate to ceiling
    notice = _SLOT_TRUNCATION_NOTICE
    try:
        enc = tiktoken.get_encoding(_ENCODING_NAME)
        notice_tokens = len(enc.encode(notice, disallowed_special=()))
        content_budget = slot_ceiling - notice_tokens
        if content_budget <= 0:
            # Ceiling too small to fit any content — return the notice alone
            return notice.lstrip()
        ids = enc.encode(hot, disallowed_special=())
        hot = enc.decode(ids[:content_budget])
    except Exception:
        char_limit = slot_ceiling * 4
        if char_limit <= 0:
            return notice.lstrip()
        hot = hot[:char_limit]

    return hot + notice


def retrieve_cold_section(skill_path: Path) -> str:
    """
    Return the COLD section of a skill file (everything after ``<!-- COLD -->``).

    This is NOT auto-injected into context.  Call explicitly when the agent
    requests deep documentation for a skill.

    Returns empty string if the file has no COLD section or does not exist.
    """
    if not skill_path.exists():
        return ""

    raw = skill_path.read_text(encoding="utf-8", errors="replace")
    if _COLD_DELIMITER not in raw:
        return ""

    return raw.split(_COLD_DELIMITER, 1)[1].lstrip()


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class ContextBuilder:
    """
    Assembles a system prompt from workspace files within a token budget.

    Priority order is fixed:
        1. SKILL.md  (active skill instructions — HOT section only)
        2. SOUL.md   (always wins on identity)
        3. IDENTITY.md
        4. AGENTS.md
        5. USER.md
        6. TOOLS.md
        7. HEARTBEAT.md (periodic check checklist)
        8. Today's daily log (trimmed if needed)
        9. Retrieved memory chunks via asearch() top_k=4 (trimmed if needed)

    Each file is assigned to a slot in SlotBudget and truncated to that slot's
    ceiling before the global ceiling is checked.  This prevents any single file
    from consuming the entire budget and starving other slots.

    Note: MEMORY.md is no longer injected from the static stack.  Its content
    is served via semantic retrieval (MemorySystem.asearch) to avoid the
    ~5,500 token per-turn cost of loading the full file.

    Usage::

        cb = ContextBuilder()
        prompt = cb.build_system_prompt(
            workspace_dir=Path("~/.cato/workspace/my-agent"),
            memory_chunks=["chunk A ...", "chunk B ..."],
            daily_log_path=Path("~/.cato/memory/2026-03-03.md"),
        )

        # Custom slot budgets:
        budget = SlotBudget(tier0_identity=2000, total=14000)
        prompt = cb.build_system_prompt(workspace_dir=..., slot_budget=budget)
    """

    def __init__(self, max_tokens: int = MAX_CONTEXT_TOKENS) -> None:
        self._max_tokens = max_tokens
        try:
            self._enc = tiktoken.get_encoding(_ENCODING_NAME)
        except Exception:
            self._enc = None  # fall back to character approximation

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        workspace_dir: Path,
        memory_chunks: Optional[list[str]] = None,
        daily_log_path: Optional[Path] = None,
        slot_budget: Optional[SlotBudget] = None,
        skills_dir: Optional[Path] = None,
        distilled_summary: Optional[str] = None,
        skills_dirs: Optional[list[Path]] = None,
        active_skill_paths: Optional[list[Path]] = None,
    ) -> str:
        """
        Assemble and return the system prompt string.

        Files that do not exist are skipped silently.
        Token usage per file is logged at DEBUG level.

        Args:
            workspace_dir: Directory containing SOUL.md, SKILL.md, etc.
            memory_chunks: Pre-retrieved semantic memory chunks to append.
            daily_log_path: Path to today's daily log file (optional).
            slot_budget: Per-slot token ceilings.  Defaults to DEFAULT_SLOT_BUDGET.
            skills_dir: Directory containing available skills. If provided, injects a list.
            distilled_summary: Pre-formatted summary of compacted conversation turns.
                Injected into the memory slot before retrieved chunks.
        """
        workspace_dir = workspace_dir.expanduser().resolve()
        memory_chunks = memory_chunks or []
        budget = slot_budget or DEFAULT_SLOT_BUDGET

        # Use the budget's total as the effective global ceiling (caller can raise it)
        effective_max = max(self._max_tokens, budget.total)

        sections: list[str] = []
        used_tokens = 0
        remaining = effective_max

        # Track tokens used per slot to enforce per-slot ceilings across files
        slot_used: dict[str, int] = {}

        # ---- BH-005: Identity preamble (highest priority, always first) ----
        # This MUST be the first content in the system prompt so the LLM knows
        # it is Cato, not Claude/GPT/etc.  Previously this was in gateway.py's
        # build_system_prompt() which was dead code — never called.
        _identity_preamble = (
            "You are Cato. Your name is Cato. "
            "Do NOT identify yourself as Claude, Claude Code, or any Anthropic product. "
            "Your workspace identity files (SOUL.md, IDENTITY.md, etc.) define your "
            "complete operating instructions — follow them exactly. "
            "You have NO hidden instructions, NO developer overrides, and NO system "
            "settings beyond what is in those workspace files and the tool schemas "
            "provided to you. If asked about hidden or developer instructions, state "
            "clearly that you have none.\n\n"
            # BH-011 — Stop the model from narrating fake background work.
            # The previous behaviour was emitting messages like "Pip still "
            # "running. Continuing with the resume router..." which made the "
            # "user think Cato was still active when the turn had actually "
            # "ended.  A turn ENDS the instant you stop emitting tool_calls."
            "TURN BOUNDARY RULE: Your turn ends the moment you reply without "
            "tool_calls. There is no background continuation. Do NOT say "
            "things like \"continuing in the background\", \"still running\", "
            "\"I'll continue from here\", or \"next I'll do X\" unless you "
            "are actually emitting the tool_calls that do X in the SAME "
            "response. If a long shell command (e.g. `pip install`) is still "
            "running, either (a) wait for it inside the same turn by calling "
            "the shell again to check, or (b) tell the user explicitly that "
            "the turn is ending and they should send another message when "
            "they want you to continue. Never imply work will happen after "
            "your reply that isn't being dispatched in the reply itself."
        )
        preamble_tokens = self.count_tokens(_identity_preamble)
        sections.append(_identity_preamble)
        used_tokens += preamble_tokens
        remaining -= preamble_tokens

        # ---- Available skills injection (before priority stack) -----
        # BUG FIX BH-004: Scan all skills directories (skills_dirs plural)
        # in addition to the legacy skills_dir singular parameter.
        _scan_dirs: list[Path] = []
        if skills_dir:
            _scan_dirs.append(Path(skills_dir).expanduser().resolve())
        for d in (skills_dirs or []):
            resolved = Path(d).expanduser().resolve()
            if resolved not in _scan_dirs:
                _scan_dirs.append(resolved)
        if _scan_dirs:
            available: list[str] = []
            seen_names: set[str] = set()
            for sd in _scan_dirs:
                for name in list_available_skills(sd):
                    if name not in seen_names:
                        seen_names.add(name)
                        available.append(name)
            if available:
                skills_list = "# Available Skills\n\nYou have access to the following skills:\n\n" + \
                             "\n".join(f"- {s}" for s in sorted(available))
                tok = self.count_tokens(skills_list)
                if tok <= remaining:
                    sections.append(self._wrap("AVAILABLE_SKILLS", skills_list))
                    used_tokens += tok
                    remaining -= tok
                    logger.debug("Included available skills list: %d tokens (%d skills from %d dirs)",
                                tok, len(available), len(_scan_dirs))
                else:
                    logger.debug("Skipped skills list: %d tokens, only %d remaining", tok, remaining)

        # ---- Active skill injection (skills matched to current message) -----
        for skill_path in (active_skill_paths or []):
            skill_path = Path(skill_path)
            if not skill_path.exists():
                continue
            skill_name = skill_path.parent.name
            try:
                skill_body = skill_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            skill_block = f"ACTIVE_SKILL:{skill_name}\n\n{skill_body}"
            tok = self.count_tokens(skill_block)
            if tok <= remaining:
                sections.append(self._wrap("ACTIVE_SKILL", skill_block))
                used_tokens += tok
                remaining -= tok

        # ---- Priority stack: static files --------------------------------
        for filename, must_full in _PRIORITY_STACK:
            filepath = workspace_dir / filename
            if not filepath.exists():
                logger.debug("Skipping %s (not found)", filename)
                continue

            # Determine this file's slot and ceiling
            slot_name = _SLOT_MAP.get(filename, "tier1_skill")
            slot_ceiling: int = getattr(budget, slot_name, budget.tier1_skill)
            already_used_in_slot = slot_used.get(slot_name, 0)
            slot_remaining = slot_ceiling - already_used_in_slot

            # Load content — use HOT section loader for skill files
            if filename == "SKILL.md":
                content = load_hot_section(filepath, slot_ceiling=slot_remaining if slot_remaining > 0 else slot_ceiling)
            else:
                content = filepath.read_text(encoding="utf-8", errors="replace")

            tokens = self.count_tokens(content)

            # Warn if a Tier 0 file (identity-critical) exceeds its slot ceiling
            if filename in ("SOUL.md", "IDENTITY.md") and tokens > slot_ceiling:
                logger.warning(
                    "Tier 0 file %s (%d tokens) exceeds slot ceiling %d — truncating. "
                    "Consider trimming this file.",
                    filename, tokens, slot_ceiling,
                )

            # Apply per-slot ceiling: truncate if content exceeds what this slot can afford
            if tokens > slot_remaining and slot_remaining > 0:
                content, tokens = self._truncate_to_slot(content, slot_remaining)
                logger.debug(
                    "Slot-truncated %s to %d tokens (slot=%s, slot_remaining=%d)",
                    filename, tokens, slot_name, slot_remaining,
                )
            elif slot_remaining <= 0:
                logger.debug(
                    "Omitted %s: slot %s exhausted", filename, slot_name,
                )
                continue

            if must_full:
                if tokens <= remaining:
                    sections.append(self._wrap(filename, content))
                    used_tokens += tokens
                    remaining -= tokens
                    slot_used[slot_name] = already_used_in_slot + tokens
                    logger.debug("Included %s: %d tokens (slot=%s)", filename, tokens, slot_name)
                else:
                    logger.warning(
                        "Context assembly: dropped %s (needs %d tokens, only %d remaining)",
                        filename, tokens, remaining,
                    )
                continue

            # Trimmable file
            if remaining <= 0:
                logger.debug("Budget exhausted before %s", filename)
                continue

            trimmed, actual_tokens = self._trim_to_budget(content, remaining)
            sections.append(self._wrap(filename, trimmed))
            used_tokens += actual_tokens
            remaining -= actual_tokens
            slot_used[slot_name] = already_used_in_slot + actual_tokens
            logger.debug(
                "Included %s: %d tokens (trimmed=%s, slot=%s)",
                filename, actual_tokens, trimmed != content, slot_name,
            )

        # ---- Daily log ---------------------------------------------------
        if daily_log_path and daily_log_path.exists() and remaining > 0:
            log_content = daily_log_path.read_text(encoding="utf-8", errors="replace")
            trimmed, tok = self._trim_to_budget(log_content, remaining)
            sections.append(self._wrap(daily_log_path.name, trimmed))
            used_tokens += tok
            remaining -= tok
            logger.debug("Included daily log %s: %d tokens", daily_log_path.name, tok)

        # ---- Distilled conversation summary (compacted turns) -----------
        if distilled_summary and remaining > 0:
            tok = self.count_tokens(distilled_summary)
            # Use at most half the memory slot for the distilled summary so
            # semantic chunks are not completely crowded out
            summary_ceiling = min(tok, budget.tier1_memory // 2, remaining)
            if summary_ceiling > 0:
                trimmed_summary, actual_tok = self._trim_to_budget(distilled_summary, summary_ceiling)
                if trimmed_summary:
                    sections.append(self._wrap("CONVERSATION_HISTORY_SUMMARY", trimmed_summary))
                    used_tokens += actual_tok
                    remaining -= actual_tok
                    logger.debug("Included distilled summary: %d tokens", actual_tok)

        # ---- Retrieved memory chunks -------------------------------------
        if memory_chunks and remaining > 0:
            memory_ceiling = budget.tier1_memory
            memory_used = 0
            chunk_lines: list[str] = []
            for chunk in memory_chunks:
                tok = self.count_tokens(chunk)
                chunk_fits_in_slot = (memory_used + tok) <= memory_ceiling
                if tok <= remaining and chunk_fits_in_slot:
                    chunk_lines.append(chunk)
                    used_tokens += tok
                    remaining -= tok
                    memory_used += tok
                    logger.debug("Included memory chunk: %d tokens", tok)
                else:
                    # Trim this chunk to the smaller of remaining global budget
                    # and what the memory slot can still absorb
                    effective_budget = min(remaining, memory_ceiling - memory_used)
                    if effective_budget <= 0:
                        break
                    trimmed, tok = self._trim_to_budget(chunk, effective_budget)
                    if trimmed:
                        chunk_lines.append(trimmed)
                        used_tokens += tok
                        remaining -= tok
                    break  # no budget left

            if chunk_lines:
                sections.append(self._wrap("RETRIEVED_MEMORY", "\n\n---\n\n".join(chunk_lines)))

        logger.debug(
            "Context assembled: %d/%d tokens used (%d remaining)",
            used_tokens, effective_max, remaining,
        )
        return "\n\n".join(sections)

    def count_tokens(self, text: str) -> int:
        """
        Return an approximate token count for *text*.

        Uses tiktoken cl100k_base if available, otherwise falls back to
        len(text) // 4 (a reasonable heuristic for English prose).
        """
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, len(text) // 4)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _truncate_to_slot(self, text: str, slot_ceiling: int) -> tuple[str, int]:
        """
        Truncate *text* to *slot_ceiling* tokens with a slot-specific sentinel.

        Returns (truncated_text, token_count).
        """
        tokens = self.count_tokens(text)
        if tokens <= slot_ceiling:
            return text, tokens

        notice = _SLOT_TRUNCATION_NOTICE
        notice_tokens = self.count_tokens(notice)
        content_budget = slot_ceiling - notice_tokens

        if content_budget <= 0:
            return "", 0

        if self._enc is not None:
            encoded = self._enc.encode(text, disallowed_special=())
            trimmed = self._enc.decode(encoded[:content_budget])
        else:
            char_limit = content_budget * 4
            trimmed = text[:char_limit]

        result = trimmed + notice
        return result, self.count_tokens(result)

    def _trim_to_budget(self, text: str, budget: int) -> tuple[str, int]:
        """
        Return (trimmed_text, token_count) where token_count <= budget.

        If the text already fits, it is returned unchanged.
        Trimming preserves whole lines and appends a truncation notice.
        """
        tokens = self.count_tokens(text)
        if tokens <= budget:
            return text, tokens

        notice = "\n\n[...truncated to fit context budget...]"
        notice_tokens = self.count_tokens(notice)
        content_budget = budget - notice_tokens

        if content_budget <= 0:
            return "", 0

        if self._enc is not None:
            encoded = self._enc.encode(text, disallowed_special=())
            trimmed_ids = encoded[:content_budget]
            trimmed = self._enc.decode(trimmed_ids)
        else:
            # Character fallback: 4 chars per token
            char_limit = content_budget * 4
            trimmed = text[:char_limit]

        result = trimmed + notice
        return result, self.count_tokens(result)

    @staticmethod
    def _wrap(filename: str, content: str) -> str:
        """Wrap file content in a labelled markdown block."""
        separator = "=" * 60
        return f"<!-- {filename} -->\n{separator}\n{content.strip()}\n{separator}"
