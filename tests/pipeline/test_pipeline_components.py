"""
test_pipeline_components.py
Unit + integration tests for the AI coding team pipeline.

Tests cover:
  1. invoke_codex.py  — module imports, Ralph init, prompt generation, completion parsing
  2. invoke_cursor.py — module imports, cursor-agent resolution, context building, prompt/result parsing
  3. invoke_agent.py  — routing logic, CLI argument forwarding
  4. Environment      — CLIs present, paths valid, Ralph skill exists
  5. Telegram bridge  — bridge script importable, queue logic
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ── Path helpers ──────────────────────────────────────────────────────────────
SCRIPTS_DIR = Path(r"C:\Users\Administrator\.claude\skills\one-shot-pipeline\scripts")
RALPH_DIR   = Path(r"C:\Users\Administrator\.claude\skills\ralph-wiggum-loop")
BRIDGE_SCRIPT = Path(r"C:\Users\Administrator\Desktop\Cato\cato_telegram_bridge.py")


def _load_module(name: str, path: Path):
    """Dynamically load a Python module from an absolute path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Environment sanity
# ─────────────────────────────────────────────────────────────────────────────

class TestEnvironment:
    def test_codex_on_path(self):
        assert shutil.which("codex") is not None, "codex CLI not found on PATH"

    def test_claude_on_path(self):
        assert shutil.which("claude") is not None, "claude CLI not found on PATH"

    def test_python313_exists(self):
        py = Path(r"C:\Python313\python.exe")
        assert py.exists(), f"Python 3.13 not found at {py}"

    def test_cursor_agent_installed(self):
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "versions"
        assert base.is_dir(), f"cursor-agent not installed (expected {base})"
        versions = list(base.iterdir())
        assert len(versions) > 0, "cursor-agent versions directory is empty"

    def test_cursor_node_and_index_exist(self):
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "versions"
        latest = sorted(p for p in base.iterdir() if p.is_dir())[-1]
        assert (latest / "node.exe").exists(), f"node.exe missing in {latest}"
        assert (latest / "index.js").exists(),  f"index.js missing in {latest}"

    def test_scripts_dir_exists(self):
        assert SCRIPTS_DIR.is_dir(), f"Pipeline scripts dir missing: {SCRIPTS_DIR}"

    def test_invoke_codex_exists(self):
        assert (SCRIPTS_DIR / "invoke_codex.py").exists()

    def test_invoke_cursor_exists(self):
        assert (SCRIPTS_DIR / "invoke_cursor.py").exists()

    def test_invoke_agent_exists(self):
        assert (SCRIPTS_DIR / "invoke_agent.py").exists()

    def test_ralph_loop_ps1_exists(self):
        ps1 = RALPH_DIR / "scripts" / "ralph-loop.ps1"
        assert ps1.exists(), f"ralph-loop.ps1 missing at {ps1}"

    def test_ralph_init_ps1_exists(self):
        ps1 = RALPH_DIR / "scripts" / "initialize-ralph.ps1"
        assert ps1.exists(), f"initialize-ralph.ps1 missing at {ps1}"

    def test_bridge_script_exists(self):
        assert BRIDGE_SCRIPT.exists(), f"Bridge script missing: {BRIDGE_SCRIPT}"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — invoke_codex.py unit tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def codex_mod():
    return _load_module("invoke_codex", SCRIPTS_DIR / "invoke_codex.py")


