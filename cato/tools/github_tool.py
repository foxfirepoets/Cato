"""
cato/tools/github_tool.py — Super-GitHub 3-Model PR Review (Skill 3).

Wraps the ``gh`` CLI (GitHub CLI) for GitHub operations.  On Windows,
.CMD wrappers are detected via shutil.which() and executed through cmd.exe /c,
identical to how codex/gemini are handled in cli_invoker.py.

GitHub token is stored in the vault as ``github_token`` and injected as
``GH_TOKEN`` environment variable to all subprocesses.

3-Model PR Review pipeline:
  1. Fetch diff via ``gh pr diff <number>``
  2. Dispatch Claude / Codex / Gemini in parallel via cli_process_pool
  3. Score confidence via confidence_extractor
  4. Abort if models diverge via early_terminator
  5. Synthesize via synthesis.py
  6. Post result as PR comment via ``gh pr comment``
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from typing import Any, Optional

from ..orchestrator.cli_invoker import _resolve_cli, _run_subprocess_async, SubprocessError
from ..orchestrator.confidence_extractor import extract_confidence
from ..orchestrator.early_terminator import wait_for_threshold
from ..orchestrator.synthesis import simple_synthesis

logger = logging.getLogger(__name__)

# Maximum diff size sent to models (characters)
_MAX_DIFF_CHARS = 12_000


# ---------------------------------------------------------------------------
# gh CLI resolver
# ---------------------------------------------------------------------------

def _resolve_gh() -> list[str]:
    """
    Resolve the ``gh`` CLI to an executable args list.

    Handles Windows .CMD wrappers the same way cli_invoker.py handles
    codex and gemini — wraps them in ``cmd.exe /c``.

    Raises FileNotFoundError if gh is not installed.
    """
    resolved = shutil.which("gh")
    if resolved is None:
        raise FileNotFoundError(
            "'gh' (GitHub CLI) not found on PATH. "
            "Install from https://cli.github.com/"
        )
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]
    return [resolved]


# ---------------------------------------------------------------------------
# PR number extraction
# ---------------------------------------------------------------------------

def _extract_pr_number(target: str) -> int:
    """
    Parse a PR URL or bare integer into a PR number.

    Accepts:
        "123"
        "https://github.com/org/repo/pull/123"
        "https://github.com/org/repo/pull/123/"
    """
    target = target.strip().rstrip("/")
    # Try bare integer first
    try:
        return int(target)
    except ValueError:
        pass
    # URL pattern
    m = re.search(r"/pull/(\d+)", target)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot parse PR number from: {target!r}")


# ---------------------------------------------------------------------------
# GitHubTool
# ---------------------------------------------------------------------------

class GitHubTool:
    """
    GitHub operations tool with 3-model AI review pipeline.

    All ``gh`` subprocesses receive ``GH_TOKEN`` from the vault.

    Usage::

        tool = GitHubTool(vault=vault)
        review = await tool.pr_review("123")
        await tool.issue_create(title="Bug: ...", body="...")
    """

    def __init__(self, vault: Any = None) -> None:
        self._vault = vault

    # ------------------------------------------------------------------ #
    # Environment helpers                                                 #
    # ------------------------------------------------------------------ #

    def _gh_env(self) -> dict[str, str]:
        """Return environment dict with GH_TOKEN injected if available."""
        env = dict(os.environ)

        # Resolve token: check env vars first, then vault, in priority order.
        # Legacy alias: some installs stored the token under the account-specific
        # name GITHUB_FOXFIREPOETS_TOKEN.  Check it as a fallback.
        token = (
            os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("github_token")
            or os.environ.get("GITHUB_FOXFIREPOETS_TOKEN")  # legacy account-specific name
        )

        if not token and self._vault is not None:
            try:
                token = (
                    self._vault.get("GITHUB_TOKEN")
                    or self._vault.get("GH_TOKEN")
                    or self._vault.get("github_token")
                    or self._vault.get("GITHUB_FOXFIREPOETS_TOKEN")  # legacy vault key
                )
            except Exception:
                pass

        if token:
            env["GH_TOKEN"] = token
        return env

    async def _run_gh(self, args: list[str], timeout_sec: float = 30.0) -> str:
        """
        Run a ``gh`` subcommand asynchronously.

        Returns stdout text.  Raises SubprocessError on non-zero exit.
        """
        gh_args = _resolve_gh() + args
        env = self._gh_env()

        proc = await asyncio.create_subprocess_exec(
            *gh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise asyncio.TimeoutError(f"gh {' '.join(args)} timed out after {timeout_sec}s")

        if proc.returncode != 0:
            raise SubprocessError(
                cmd=f"gh {' '.join(args)}",
                returncode=proc.returncode,
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        return stdout.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ #
    # PR operations                                                       #
    # ------------------------------------------------------------------ #

    async def pr_review(self, target: str) -> str:
        """
        Run the full 3-model review pipeline for a PR.

        Steps:
          1. Parse target to PR number
          2. Fetch diff via ``gh pr diff``
          3. Dispatch Claude / Codex / Gemini in parallel
          4. Collect confidence scores, abort if diverge
          5. Synthesize via simple_synthesis
          6. Post comment via ``gh pr comment``
          7. Return formatted review text
        """
        try:
            pr_num = _extract_pr_number(target)
        except ValueError as exc:
            return f"Error: {exc}"

        # Step 1: fetch diff
        try:
            diff = await self._run_gh(["pr", "diff", str(pr_num)], timeout_sec=30)
        except Exception as exc:
            return f"Error fetching PR diff: {exc}"

        # Truncate diff to model-safe size
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS] + f"\n... [diff truncated at {_MAX_DIFF_CHARS} chars]"

        prompt = (
            f"Review this pull request diff and provide:\n"
            f"1. A summary of what changed\n"
            f"2. Potential bugs or issues\n"
            f"3. Security concerns\n"
            f"4. Code quality feedback\n"
            f"5. A recommendation (approve / request changes)\n\n"
            f"--- DIFF START ---\n{diff}\n--- DIFF END ---\n"
            f"Confidence: <score 0.0–1.0>"
        )

        # Step 2: dispatch 3 models in parallel
        results_queue: asyncio.Queue = asyncio.Queue()
        cancel_event = asyncio.Event()

        async def _invoke_model(model: str) -> None:
            try:
                result = await _invoke_single_model(model, prompt, self._gh_env())
                await results_queue.put(result)
            except Exception as exc:
                logger.warning("Model %s failed in PR review: %s", model, exc)
                await results_queue.put({
                    "model": model,
                    "response": f"[{model} failed: {exc}]",
                    "confidence": 0.1,
                    "latency_ms": 0,
                })

        model_tasks = [
            asyncio.create_task(_invoke_model("claude")),
            asyncio.create_task(_invoke_model("codex")),
            asyncio.create_task(_invoke_model("gemini")),
        ]

        # Step 3: collect via early terminator
        threshold_result = await wait_for_threshold(
            results_queue=results_queue,
            threshold=0.85,
            max_wait_ms=60_000,
            cancel_event=cancel_event,
        )

        # Cancel slow models if threshold was met
        if cancel_event.is_set():
            for t in model_tasks:
                if not t.done():
                    t.cancel()
        await asyncio.gather(*model_tasks, return_exceptions=True)

        # Collect all results that arrived
        collected: list[dict] = []
        collected.append(threshold_result["winner"])
        while not results_queue.empty():
            try:
                collected.append(results_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        # Step 4: synthesis (need exactly 3 for simple_synthesis)
        def _pad(results: list[dict], n: int = 3) -> list[dict]:
            while len(results) < n:
                results.append({"model": "unknown", "response": "", "confidence": 0.0, "latency_ms": 0})
            return results[:n]

        padded = _pad(list(collected))
        synthesis = simple_synthesis(padded[0], padded[1], padded[2])
        primary = synthesis["primary"]
        review_text = primary["response"]
        model_name = primary["model"]
        conf = primary["confidence"]

        # Step 5: post comment
        comment = (
            f"## Cato AI Review\n\n"
            f"**Model:** {model_name}  **Confidence:** {conf:.0%}  "
            f"**Early termination:** {threshold_result['terminated_early']}\n\n"
            f"{review_text}"
        )
        try:
            await self._run_gh(["pr", "comment", str(pr_num), "--body", comment], timeout_sec=20)
            posted = True
        except Exception as exc:
            logger.warning("Could not post PR comment: %s", exc)
            posted = False

        header = f"PR #{pr_num} Review — {model_name} ({conf:.0%} confidence)"
        if posted:
            header += " [comment posted]"
        return f"{header}\n\n{review_text}"

    async def pr_merge(self, pr_number: int, method: str = "squash") -> str:
        """Merge a PR using the specified method (squash | merge | rebase)."""
        valid_methods = {"squash", "merge", "rebase"}
        if method not in valid_methods:
            return f"Invalid merge method {method!r}. Choose from: {', '.join(valid_methods)}"
        try:
            out = await self._run_gh(["pr", "merge", str(pr_number), f"--{method}"], timeout_sec=30)
            return out or f"PR #{pr_number} merged via {method}."
        except Exception as exc:
            return f"Error merging PR #{pr_number}: {exc}"

    # ------------------------------------------------------------------ #
    # Issue operations                                                    #
    # ------------------------------------------------------------------ #

    async def issue_create(self, title: str, body: str = "") -> str:
        """Create a new issue and return the issue URL."""
        args = ["issue", "create", "--title", title]
        if body:
            args += ["--body", body]
        else:
            args += ["--body", ""]
        try:
            out = await self._run_gh(args, timeout_sec=20)
            return out.strip() or "Issue created."
        except Exception as exc:
            return f"Error creating issue: {exc}"

    async def issue_list(self) -> str:
        """List open issues as formatted text."""
        try:
            out = await self._run_gh(
                ["issue", "list", "--json", "number,title,state,url", "--limit", "20"],
                timeout_sec=20,
            )
        except Exception as exc:
            return f"Error listing issues: {exc}"

        try:
            issues = json.loads(out)
        except json.JSONDecodeError:
            return out

        if not issues:
            return "No open issues found."

        lines = ["Open Issues:", "-" * 40]
        for issue in issues:
            lines.append(
                f"#{issue.get('number', '?')}  {issue.get('title', '')}  [{issue.get('state', '')}]"
            )
            url = issue.get("url", "")
            if url:
                lines.append(f"   {url}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Release operations                                                  #
    # ------------------------------------------------------------------ #

    async def release_create(self, tag: str, notes: str = "") -> str:
        """Create a GitHub release for *tag*."""
        args = ["release", "create", tag, "--generate-notes"]
        if notes:
            args += ["--notes", notes]
        try:
            out = await self._run_gh(args, timeout_sec=30)
            return out.strip() or f"Release {tag} created."
        except Exception as exc:
            return f"Error creating release {tag}: {exc}"


# ---------------------------------------------------------------------------
# Single-model invocation helper
# ---------------------------------------------------------------------------

async def _invoke_single_model(model: str, prompt: str, env: dict) -> dict:
    """
    Invoke one model (claude | codex | gemini) with *prompt*.

    Returns a result dict compatible with simple_synthesis.
    Falls back gracefully when the model CLI is not installed.
    """
    import time as _time
    start = _time.time()

    try:
        args = _resolve_cli(model)
    except FileNotFoundError:
        elapsed = (_time.time() - start) * 1000
        return {
            "model": model,
            "response": f"[{model} not installed]",
            "confidence": 0.0,
            "latency_ms": elapsed,
        }

    # Build model-specific invocation
    if model == "claude":
        full_args = args + ["-p", prompt, "--output-format", "text"]
    elif model == "codex":
        full_args = args + ["-q", "--no-session-persistence", prompt]
    elif model == "gemini":
        full_args = args + ["-p", prompt]
    else:
        full_args = args + [prompt]

    try:
        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        response = await asyncio.wait_for(
            _run_subprocess_async(full_args, timeout_sec=45.0),
            timeout=50.0,
        )
    except Exception as exc:
        elapsed = (_time.time() - start) * 1000
        return {
            "model": model,
            "response": f"[{model} error: {exc}]",
            "confidence": 0.1,
            "latency_ms": elapsed,
        }

    elapsed = (_time.time() - start) * 1000
    confidence = extract_confidence(response)
    return {
        "model": model,
        "response": response,
        "confidence": confidence,
        "latency_ms": elapsed,
    }
