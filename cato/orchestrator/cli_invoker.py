"""
Async CLI invoker for Claude CLI, Codex CLI, Gemini CLI, and Cursor Agent.
Handles parallel invocation of multiple models with fallback support.

All models are invoked via their installed CLIs (claude, codex, gemini, cursor)
using asyncio.create_subprocess_exec() for true non-blocking parallelism.

On Windows, .CMD batch wrappers (npm-installed CLIs like codex and gemini)
are resolved via shutil.which() and executed through cmd.exe /c.

Subagent routing
----------------
``invoke_subagent(prompt, task, backend)`` lets Cato delegate coding tasks to
whichever CLI the user has configured as their ``subagent_coding_backend``.
This mirrors what OpenClaw does with ChatGPT (route coding to GPT via OAuth),
but Cato supports four backends: claude, codex, gemini, cursor.

Users configure this in ~/.cato/config.yaml::

    subagent_enabled: true
    subagent_coding_backend: codex   # or claude / gemini / cursor
"""

import asyncio
import logging
import shutil
import sys
import time
from typing import Any, Dict, Literal, Optional, Tuple

from cato.orchestrator.confidence_extractor import extract_confidence

SubagentBackend = Literal["claude", "codex", "gemini", "cursor"]

logger = logging.getLogger(__name__)