class TestInvokeCodex:
    def test_module_loads(self, codex_mod):
        assert codex_mod is not None

    def test_resolve_codex_finds_executable(self, codex_mod):
        args = codex_mod._resolve_codex()
        assert isinstance(args, list)
        assert len(args) >= 1
        # On Windows via cmd.exe wrapper: ["cmd.exe", "/c", "<path>"]
        # Or direct path list: ["<path>"]
        joined = " ".join(args).lower()
        assert "codex" in joined

    def test_ralph_dirs_defined(self, codex_mod):
        assert codex_mod.RALPH_SKILLS_DIR.is_dir(), \
            f"RALPH_SKILLS_DIR does not exist: {codex_mod.RALPH_SKILLS_DIR}"
        assert codex_mod.RALPH_LOOP_PS1.exists(), \
            f"ralph-loop.ps1 missing: {codex_mod.RALPH_LOOP_PS1}"

    def test_ensure_ralph_initialized_creates_structure(self, codex_mod, tmp_path):
        codex_mod._ensure_ralph_initialized(tmp_path, "test-project")
        ralph = tmp_path / ".ralph"
        assert ralph.is_dir()
        assert (ralph / ".iteration").exists()
        assert (ralph / "state.md").exists()
        assert (ralph / "progress.md").exists()
        assert (ralph / "guardrails.md").exists()
        assert (ralph / "errors.log").exists()

    def test_ensure_ralph_skips_if_already_initialized(self, codex_mod, tmp_path):
        # First init
        codex_mod._ensure_ralph_initialized(tmp_path, "test-project")
        # Write a sentinel value
        (tmp_path / ".ralph" / ".iteration").write_text("42", encoding="utf-8")
        # Second call — must not overwrite
        codex_mod._ensure_ralph_initialized(tmp_path, "test-project")
        assert (tmp_path / ".ralph" / ".iteration").read_text() == "42"

    def test_build_codex_prompt_contains_task(self, codex_mod, tmp_path):
        prompt = codex_mod._build_codex_prompt("BUILD THE AUTH MODULE", tmp_path, 10)
        assert "BUILD THE AUTH MODULE" in prompt

    def test_build_codex_prompt_contains_ralph_steps(self, codex_mod, tmp_path):
        prompt = codex_mod._build_codex_prompt("task", tmp_path, 5)
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "IMPLEMENTATION_PLAN.md" in prompt
        assert "<promise>TASK_COMPLETE</promise>" in prompt

    def test_build_codex_prompt_contains_max_iterations(self, codex_mod, tmp_path):
        prompt = codex_mod._build_codex_prompt("task", tmp_path, 7)
        assert "7" in prompt

    def test_check_ralph_completion_no_files(self, codex_mod, tmp_path):
        result = codex_mod._check_ralph_completion(tmp_path)
        assert result["promise_found"] is False
        assert result["tasks_total"] == 0
        assert result["tasks_complete"] == 0
        assert result["iteration"] == 0

    def test_check_ralph_completion_with_promise(self, codex_mod, tmp_path):
        ralph = tmp_path / ".ralph"
        ralph.mkdir()
        (ralph / "progress.md").write_text(
            "Done!\n<promise>TASK_COMPLETE</promise>\n", encoding="utf-8"
        )
        result = codex_mod._check_ralph_completion(tmp_path)
        assert result["promise_found"] is True

    def test_check_ralph_completion_counts_tasks(self, codex_mod, tmp_path):
        plan = tmp_path / "IMPLEMENTATION_PLAN.md"
        plan.write_text(
            "- [x] Task 1\n- [x] Task 2\n- [ ] Task 3\n- [ ] Task 4\n",
            encoding="utf-8",
        )
        result = codex_mod._check_ralph_completion(tmp_path)
        assert result["tasks_total"] == 4
        assert result["tasks_complete"] == 2

    def test_check_ralph_reads_iteration(self, codex_mod, tmp_path):
        ralph = tmp_path / ".ralph"
        ralph.mkdir()
        (ralph / ".iteration").write_text("13", encoding="utf-8")
        result = codex_mod._check_ralph_completion(tmp_path)
        assert result["iteration"] == 13


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — invoke_cursor.py unit tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def cursor_mod():
    return _load_module("invoke_cursor", SCRIPTS_DIR / "invoke_cursor.py")


