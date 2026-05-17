from __future__ import annotations

import asyncio
import json

from cato.agent_loop import _parse_tool_calls_text
from cato.core.context_builder import ContextBuilder, resolve_active_skills
from cato.tools.file import FileTool


def test_resolve_active_skills_matches_named_skill_and_trigger(tmp_path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "genesis-pipeline"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: "Genesis Pipeline"
---
# Genesis Pipeline

## Trigger Phrases
"run genesis", "start pipeline"

## Rules
Use python.execute.
""",
        encoding="utf-8",
    )

    active = resolve_active_skills("Please run genesis for this idea", [skills_root])
    assert active == [(skill_dir / "SKILL.md").resolve()]


def test_build_system_prompt_includes_active_skill(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "coding-agent"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        """# Coding Agent

## Trigger Phrases
"use coding agent"

## Rules
Use shell.exec.
""",
        encoding="utf-8",
    )

    prompt = ContextBuilder().build_system_prompt(
        workspace_dir=workspace,
        skills_dirs=[skills_root],
        active_skill_paths=[skill_path.resolve()],
    )

    assert "ACTIVE_SKILL:coding-agent" in prompt
    assert "Use shell.exec." in prompt


def test_parse_tool_calls_text_handles_legacy_invoke_shell_and_browser():
    text = """
Before
<minimax:tool_call>
<invoke name="executor">
<parameter name="command">Get-ChildItem</parameter>
<parameter name="cwd">C:\\Users\\Administrator\\Desktop\\Cato</parameter>
</invoke>
</minimax:tool_call>
<invoke name="browser">
<parameter name="action">navigate</parameter>
<parameter name="url">https://example.com</parameter>
</invoke>
After
"""
    calls = _parse_tool_calls_text(text)

    assert len(calls) == 2
    assert calls[0].name == "shell.exec"
    assert calls[0].args["command"] == "Get-ChildItem"
    assert calls[1].name == "browser"
    assert calls[1].args["action"] == "navigate"


def test_file_tool_can_read_from_trusted_non_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    target = skills / "skill.txt"
    target.write_text("skill-body", encoding="utf-8")

    tool = FileTool()
    monkeypatch.setattr(
        tool,
        "_trusted_roots",
        lambda _agent_id: {"workspace": workspace, "skills": skills},
    )

    result = asyncio.run(tool._run(action="read", path="skill.txt", root="skills"))

    assert result["success"] is True
    assert result["content"] == "skill-body"


def test_file_tool_lists_trusted_roots(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()

    tool = FileTool()
    monkeypatch.setattr(
        tool,
        "_trusted_roots",
        lambda _agent_id: {"workspace": workspace, "skills": skills},
    )

    result = asyncio.run(tool._run(action="roots", path="", root="workspace"))

    assert result["success"] is True
    payload = json.loads(result["content"])
    assert payload["workspace"] == str(workspace)
    assert payload["skills"] == str(skills)
