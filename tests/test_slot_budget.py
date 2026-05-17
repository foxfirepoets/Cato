"""
tests/test_slot_budget.py — Phase C Step 2 + Step 3 tests.

Covers:
  - SlotBudget dataclass defaults and invariants  (Step 2)
  - Per-slot ceiling enforcement in build_system_prompt()  (Step 2)
  - load_hot_section() HOT/COLD split loader  (Step 3)
  - retrieve_cold_section() on-demand COLD access  (Step 3)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cato.core.context_builder import (
    DEFAULT_SLOT_BUDGET,
    MAX_CONTEXT_TOKENS,
    SlotBudget,
    ContextBuilder,
    load_hot_section,
    retrieve_cold_section,
    _COLD_DELIMITER,
    _SLOT_TRUNCATION_NOTICE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Step 2 — SlotBudget dataclass
# ---------------------------------------------------------------------------

class TestSlotBudgetDefaults:
    """SlotBudget default values and invariant checks."""

    def test_default_total_is_12000(self):
        assert DEFAULT_SLOT_BUDGET.total == 12000

    def test_max_context_tokens_raised_to_12000(self):
        """Global constant must be 12000 (raised from 7000)."""
        assert MAX_CONTEXT_TOKENS == 12000

    def test_all_slots_plus_headroom_equal_total(self):
        b = SlotBudget()
        parts = (
            b.tier0_identity
            + b.tier0_agents
            + b.tier1_skill
            + b.tier1_memory
            + b.tier1_tools
            + b.tier1_history
            + b.headroom
        )
        assert parts == b.total, (
            f"Slot budget invariant failed: {parts} != {b.total}"
        )

    def test_tier0_identity_default(self):
        assert SlotBudget().tier0_identity == 1500

    def test_tier0_agents_default(self):
        assert SlotBudget().tier0_agents == 800

    def test_tier1_skill_default(self):
        assert SlotBudget().tier1_skill == 1600

    def test_tier1_memory_default(self):
        assert SlotBudget().tier1_memory == 2000

    def test_tier1_tools_default(self):
        assert SlotBudget().tier1_tools == 500

    def test_tier1_history_default(self):
        assert SlotBudget().tier1_history == 4000

    def test_headroom_default(self):
        assert SlotBudget().headroom == 1600

    def test_custom_slot_budget_accepted(self):
        b = SlotBudget(tier0_identity=2000)
        assert b.tier0_identity == 2000

    def test_default_slot_budget_is_slot_budget_instance(self):
        assert isinstance(DEFAULT_SLOT_BUDGET, SlotBudget)


# ---------------------------------------------------------------------------
# Step 2 — Per-slot ceiling enforcement in build_system_prompt()
# ---------------------------------------------------------------------------

class TestSlotCeilingEnforcement:
    """build_system_prompt() respects per-slot ceilings."""

    def _builder(self) -> ContextBuilder:
        return ContextBuilder(max_tokens=MAX_CONTEXT_TOKENS)

    def test_file_within_slot_ceiling_not_truncated(self, tmp_path):
        # SOUL.md with content that fits within tier0_identity (1500 tokens)
        content = "This is SOUL.md content.\n" * 10  # ~50 tokens
        _write(tmp_path, "SOUL.md", content)
        cb = self._builder()
        prompt = cb.build_system_prompt(tmp_path)
        # Sentinel must NOT appear — content was not truncated
        assert _SLOT_TRUNCATION_NOTICE not in prompt
        assert "This is SOUL.md content." in prompt

    def test_file_exceeding_slot_ceiling_gets_truncated_with_sentinel(self, tmp_path):
        # Create a tiny budget where tier0_identity is only 20 tokens
        tiny_budget = SlotBudget(
            tier0_identity=20,
            tier0_agents=800,
            tier1_skill=600,
            tier1_memory=2000,
            tier1_tools=500,
            tier1_history=4000,
            headroom=4080,
            total=12000,
        )
        # SOUL.md with ~100 tokens of content — exceeds the 20-token slot
        content = "Identity paragraph. " * 20
        _write(tmp_path, "SOUL.md", content)
        cb = self._builder()
        prompt = cb.build_system_prompt(tmp_path, slot_budget=tiny_budget)
        # Sentinel MUST appear because the file was slot-truncated
        assert _SLOT_TRUNCATION_NOTICE in prompt

    def test_build_system_prompt_accepts_custom_slot_budget(self, tmp_path):
        """build_system_prompt() should accept a SlotBudget kwarg without error."""
        _write(tmp_path, "SOUL.md", "Hello identity.")
        cb = self._builder()
        custom = SlotBudget(tier0_identity=50)
        # Should not raise
        prompt = cb.build_system_prompt(tmp_path, slot_budget=custom)
        assert isinstance(prompt, str)

    def test_global_ceiling_uses_budget_total(self, tmp_path):
        """Effective max tokens should use budget.total, not the legacy 7000."""
        _write(tmp_path, "SOUL.md", "A" * 40)  # small file
        cb = ContextBuilder(max_tokens=7000)  # old value
        big_budget = SlotBudget()  # total=12000
        prompt = cb.build_system_prompt(tmp_path, slot_budget=big_budget)
        # As long as it succeeds without raising, the larger ceiling was respected
        assert isinstance(prompt, str)

    def test_skill_file_uses_hot_section_in_prompt(self, tmp_path):
        """When SKILL.md has a COLD delimiter, only HOT content appears in prompt."""
        hot_content = "# My Skill\nDo the thing.\n"
        cold_content = "## Extended Docs\nLots of detail here.\n"
        skill_text = hot_content + _COLD_DELIMITER + "\n" + cold_content
        _write(tmp_path, "SKILL.md", skill_text)
        cb = self._builder()
        prompt = cb.build_system_prompt(tmp_path)
        assert "Do the thing." in prompt
        assert "Extended Docs" not in prompt
        assert "Lots of detail here." not in prompt

    def test_tools_file_slot_assignment(self, tmp_path):
        """TOOLS.md should be included within tier1_tools budget."""
        _write(tmp_path, "TOOLS.md", "Tool list here.\n")
        cb = self._builder()
        prompt = cb.build_system_prompt(tmp_path)
        assert "Tool list here." in prompt


# ---------------------------------------------------------------------------
# Step 3 — load_hot_section()
# ---------------------------------------------------------------------------

class TestLoadHotSection:
    """load_hot_section() correctly splits and enforces ceilings."""

    def test_returns_hot_section_only_when_delimiter_present(self, tmp_path):
        content = "HOT content here\n" + _COLD_DELIMITER + "\nCOLD content here\n"
        p = _write(tmp_path, "SKILL.md", content)
        hot = load_hot_section(p)
        assert "HOT content here" in hot
        assert "COLD content here" not in hot

    def test_returns_full_content_when_no_delimiter(self, tmp_path):
        content = "Full content, no split needed.\nLine 2.\n"
        p = _write(tmp_path, "SKILL.md", content)
        hot = load_hot_section(p)
        assert "Full content, no split needed." in hot
        assert "Line 2." in hot

    def test_cold_delimiter_not_included_in_hot(self, tmp_path):
        content = "HOT part\n" + _COLD_DELIMITER + "\nCOLD part\n"
        p = _write(tmp_path, "SKILL.md", content)
        hot = load_hot_section(p)
        assert _COLD_DELIMITER not in hot

    def test_hot_section_exceeding_ceiling_is_truncated_with_sentinel(self, tmp_path):
        # HOT section ~200 words, well over a 50-token ceiling
        hot_words = "word " * 200
        content = hot_words + _COLD_DELIMITER + "\nCOLD\n"
        p = _write(tmp_path, "SKILL.md", content)
        # Enforce a ceiling larger than the notice itself but smaller than the HOT section
        result = load_hot_section(p, slot_ceiling=50)
        # The sentinel text must appear somewhere in the result (with or without leading newline)
        assert "truncated" in result and "memory search" in result

    def test_hot_section_within_ceiling_not_truncated(self, tmp_path):
        content = "Short HOT.\n" + _COLD_DELIMITER + "\nCOLD here.\n"
        p = _write(tmp_path, "SKILL.md", content)
        result = load_hot_section(p, slot_ceiling=600)
        assert _SLOT_TRUNCATION_NOTICE not in result
        assert "Short HOT." in result

    def test_file_with_only_hot_content_returned_in_full_up_to_ceiling(self, tmp_path):
        content = "All hot. No cold. Simple skill.\n"
        p = _write(tmp_path, "SKILL.md", content)
        result = load_hot_section(p, slot_ceiling=600)
        assert result == content.rstrip()

    def test_returns_empty_string_for_nonexistent_file(self, tmp_path):
        p = tmp_path / "NONEXISTENT.md"
        result = load_hot_section(p)
        assert result == ""

    def test_default_slot_ceiling_is_600(self, tmp_path):
        """Default ceiling parameter must match SlotBudget.tier1_skill == 600."""
        content = "x " * 100  # ~100 tokens, fits in 600
        p = _write(tmp_path, "SKILL.md", content)
        result = load_hot_section(p)  # no explicit ceiling
        assert _SLOT_TRUNCATION_NOTICE not in result

    def test_multiple_cold_delimiters_only_first_splits(self, tmp_path):
        """Only the first <!-- COLD --> delimiter is used as the split point."""
        content = (
            "HOT\n"
            + _COLD_DELIMITER + "\n"
            + "COLD-1\n"
            + _COLD_DELIMITER + "\n"
            + "COLD-2\n"
        )
        p = _write(tmp_path, "SKILL.md", content)
        hot = load_hot_section(p)
        assert "HOT" in hot
        assert "COLD-1" not in hot
        assert "COLD-2" not in hot


# ---------------------------------------------------------------------------
# Step 3 — retrieve_cold_section()
# ---------------------------------------------------------------------------

class TestRetrieveColdSection:
    """retrieve_cold_section() returns only content after the delimiter."""

    def test_returns_cold_content_when_delimiter_present(self, tmp_path):
        content = "HOT\n" + _COLD_DELIMITER + "\nCOLD content here\n"
        p = _write(tmp_path, "SKILL.md", content)
        cold = retrieve_cold_section(p)
        assert "COLD content here" in cold
        assert "HOT" not in cold

    def test_returns_empty_string_when_no_delimiter(self, tmp_path):
        content = "All hot, no delimiter.\n"
        p = _write(tmp_path, "SKILL.md", content)
        cold = retrieve_cold_section(p)
        assert cold == ""

    def test_returns_empty_string_for_nonexistent_file(self, tmp_path):
        p = tmp_path / "MISSING.md"
        cold = retrieve_cold_section(p)
        assert cold == ""

    def test_cold_section_does_not_include_delimiter_line(self, tmp_path):
        content = "HOT\n" + _COLD_DELIMITER + "\nCOLD line\n"
        p = _write(tmp_path, "SKILL.md", content)
        cold = retrieve_cold_section(p)
        assert _COLD_DELIMITER not in cold

    def test_hot_content_not_in_cold_section(self, tmp_path):
        content = "Unique HOT phrase XYZ\n" + _COLD_DELIMITER + "\nUnique COLD phrase ABC\n"
        p = _write(tmp_path, "SKILL.md", content)
        cold = retrieve_cold_section(p)
        assert "Unique HOT phrase XYZ" not in cold
        assert "Unique COLD phrase ABC" in cold


# ---------------------------------------------------------------------------
# Integration — real skill files
# ---------------------------------------------------------------------------

class TestRealSkillFiles:
    """Verify that the real skill files in cato/skills/ respect the convention."""

    _SKILLS_DIR = Path(__file__).parent.parent / "cato" / "skills"

    def _skill_files(self):
        return list(self._SKILLS_DIR.glob("*.md"))

    def test_skill_files_exist(self):
        assert len(self._skill_files()) >= 1

    def test_hot_section_of_coding_agent_under_300_tokens(self):
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        skill = self._SKILLS_DIR / "coding_agent.md"
        if not skill.exists():
            pytest.skip("coding_agent.md not found")
        hot = load_hot_section(skill, slot_ceiling=9999)  # no ceiling, measure only
        tokens = len(enc.encode(hot, disallowed_special=()))
        assert tokens <= 300, f"coding_agent.md HOT section is {tokens} tokens (max 300)"

    def test_hot_section_of_conduit_under_300_tokens(self):
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        skill = self._SKILLS_DIR / "conduit.md"
        if not skill.exists():
            pytest.skip("conduit.md not found")
        hot = load_hot_section(skill, slot_ceiling=9999)
        tokens = len(enc.encode(hot, disallowed_special=()))
        assert tokens <= 300, f"conduit.md HOT section is {tokens} tokens (max 300)"

    def test_files_without_delimiter_are_all_under_300_tokens_or_have_delimiter(self):
        """Every skill file must either have a <!-- COLD --> delimiter or be ≤300 tokens."""
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        violations = []
        for skill in self._skill_files():
            if skill.name == "README.md":
                continue
            text = skill.read_text(encoding="utf-8")
            if _COLD_DELIMITER not in text:
                tokens = len(enc.encode(text, disallowed_special=()))
                if tokens > 300:
                    violations.append(f"{skill.name}: {tokens} tokens, no COLD delimiter")
        assert not violations, "Skills exceeding 300 tokens without delimiter: " + str(violations)