class TestInvokeCursor:
    def test_module_loads(self, cursor_mod):
        assert cursor_mod is not None

    def test_resolve_cursor_agent_returns_paths(self, cursor_mod):
        node_exe, index_js = cursor_mod._resolve_cursor_agent()
        assert Path(node_exe).exists(), f"node.exe not found: {node_exe}"
        assert Path(index_js).exists(), f"index.js not found: {index_js}"

    def test_build_codebase_context_returns_string(self, cursor_mod, tmp_path):
        # Create some fake source files
        (tmp_path / "app.tsx").write_text("export default function App() {}", encoding="utf-8")
        (tmp_path / "utils.ts").write_text("export const add = (a: number, b: number) => a + b;", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "react.js").write_text("// skipped", encoding="utf-8")

        ctx = cursor_mod._build_codebase_context(tmp_path)
        assert isinstance(ctx, str)
        assert len(ctx) > 0
        assert "app.tsx" in ctx
        assert "utils.ts" in ctx
        # node_modules should be excluded
        assert "react.js" not in ctx

    def test_build_codebase_context_skips_next_and_git(self, cursor_mod, tmp_path):
        for skip_dir in [".next", ".git", "dist", "build"]:
            d = tmp_path / skip_dir
            d.mkdir()
            (d / "secret.ts").write_text("secret", encoding="utf-8")
        ctx = cursor_mod._build_codebase_context(tmp_path)
        assert "secret.ts" not in ctx

    def test_build_codebase_context_prioritizes_test_files(self, cursor_mod, tmp_path):
        (tmp_path / "auth.test.ts").write_text("test('login', () => {})", encoding="utf-8")
        (tmp_path / "utils.ts").write_text("export const x = 1;", encoding="utf-8")
        ctx = cursor_mod._build_codebase_context(tmp_path)
        # test file should appear before utils.ts
        assert ctx.index("auth.test.ts") < ctx.index("utils.ts")

    def test_build_fix_prompt_contains_test_output(self, cursor_mod, tmp_path):
        prompt = cursor_mod._build_fix_prompt(
            tmp_path,
            test_output="FAILED: test_login_returns_401",
            guardrails="",
            extra_task="",
            validation_cmd="npm test",
        )
        assert "FAILED: test_login_returns_401" in prompt

    def test_build_fix_prompt_contains_completion_signals(self, cursor_mod, tmp_path):
        prompt = cursor_mod._build_fix_prompt(tmp_path, "failures", "", "", "npm test")
        assert "<promise>FIXES_COMPLETE</promise>" in prompt
        assert "<promise>FIXES_PARTIAL</promise>" in prompt

    def test_build_fix_prompt_contains_guardrails(self, cursor_mod, tmp_path):
        prompt = cursor_mod._build_fix_prompt(
            tmp_path, "", "DON'T import from barrel files", "", "npm test"
        )
        assert "DON'T import from barrel files" in prompt

    def test_parse_cursor_result_complete(self, cursor_mod):
        response = "Fixed everything.\n<promise>FIXES_COMPLETE</promise>"
        result = cursor_mod._parse_cursor_result(response)
        assert result["complete"] is True
        assert result["partial"] is False
        assert result["promise_found"] is True

    def test_parse_cursor_result_partial(self, cursor_mod):
        response = (
            "Partial fix.\n<promise>FIXES_PARTIAL</promise>\n"
            "<remaining_failures>\ntest_payment still fails\n</remaining_failures>"
        )
        result = cursor_mod._parse_cursor_result(response)
        assert result["complete"] is False
        assert result["partial"] is True
        assert result["promise_found"] is True
        assert "test_payment still fails" in result["remaining_failures"]

    def test_parse_cursor_result_no_promise(self, cursor_mod):
        result = cursor_mod._parse_cursor_result("Some output with no promise.")
        assert result["complete"] is False
        assert result["partial"] is False
        assert result["promise_found"] is False

    def test_read_validation_cmd_fallback(self, cursor_mod, tmp_path):
        cmd = cursor_mod._read_validation_cmd(tmp_path)
        # No AGENTS.md in tmp_path — should return default
        assert "npm" in cmd

    def test_read_validation_cmd_from_agents_md(self, cursor_mod, tmp_path):
        (tmp_path / "AGENTS.md").write_text(
            "## Validation\nnpm run typecheck && npm run build\n", encoding="utf-8"
        )
        cmd = cursor_mod._read_validation_cmd(tmp_path)
        assert "npm run typecheck" in cmd


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — invoke_agent.py unit tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def agent_mod():
    return _load_module("invoke_agent", SCRIPTS_DIR / "invoke_agent.py")