class SubprocessError(RuntimeError):
    """Raised when a CLI subprocess exits with a non-zero return code."""
    def __init__(self, cmd: str, returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{cmd} exited with code {returncode}: {stderr[:200]}")


def _resolve_cli(name: str) -> list:
    """
    Resolve a CLI command name to an executable args list.

    On Windows, npm-installed CLIs are .CMD batch wrappers that cannot be
    executed directly by asyncio.create_subprocess_exec(). This function
    uses shutil.which() to find the real path, and wraps .cmd/.bat files
    in 'cmd.exe /c' so they execute correctly.

    Args:
        name: CLI command name (e.g. "codex", "gemini", "claude").

    Returns:
        List of args suitable for create_subprocess_exec.

    Raises:
        FileNotFoundError: If the CLI is not installed.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(f"{name} not found on PATH")

    # On Windows, .CMD/.BAT wrappers need cmd.exe /c to execute
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]

    return [resolved]


async def _run_subprocess_async(
    args: list,
    timeout_sec: float = 60.0,
    stdin_data: Optional[bytes] = None,
) -> str:
    """
    Run a subprocess asynchronously without blocking the event loop.

    Args:
        args: Command and arguments list.
        timeout_sec: Maximum time to wait for the process.
        stdin_data: Optional bytes to write to the process stdin before
            waiting for output.  When None, stdin is inherited (default).

    Returns:
        stdout text on success.

    Raises:
        FileNotFoundError: If the executable is not found.
        asyncio.TimeoutError: If the process exceeds timeout_sec.
        SubprocessError: If the process exits with non-zero return code.
    """
    import os as _os
    _env = {k: v for k, v in _os.environ.items() if k != "CLAUDECODE"}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data), timeout=timeout_sec
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()  # reap the zombie
        raise

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise SubprocessError(args[0], proc.returncode, stderr_text)

    if not stdout_text and stderr_text:
        # Some CLIs write to stderr even on success (warnings, progress)
        logger.warning("CLI %s returned empty stdout, stderr: %s", args[0], stderr_text[:200])

    return stdout_text


async def invoke_claude_api(prompt: str, task: str) -> Dict:
    """
    Invoke Claude via the ``claude`` CLI (non-blocking).

    Uses asyncio.create_subprocess_exec() so the event loop is not blocked
    while waiting for the child process.  Falls back to a mock response with
    confidence 0.75 when the ``claude`` binary is not found.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        {
            "model": "claude",
            "response": str,
            "confidence": float,
            "latency_ms": float,
            "degraded": bool
        }
    """
    start_time = time.time()

    try:
        full_prompt = f"Task: {task}\n\nContext: {prompt}"

        # Pool-first: use persistent process if warm
        from cato.orchestrator.cli_process_pool import get_pool
        pool = get_pool()
        if pool.is_warm("claude"):
            response_text = await pool.send_to("claude", full_prompt)
            source = "pool"
        else:
            cli_args = _resolve_cli("claude")
            response_text = await _run_subprocess_async(
                cli_args + ["-p", full_prompt],
                timeout_sec=60.0,
            )
            source = "subprocess"
        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000

        return {
            "model": "claude",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": source,
        }
    except FileNotFoundError:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("claude CLI not found, using mock response")
        return {
            "model": "claude",
            "response": f"[Claude Mock] CLI not installed. Task: {task}",
            "confidence": 0.75,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        # Note: SubprocessError only comes from _run_subprocess_async (cold path).
        # Pool errors surface as RuntimeError and fall through to the generic handler.
        latency_ms = (time.time() - start_time) * 1000
        logger.error("claude CLI failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "claude",
            "response": f"[Claude Error] {e.stderr[:500]}",
            "confidence": 0.5,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "claude",
            "response": "[Claude Error] Process timed out after 60s",
            "confidence": 0.5,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "claude",
            "response": f"[Claude Error] {str(e)}",
            "confidence": 0.5,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_codex_cli(prompt: str, task: str) -> Dict:
    """
    Invoke Codex CLI asynchronously.

    Uses _resolve_cli() to handle Windows .CMD wrappers, then
    asyncio.create_subprocess_exec() for non-blocking execution.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        dict with model, response, confidence, latency_ms, degraded keys.
    """
    start_time = time.time()

    try:
        full_prompt = f"Task: {task}\n\nContext: {prompt}"

        # Pool-first: use persistent process if warm
        from cato.orchestrator.cli_process_pool import get_pool
        pool = get_pool()
        if pool.is_warm("codex"):
            response_text = await pool.send_to("codex", full_prompt)
            source = "pool"
        else:
            cli_args = _resolve_cli("codex")
            response_text = await _run_subprocess_async(
                cli_args + ["exec", full_prompt],
                timeout_sec=60.0,
            )
            source = "subprocess"
        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000

        return {
            "model": "codex",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": source,
        }
    except FileNotFoundError:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("codex CLI not found, using mock response")
        return {
            "model": "codex",
            "response": f"[Codex Mock] CLI not installed. Task: {task}",
            "confidence": 0.72,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        # Note: SubprocessError only comes from _run_subprocess_async (cold path).
        # Pool errors surface as RuntimeError and fall through to the generic handler.
        latency_ms = (time.time() - start_time) * 1000
        logger.error("codex CLI failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "codex",
            "response": f"[Codex Error] {e.stderr[:500]}",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "codex",
            "response": "[Codex Error] Process timed out after 60s",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "codex",
            "response": f"[Codex Error] {str(e)}",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_gemini_cli(prompt: str, task: str) -> Dict:
    """
    Invoke Gemini CLI asynchronously.

    Uses _resolve_cli() to handle Windows .CMD wrappers, then
    asyncio.create_subprocess_exec() for non-blocking execution.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        dict with model, response, confidence, latency_ms, degraded keys.
    """
    start_time = time.time()

    try:
        cli_args = _resolve_cli("gemini")
        full_prompt = f"Task: {task}\n\nContext: {prompt}"
        response_text = await _run_subprocess_async(
            cli_args + ["-p", full_prompt],
            timeout_sec=60.0,
        )
        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000

        return {
            "model": "gemini",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": "subprocess",
        }
    except FileNotFoundError:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("gemini CLI not found, using mock response")
        return {
            "model": "gemini",
            "response": f"[Gemini Mock] CLI not installed. Task: {task}",
            "confidence": 0.68,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error("gemini CLI failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "gemini",
            "response": f"[Gemini Error] {e.stderr[:500]}",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "gemini",
            "response": "[Gemini Error] Process timed out after 60s",
            "confidence": 0.55,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "gemini",
            "response": f"[Gemini Error] {str(e)}",
            "confidence": 0.55,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_all_parallel(
    prompt: str,
    task: str,
) -> Tuple[Dict, Dict, Dict]:
    """
    Invoke all 3 models concurrently using asyncio.

    All three coroutines are scheduled as tasks so the event loop can
    interleave their I/O wait times.  The function still awaits all three
    results before returning; for mid-flight cancellation driven by an early
    confidence threshold, use ``invoke_with_early_termination`` instead.

    On Windows, Gemini is skipped (known stdin pipe detection hang) and a
    degraded stub is returned in its place so callers see a consistent
    three-element tuple.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        (claude_result, codex_result, gemini_result) — all three dicts.
    """
    claude_task = asyncio.create_task(invoke_claude_api(prompt, task))
    codex_task = asyncio.create_task(invoke_codex_cli(prompt, task))

    # Gemini hangs on Windows due to stdin pipe detection issue — skip it.
    if sys.platform != "win32":
        gemini_task = asyncio.create_task(invoke_gemini_cli(prompt, task))
        claude_result, codex_result, gemini_result = await asyncio.gather(
            claude_task, codex_task, gemini_task
        )
    else:
        logger.info("[CodingAgent] Skipping Gemini on Windows (known stdin hang issue)")
        gemini_stub: Dict = {
            "model": "gemini",
            "response": "[Gemini] Skipped on Windows (stdin pipe detection issue)",
            "confidence": 0.0,
            "latency_ms": 0.0,
            "degraded": True,
            "source": "skipped",
        }
        claude_result, codex_result = await asyncio.gather(claude_task, codex_task)
        gemini_result = gemini_stub

    return (claude_result, codex_result, gemini_result)


async def invoke_with_early_termination(
    prompt: str,
    task: str,
    results_queue: asyncio.Queue,
    threshold: float = 0.90,
    cancel_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Invoke all 3 models concurrently and push each result into *results_queue*
    as soon as it arrives.

    This is the correct companion to ``wait_for_threshold``.  Results land in
    the queue the moment each model finishes, so ``wait_for_threshold`` can
    act on the first high-confidence result and cancel the remaining work
    before the slower models finish.

    When *cancel_event* is set, any model tasks that have not yet completed
    are cancelled, achieving real latency savings for the early-termination
    path.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.
        results_queue: Queue that ``wait_for_threshold`` is consuming.
        threshold: Passed through for documentation purposes; the actual
            termination decision is made by ``wait_for_threshold``.
        cancel_event: Optional asyncio.Event; when set, remaining model tasks
            are cancelled immediately.
    """
    async def _invoke_and_enqueue(coro):
        result = await coro
        await results_queue.put(result)

    model_tasks = [
        asyncio.create_task(_invoke_and_enqueue(invoke_claude_api(prompt, task))),
        asyncio.create_task(_invoke_and_enqueue(invoke_codex_cli(prompt, task))),
    ]
    if sys.platform != "win32":
        model_tasks.append(
            asyncio.create_task(_invoke_and_enqueue(invoke_gemini_cli(prompt, task)))
        )
    else:
        logger.info("[CodingAgent] Skipping Gemini on Windows (known stdin hang issue)")
        # Immediately enqueue a degraded stub so consumers get the expected result count
        asyncio.get_event_loop().call_soon(
            lambda: results_queue.put_nowait({
                "model": "gemini",
                "response": "[Gemini] Skipped on Windows (stdin pipe detection issue)",
                "confidence": 0.0,
                "latency_ms": 0.0,
                "degraded": True,
                "source": "skipped",
            })
        )

    if cancel_event is not None:
        # Race: finish all models OR cancel_event fires
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        done, pending = await asyncio.wait(
            model_tasks + [cancel_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_waiter in done:
            # Threshold was met — cancel any models still running
            for t in model_tasks:
                if not t.done():
                    t.cancel()
            # Suppress CancelledError from cancelled tasks
            await asyncio.gather(*model_tasks, return_exceptions=True)
        else:
            # All models finished before cancellation
            cancel_waiter.cancel()
            await asyncio.gather(*model_tasks, return_exceptions=True)
    else:
        await asyncio.gather(*model_tasks, return_exceptions=True)


def _resolve_cursor_agent() -> Tuple[str, str]:
    """
    Locate the cursor-agent Node.js binary and index.js entry point.

    The cursor-agent CLI installs under:
        %LOCALAPPDATA%\\cursor-agent\\versions\\<version>\\

    It ships its own node.exe and a bundled index.js.  We use these directly
    rather than going through the .cmd/.ps1 launcher, which hangs in a
    non-interactive shell (PowerShell startup overhead + no TTY).

    Returns:
        (node_exe_path, index_js_path) — both absolute paths.

    Raises:
        FileNotFoundError: If the cursor-agent installation is not found.
    """
    import os
    from pathlib import Path

    base = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "versions"
    if not base.is_dir():
        raise FileNotFoundError(f"cursor-agent not installed (looked in {base})")

    # Pick the lexicographically latest version directory
    versions = sorted(p for p in base.iterdir() if p.is_dir())
    if not versions:
        raise FileNotFoundError(f"cursor-agent versions directory is empty: {base}")

    latest = versions[-1]
    node_exe = latest / "node.exe"
    index_js = latest / "index.js"

    if not node_exe.exists():
        raise FileNotFoundError(f"cursor-agent node.exe not found: {node_exe}")
    if not index_js.exists():
        raise FileNotFoundError(f"cursor-agent index.js not found: {index_js}")

    return str(node_exe), str(index_js)


async def invoke_cursor_cli(prompt: str, task: str) -> Dict:
    """
    Invoke Cursor Agent CLI in headless (non-interactive) mode.

    The cursor-agent CLI (``agent`` command, v2026.02.27+) supports a
    ``--print`` flag that enables headless/scriptable output — equivalent to
    Claude's ``-p`` flag.  Combined with ``--trust`` (skip workspace prompt)
    and ``--yolo`` (allow all tool use without confirmation), it can be driven
    from a subprocess without a TTY.

    The .cmd/.ps1 launchers hang in a non-interactive shell due to PowerShell
    startup overhead.  This function bypasses them by invoking node.exe and
    index.js from the versioned installation directory directly.

    Auth state is read from ~/.cursor/cli-config.json (set by ``agent login``).
    Model defaults to ``auto`` (Cursor's model router) to avoid hitting
    per-model usage caps on individual Claude/Codex allocations.

    Falls back to a degraded mock if:
    - cursor-agent is not installed
    - The subprocess times out (120 s)
    - The agent returns a non-zero exit code

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        dict with model, response, confidence, latency_ms, degraded, source keys.
    """
    start_time = time.time()

    try:
        node_exe, index_js = _resolve_cursor_agent()
        full_prompt = f"Task: {task}\n\nContext: {prompt}"

        response_text = await _run_subprocess_async(
            [node_exe, index_js, "--print", "--trust", "--yolo", "--model", "auto", full_prompt],
            timeout_sec=120.0,
        )

        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "cursor",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": "subprocess",
        }
    except FileNotFoundError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("cursor-agent not found: %s", e)
        return {
            "model": "cursor",
            "response": f"[Cursor] Agent CLI not installed. Run `agent login` to set up. ({e})",
            "confidence": 0.0,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error("cursor-agent failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "cursor",
            "response": f"[Cursor Error] {e.stderr[:500]}",
            "confidence": 0.0,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "cursor",
            "response": "[Cursor Error] Agent timed out after 120s",
            "confidence": 0.0,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "cursor",
            "response": f"[Cursor Error] {str(e)}",
            "confidence": 0.0,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_subagent(
    prompt: str,
    task: str,
    backend: SubagentBackend = "codex",
) -> Dict:
    """
    Route a task to a specific CLI backend as a subagent.

    This is Cato's answer to OpenClaw's ChatGPT-subagent trick.  OpenClaw
    lets users point coding tasks at their ChatGPT plan to leverage OpenAI's
    included usage.  Cato does the same but supports four backends:

    - ``claude``  — Claude Code CLI (uses your Anthropic plan / free tier)
    - ``codex``   — OpenAI Codex CLI (uses your OpenAI plan)
    - ``gemini``  — Google Gemini CLI (uses your Google plan / free tier)
    - ``cursor``  — Cursor Agent CLI (uses your Cursor Pro subscription)

    The active backend is read from ``CatoConfig.subagent_coding_backend``
    (set in ~/.cato/config.yaml) so users can switch without code changes.

    Args:
        prompt: Context or code to send to the subagent.
        task: High-level task description.
        backend: Which CLI to invoke ("claude", "codex", "gemini", "cursor").

    Returns:
        Same dict shape as the individual invoke_* functions.
    """
    _dispatch: dict[str, Any] = {
        "claude": invoke_claude_api,
        "codex": invoke_codex_cli,
        "gemini": invoke_gemini_cli,
        "cursor": invoke_cursor_cli,
    }
    fn = _dispatch.get(backend)
    if fn is None:
        logger.warning("Unknown subagent backend %r, falling back to codex", backend)
        fn = invoke_codex_cli

    logger.info("Subagent routing task to backend=%r", backend)
    return await fn(prompt, task)


# ---------------------------------------------------------------------------
# Genesis pipeline phase routing
# ---------------------------------------------------------------------------

# Primary and fallback CLI workers for each Genesis phase (1-indexed).
# None as fallback means no automatic retry — surface error to user directly.
_GENESIS_PHASE_ROUTING: dict[int, tuple[SubagentBackend, SubagentBackend | None]] = {
    1: ("claude", None),    # Market research — deep research + web tools
    2: ("claude", None),    # SEO + marketing — structured doc generation
    3: ("gemini", "claude"),# Design system — large-context descriptions; fallback if gemini hangs
    4: ("claude", "codex"), # Technical spec — multi-tool orchestration
    5: ("claude", "codex"), # Construction (Ralph loop) — loop controller
    6: ("codex", "claude"), # Test + fix — codex --full-auto fix loop
    7: ("claude", None),    # Deploy + validate — vault access + approval gate
    8: ("claude", None),    # Marketing automation — fan-out coordination
    9: ("claude", None),    # Long-term health — read-only analysis
}

# Per-phase timeouts in seconds.
_GENESIS_PHASE_TIMEOUTS: dict[int, float] = {
    1: 180.0,
    2: 120.0,
    3: 120.0,
    4: 150.0,
    5: 600.0,
    6: 300.0,
    7: 240.0,
    8: 120.0,
    9:  60.0,
}


async def invoke_for_genesis_phase(
    phase: int,
    context: str,
    business_id: str,
) -> Dict:
    """
    Route a Genesis pipeline phase to its designated CLI worker.

    Looks up the primary backend for *phase* in ``_GENESIS_PHASE_ROUTING``,
    calls ``invoke_subagent``, and automatically retries on the fallback
    backend if the primary returns ``degraded=True``.  If both primary and
    fallback degrade, the degraded fallback result is returned and the caller
    is responsible for Andon Cord escalation.

    Args:
        phase: Genesis phase number (1-9).
        context: Full prompt context for the phase (research brief, spec, etc.).
        business_id: Slug identifier for the business build (used in task label).

    Returns:
        Same dict shape as ``invoke_subagent``:
        {"model", "response", "confidence", "latency_ms", "degraded", "source"}

    Raises:
        ValueError: If *phase* is not in the range 1-9.
    """
    if phase not in _GENESIS_PHASE_ROUTING:
        raise ValueError(
            f"Invalid Genesis phase {phase!r}. Must be an integer 1-9."
        )

    primary, fallback = _GENESIS_PHASE_ROUTING[phase]
    timeout = _GENESIS_PHASE_TIMEOUTS[phase]
    task_label = f"genesis:{business_id}:phase{phase}"

    logger.info(
        "Genesis phase %d routing to primary=%r (fallback=%r, timeout=%.0fs) business=%r",
        phase, primary, fallback, timeout, business_id,
    )

    import asyncio as _asyncio
    try:
        result = await _asyncio.wait_for(
            invoke_subagent(context, task_label, backend=primary),
            timeout=timeout,
        )
    except _asyncio.TimeoutError:
        logger.warning(
            "Genesis phase %d primary=%r timed out after %.0fs",
            phase, primary, timeout,
        )
        result = {
            "model": primary, "response": f"[Timeout] phase {phase} exceeded {timeout:.0f}s",
            "confidence": 0.0, "latency_ms": timeout * 1000, "degraded": True, "source": "timeout",
        }

    if result.get("degraded") and fallback is not None:
        logger.warning(
            "Genesis phase %d primary=%r degraded, retrying with fallback=%r",
            phase, primary, fallback,
        )
        try:
            result = await _asyncio.wait_for(
                invoke_subagent(context, task_label, backend=fallback),
                timeout=timeout,
            )
        except _asyncio.TimeoutError:
            logger.error(
                "Genesis phase %d fallback=%r also timed out — Andon Cord needed",
                phase, fallback,
            )
            result = {
                "model": fallback, "response": f"[Timeout] phase {phase} fallback exceeded {timeout:.0f}s",
                "confidence": 0.0, "latency_ms": timeout * 1000, "degraded": True, "source": "timeout",
            }
        if result.get("degraded"):
            logger.error(
                "Genesis phase %d fallback=%r also degraded — Andon Cord needed",
                phase, fallback,
            )

    return result