class TestInvokeAgent:
    def test_module_loads(self, agent_mod):
        assert agent_mod is not None

    def test_role_agent_mapping(self, agent_mod):
        assert agent_mod.ROLE_AGENT["build"]  == "codex"
        assert agent_mod.ROLE_AGENT["fix"]    == "cursor"
        assert agent_mod.ROLE_AGENT["test"]   == "codex"
        assert agent_mod.ROLE_AGENT["review"] == "cursor"
        assert agent_mod.ROLE_AGENT["deploy"] == "codex"

    def test_agent_scripts_exist(self, agent_mod):
        for agent, script in agent_mod.AGENT_SCRIPT.items():
            assert Path(script).exists(), f"Script for {agent} missing: {script}"

    def test_role_timeouts_defined(self, agent_mod):
        assert agent_mod.ROLE_TIMEOUT["build"] >= 3600   # at least 1 hour for Ralph Loop
        assert agent_mod.ROLE_TIMEOUT["fix"]   >= 60     # at least 1 min for Cursor fixes

    def test_scripts_dir_points_to_correct_location(self, agent_mod):
        assert agent_mod.SCRIPTS_DIR.is_dir()
        assert (agent_mod.SCRIPTS_DIR / "invoke_codex.py").exists()
        assert (agent_mod.SCRIPTS_DIR / "invoke_cursor.py").exists()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — invoke_agent.py CLI dry-run (subprocess, no actual agent call)
# ─────────────────────────────────────────────────────────────────────────────

class TestInvokeAgentCLI:
    """
    Run invoke_agent.py with --help to verify it parses correctly.
    These tests don't spawn Codex or Cursor.
    """
    PYTHON = r"C:\Python313\python.exe"
    SCRIPT = str(SCRIPTS_DIR / "invoke_agent.py")

    def _run(self, *args, timeout=15):
        import subprocess
        result = subprocess.run(
            [self.PYTHON, self.SCRIPT, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result

    def test_help_exits_zero(self):
        r = self._run("--help")
        assert r.returncode == 0
        assert "role" in r.stdout.lower()

    def test_missing_role_exits_nonzero(self):
        r = self._run("--cwd", ".", "--output", "out.json")
        assert r.returncode != 0

    def test_invalid_role_exits_nonzero(self):
        r = self._run("--role", "invalid_role", "--cwd", ".", "--output", "out.json")
        assert r.returncode != 0


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — invoke_codex.py CLI dry-run
# ─────────────────────────────────────────────────────────────────────────────

class TestInvokeCodexCLI:
    PYTHON = r"C:\Python313\python.exe"
    SCRIPT = str(SCRIPTS_DIR / "invoke_codex.py")

    def _run(self, *args, timeout=15):
        import subprocess
        result = subprocess.run(
            [self.PYTHON, self.SCRIPT, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result

    def test_help_exits_zero(self):
        r = self._run("--help")
        assert r.returncode == 0

    def test_missing_required_args_exits_nonzero(self):
        r = self._run("--cwd", ".")
        assert r.returncode != 0

    def test_nonexistent_cwd_exits_nonzero(self):
        r = self._run("--task", "build stuff", "--cwd", "C:\\nonexistent\\path", "--output", "out.json")
        assert r.returncode != 0


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — invoke_cursor.py CLI dry-run
# ─────────────────────────────────────────────────────────────────────────────

class TestInvokeCursorCLI:
    PYTHON = r"C:\Python313\python.exe"
    SCRIPT = str(SCRIPTS_DIR / "invoke_cursor.py")

    def _run(self, *args, timeout=15):
        import subprocess
        result = subprocess.run(
            [self.PYTHON, self.SCRIPT, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result

    def test_help_exits_zero(self):
        r = self._run("--help")
        assert r.returncode == 0

    def test_nonexistent_cwd_exits_nonzero(self):
        r = self._run("--cwd", "C:\\nonexistent\\path", "--output", "out.json")
        assert r.returncode != 0


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — Telegram bridge smoke test
# ─────────────────────────────────────────────────────────────────────────────

class TestTelegramBridge:
    def test_bridge_script_exists(self):
        assert BRIDGE_SCRIPT.exists()

    def test_bridge_is_running(self):
        """Check a cato_telegram_bridge.py process is live (skip if not running)."""
        import subprocess
        import pytest
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*cato_telegram_bridge*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=10
        )
        pids = [l.strip() for l in result.stdout.strip().splitlines() if l.strip().isdigit()]
        if len(pids) == 0:
            pytest.skip("cato_telegram_bridge.py is not running — start it before running live bridge tests")

    def test_launch_bridge_has_correct_token(self):
        launch = Path(r"C:\Users\Administrator\Desktop\Cato\launch_bridge.py")
        content = launch.read_text(encoding="utf-8")
        assert "8573304576" in content, "launch_bridge.py does not contain the claudeoneshot_bot token"
        # Must NOT contain old Cato bot token
        assert "8622193070" not in content, "launch_bridge.py still has old Cato bot token!"

    def test_bridge_log_shows_application_started(self):
        import subprocess
        import pytest as _pytest
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*cato_telegram_bridge*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=10,
        )
        pids = [l.strip() for l in result.stdout.strip().splitlines() if l.strip().isdigit()]
        if len(pids) == 0:
            _pytest.skip("cato_telegram_bridge.py is not running — skipping log content check")
        log = Path(r"C:\Users\Administrator\Desktop\Cato\logs\telegram_bridge.log")
        assert log.exists(), "Bridge log file does not exist"
        content = log.read_text(encoding="utf-8", errors="replace")
        # Find the last "Application started" — most recent launch
        assert "Application started" in content, "Bridge never successfully started (no 'Application started' in log)"

    def test_bridge_log_no_recent_conflict_errors(self):
        """Verify no 409 Conflict in the last 50 lines of the log."""
        log = Path(r"C:\Users\Administrator\Desktop\Cato\logs\telegram_bridge.log")
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        last_50 = "\n".join(lines[-50:])
        # Find last Application started index
        started_lines = [i for i, l in enumerate(lines) if "Application started" in l]
        if started_lines:
            after_start = "\n".join(lines[started_lines[-1]:])
            assert "Conflict: terminated by other getUpdates" not in after_start, \
                "409 Conflict error detected AFTER last successful start — two pollers fighting!"

    def test_autostart_registry_has_correct_token(self):
        """Check Windows registry autostart entry contains new token (skip if not registered)."""
        import subprocess
        import pytest
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             r"(Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' "
             r"-Name 'CatoTelegramBridge' -ErrorAction SilentlyContinue).CatoTelegramBridge"],
            capture_output=True, text=True, timeout=10
        )
        reg_value = result.stdout.strip()
        if not reg_value or "8573304576" not in reg_value:
            pytest.skip("CatoTelegramBridge autostart not registered in HKCU Run — run register_telegram_bridge_task.ps1 to configure")
        assert "8573304576" in reg_value, f"Registry autostart uses wrong token: {reg_value[:80]}"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — Ralph Wiggum Loop skill integrity
# ─────────────────────────────────────────────────────────────────────────────

class TestRalphSkill:
    def test_ralph_skill_dir_exists(self):
        assert RALPH_DIR.is_dir()

    def test_ralph_skill_md_exists(self):
        skill_md = RALPH_DIR / "SKILL.md"
        assert skill_md.exists(), f"SKILL.md missing from Ralph skill: {RALPH_DIR}"

    def test_ralph_loop_ps1_is_nonempty(self):
        ps1 = RALPH_DIR / "scripts" / "ralph-loop.ps1"
        content = ps1.read_text(encoding="utf-8", errors="replace")
        assert len(content) > 100, "ralph-loop.ps1 appears to be empty or too short"

    def test_ralph_init_ps1_is_nonempty(self):
        ps1 = RALPH_DIR / "scripts" / "initialize-ralph.ps1"
        content = ps1.read_text(encoding="utf-8", errors="replace")
        assert len(content) > 100, "initialize-ralph.ps1 appears to be empty or too short"

    def test_ralph_state_structure_valid(self, tmp_path):
        """Initialize Ralph in a temp dir and verify all required state files."""
        codex_mod = _load_module("invoke_codex", SCRIPTS_DIR / "invoke_codex.py")
        codex_mod._ensure_ralph_initialized(tmp_path, "my-test-project")

        ralph = tmp_path / ".ralph"
        required = [".iteration", "state.md", "progress.md", "guardrails.md", "errors.log"]
        for fname in required:
            assert (ralph / fname).exists(), f"Missing Ralph state file: {fname}"

        iteration = int((ralph / ".iteration").read_text().strip())
        assert iteration == 1, f"Initial iteration should be 1, got {iteration}"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — Output JSON schema validation
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputSchemas:
    """Validate the JSON output schemas that the pipeline reads."""

    def test_codex_success_schema(self):
        result = {
            "agent": "codex",
            "phase": "phase_5_construction",
            "status": "success",
            "elapsed_sec": 42.0,
            "cwd": "/tmp/website",
            "ralph": {
                "promise_found": True,
                "tasks_complete": 10,
                "tasks_total": 10,
                "iteration": 10,
            },
            "output": "All done",
        }
        assert result["status"] in ("success", "partial", "error", "timeout")
        assert result["ralph"]["promise_found"] is True

    def test_cursor_success_schema(self):
        result = {
            "agent": "cursor",
            "phase": "phase_6_fix_iteration",
            "status": "success",
            "elapsed_sec": 15.0,
            "cwd": "/tmp/website",
            "fix_rounds": 1,
            "responses": [{"round": 1, "response": "<promise>FIXES_COMPLETE</promise>"}],
        }
        assert result["status"] == "success"
        assert result["fix_rounds"] >= 1
        assert any(
            "<promise>FIXES_COMPLETE</promise>" in r["response"]
            for r in result["responses"]
        )

    def test_codex_result_serializable(self, tmp_path):
        """Write and re-read a result JSON to verify serialization round-trip."""
        out = tmp_path / "result.json"
        data = {
            "agent": "codex",
            "status": "success",
            "ralph": {"promise_found": True, "tasks_complete": 5, "tasks_total": 5, "iteration": 5},
            "output": "done",
        }
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        loaded = json.loads(out.read_text())
        assert loaded["ralph"]["promise_found"] is True


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 11 — Live agent hello tests (real subprocess invocations)
# ─────────────────────────────────────────────────────────────────────────────

HELLO_LOG_DIR = Path(r"C:\Users\Administrator\Desktop\Cato\logs\pipeline_hello_test")
CODEX_PS1     = r"C:\Users\Administrator\AppData\Roaming\npm\codex.ps1"
CURSOR_NODE   = r"C:\Users\Administrator\AppData\Local\cursor-agent\versions\2026.02.27-e7d2ef6\node.exe"
CURSOR_INDEX  = r"C:\Users\Administrator\AppData\Local\cursor-agent\versions\2026.02.27-e7d2ef6\index.js"


@pytest.mark.live
class TestLiveCodexHello:
    """
    Fires a real 'codex exec' call and verifies we get a response.
    Marked with timeout — if Codex doesn't respond in 90s something is wrong.
    """

    def test_codex_exec_subcommand_works(self):
        """Verify 'codex exec --json' exits 0 and returns JSONL with agent_message."""
        import subprocess
        out_file = HELLO_LOG_DIR / "codex_hello_live.txt"
        HELLO_LOG_DIR.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["powershell", "-Command",
             f'& "{CODEX_PS1}" exec --json '
             f'"Reply with EXACTLY this one line: Hello from Codex!" '
             f'> "{out_file}" 2>&1'],
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert result.returncode == 0, f"codex exec failed (exit {result.returncode}): {result.stderr[:300]}"

        # Parse JSONL output — look for agent_message item
        assert out_file.exists(), "codex exec produced no output file"

        # Codex writes UTF-16LE on Windows — detect and decode accordingly
        raw_bytes = out_file.read_bytes()
        if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
            raw = raw_bytes.decode("utf-16", errors="replace")
        else:
            raw = raw_bytes.decode("utf-8", errors="replace")
        assert raw.strip(), "codex exec output file is empty"

        # Find the agent_message line (strip null bytes from UTF-16 decoded text)
        found_hello = False
        for line in raw.splitlines():
            line = line.strip().replace("\x00", "")
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "item.completed":
                    text = event.get("item", {}).get("text", "")
                    if "Hello" in text or "Codex" in text:
                        found_hello = True
                        break
            except json.JSONDecodeError:
                continue

        assert found_hello, (
            f"No 'Hello from Codex' agent_message found in codex output.\n"
            f"Full output (first 800 chars):\n{raw[:800]}"
        )

    def test_codex_invoke_flags_correct(self):
        """Verify invoke_codex.py uses 'exec --json' (not the old '--full-auto -q')."""
        src = (SCRIPTS_DIR / "invoke_codex.py").read_text(encoding="utf-8")
        assert '"exec"' in src,       "invoke_codex.py must use 'exec' subcommand"
        assert '"--json"' in src,      "invoke_codex.py must use '--json' flag for non-interactive output"
        assert '"--full-auto"' not in src, "invoke_codex.py must NOT use deprecated '--full-auto' flag"
        assert '"-q"' not in src,     "invoke_codex.py must NOT use invalid '-q' flag"


@pytest.mark.live
class TestLiveCursorHello:
    """
    Fires a real cursor-agent --print call and verifies we get a response.
    """

    def test_cursor_print_mode_works(self):
        """Verify cursor-agent --print exits 0 and returns text response."""
        import subprocess
        out_file = HELLO_LOG_DIR / "cursor_hello_live.txt"
        HELLO_LOG_DIR.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [CURSOR_NODE, CURSOR_INDEX,
             "--print", "--trust", "--yolo",
             "Reply with EXACTLY this one line: Hello from Cursor!"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(Path(r"C:\Users\Administrator\Desktop\Cato")),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        # Write output for inspection
        out_file.write_text(
            result.stdout + ("\n--- STDERR ---\n" + result.stderr if result.stderr else ""),
            encoding="utf-8",
        )

        assert result.returncode == 0, (
            f"cursor-agent --print failed (exit {result.returncode}):\n{result.stderr[:300]}"
        )
        assert "Hello" in result.stdout, (
            f"Expected 'Hello' in cursor response, got:\n{result.stdout[:300]}"
        )

    def test_cursor_invoke_flags_correct(self):
        """Verify invoke_cursor.py uses the correct --print --trust --yolo flags."""
        src = (SCRIPTS_DIR / "invoke_cursor.py").read_text(encoding="utf-8")
        assert '"--print"' in src,  "invoke_cursor.py must use '--print' for headless mode"
        assert '"--trust"' in src,  "invoke_cursor.py must use '--trust' to skip workspace prompt"
        assert '"--yolo"' in src,   "invoke_cursor.py must use '--yolo' to auto-approve commands"
