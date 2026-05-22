from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from cato.config import CatoConfig
from cato.platform import get_data_dir

from .models import PHASE_NAMES, EmpireRun, PhasePromptBundle, WorkerAssignment, WorkerResult
from .phase_library import EmpirePhaseLibrary
from .phase_validation import EmpirePhaseValidator
from .store import PipelineStore
from .workers import WorkerAdapter, get_worker_registry


class PhaseRouter:
    DEFAULT_PHASE_WORKERS: dict[int, str] = {
        1: "claude",
        2: "claude",
        3: "gemini",
        4: "claude",
        5: "claude",
        6: "codex",
        7: "claude",
        8: "claude",
        9: "claude",
    }

    def worker_for(self, phase: int, override: Optional[str] = None) -> str:
        if override:
            return override
        if phase not in self.DEFAULT_PHASE_WORKERS:
            raise ValueError(f"Unknown phase: {phase}")
        return self.DEFAULT_PHASE_WORKERS[phase]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:80] or f"business-{uuid.uuid4().hex[:8]}"


class EmpireRuntime:
    def __init__(
        self,
        config: Optional[CatoConfig] = None,
        *,
        store: Optional[PipelineStore] = None,
        worker_registry: Optional[dict[str, WorkerAdapter]] = None,
    ) -> None:
        self._cfg = config or CatoConfig.load()
        self._pipeline_root = Path(
            getattr(self._cfg, "pipeline_root_dir", "") or (get_data_dir() / "businesses")
        ).expanduser().resolve()
        self._pipeline_root.mkdir(parents=True, exist_ok=True)
        self._store = store or PipelineStore(self._pipeline_root / "empire.db")
        self._workers = worker_registry or get_worker_registry()
        self._router = PhaseRouter()
        self._phase_library = EmpirePhaseLibrary()
        self._validator = EmpirePhaseValidator()

    @property
    def pipeline_root(self) -> Path:
        return self._pipeline_root

    def business_dir(self, business_slug: str) -> Path:
        return self._pipeline_root / business_slug

    def create_business_scaffold(self, idea: str, business_slug: Optional[str] = None) -> EmpireRun:
        business_slug = business_slug or slugify(idea)
        root = self.business_dir(business_slug)
        dirs = [
            root,
            root / "phase_1_outputs",
            root / "phase_2_outputs",
            root / "phase_3_outputs",
            root / "brand",
            root / "phase_4_outputs",
            root / "website",
            root / "worktrees",
            root / "checkpoints",
            root / "prompts",
            root / "logs",
            root / "reports",
            root / "audit",
            root / "deployment",
        ]
        for path in dirs:
            path.mkdir(parents=True, exist_ok=True)

        run_id = f"run-{uuid.uuid4().hex[:12]}"
        manifest = {
            "idea": idea,
            "business_slug": business_slug,
            "business_dir": str(root),
            "run_id": run_id,
            "status": "CREATED",
            "current_phase": 0,
            "phase_workers": self._router.DEFAULT_PHASE_WORKERS,
            "phase_specs": {
                phase: {
                    "worker": spec.worker,
                    "model_tier": spec.model_tier,
                    "output_dir": spec.output_dir,
                    "support_workers": spec.support_workers,
                    "required_outputs": spec.required_outputs,
                }
                for phase, spec in self._phase_library._specs.items()
            },
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        run = self._store.create_run(
            run_id=run_id,
            business_slug=business_slug,
            idea=idea,
            business_dir=root,
            metadata=manifest,
        )
        self._sync_manifest(run.business_dir, manifest)
        self._write_active_tasks()
        return run

    def get_run(self, business_slug: str) -> Optional[EmpireRun]:
        run = self._store.get_run_by_slug(business_slug)
        if run is not None:
            return run

        manifest_path = self.business_dir(business_slug) / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        return EmpireRun(
            run_id=manifest.get("run_id", f"scaffold-{business_slug}"),
            business_slug=manifest.get("business_slug", business_slug),
            idea=manifest.get("idea", business_slug),
            business_dir=self.business_dir(business_slug),
            status=manifest.get("status", "SCAFFOLDED"),
            current_phase=int(manifest.get("current_phase", 0) or 0),
            metadata=manifest,
        )

    def list_runs(self) -> list[EmpireRun]:
        return self._store.list_runs()

    def _sync_manifest(self, business_dir: Path, manifest: dict[str, Any]) -> None:
        (business_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

    def _set_run_state(
        self,
        run: EmpireRun,
        *,
        status: Optional[str] = None,
        current_phase: Optional[int] = None,
        metadata_updates: Optional[dict[str, Any]] = None,
    ) -> EmpireRun:
        merged = dict(run.metadata or {})
        if metadata_updates:
            for key, value in metadata_updates.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    nested = dict(merged[key])
                    nested.update(value)
                    merged[key] = nested
                else:
                    merged[key] = value
        if status is not None:
            merged["status"] = status
        if current_phase is not None:
            merged["current_phase"] = current_phase
        self._store.update_run_status(
            run.run_id,
            status=status,
            current_phase=current_phase,
            metadata=merged,
        )
        refreshed = self._store.get_run(run.run_id)
        self._sync_manifest(refreshed.business_dir, refreshed.metadata)
        return refreshed

    def build_phase_prompt(
        self,
        *,
        business_slug: str,
        phase: int,
    ) -> PhasePromptBundle:
        run = self.get_run(business_slug)
        if run is None:
            raise KeyError(f"Unknown business: {business_slug}")
        return self._phase_library.build_prompt(run, phase)

    async def dispatch_phase(
        self,
        *,
        business_slug: str,
        phase: int,
        prompt: Optional[str] = None,
        worker_override: Optional[str] = None,
        workdir: Optional[Path] = None,
        timeout_sec: float = 300.0,
    ) -> WorkerResult:
        run = self.get_run(business_slug)
        if run is None:
            raise KeyError(f"Unknown business: {business_slug}")

        bundle = self._phase_library.build_prompt(run, phase)
        worker_name = worker_override or bundle.spec.worker or self._router.worker_for(phase)
        worker = self._workers.get(worker_name)
        if worker is None:
            raise ValueError(f"Worker adapter not configured: {worker_name}")

        prompt_dir = run.business_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        phase_name = PHASE_NAMES.get(phase, f"phase-{phase}")
        prompt_path = prompt_dir / f"phase-{phase}-{worker_name}.md"
        prompt_text = prompt or bundle.prompt
        prompt_path.write_text(prompt_text, encoding="utf-8")

        task_id = f"task-{phase}-{business_slug}-{uuid.uuid4().hex[:8]}"
        default_workdir = run.business_dir / bundle.spec.output_dir
        actual_workdir = (workdir or default_workdir).resolve()
        assignment = WorkerAssignment(
            task_id=task_id,
            run_id=run.run_id,
            business_slug=business_slug,
            phase=phase,
            prompt=prompt_text,
            worker=worker_name,
            cwd=actual_workdir,
            timeout_sec=timeout_sec,
            prompt_file=prompt_path,
            metadata={
                "phase_name": phase_name,
                "model_tier": bundle.spec.model_tier,
                "required_outputs": bundle.spec.required_outputs,
                "requirements": [
                    {
                        "type": req.type,
                        "script": str(req.script) if req.script else None,
                        "args": req.args,
                        "exit_code_0_required": req.exit_code_0_required,
                        "note": req.note,
                    }
                    for req in bundle.requirements
                ],
            },
        )

        self._store.add_task(
            task_id=task_id,
            run_id=run.run_id,
            business_slug=business_slug,
            phase=phase,
            worker=worker_name,
            prompt_file=prompt_path,
            workdir=actual_workdir,
            note=f"Dispatching {phase_name} to {worker_name}",
        )
        self._store.update_run_status(run.run_id, status="RUNNING", current_phase=phase)
        self._write_active_tasks()

        result = await worker.run(assignment)
        self._store.update_task(
            task_id,
            status="done" if result.success else "failed",
            note=(result.response[:240] if result.response else result.error[:240]),
            result={
                "success": result.success,
                "response": result.response,
                "error": result.error,
                "latency_ms": result.latency_ms,
                "worker": result.worker,
            },
        )
        self._store.update_run_status(
            run.run_id,
            status="RUNNING" if result.success else "FAILED",
            current_phase=phase,
        )
        self._write_active_tasks()
        return result

    async def _run_requirement(
        self,
        *,
        run: EmpireRun,
        phase: int,
        requirement: Any,
    ) -> dict[str, Any]:
        started = time.time()
        task_id = f"task-script-{phase}-{run.business_slug}-{uuid.uuid4().hex[:8]}"
        script = requirement.script
        if script is None:
            return {
                "task_id": task_id,
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "missing script path",
                "duration_ms": 0,
            }

        script_path = Path(script)

        # SECURITY: Validate script path is within trusted directories.
        # Scripts must be relative paths within the business directory or the
        # built-in pipeline/scripts directory co-located with this file.
        trusted_roots = [
            run.business_dir.resolve(),
            (Path(__file__).parent / "scripts").resolve(),
        ]
        if not script_path.is_absolute():
            resolved_script = (run.business_dir / script_path).resolve()
        else:
            resolved_script = script_path.resolve()
        resolved_str = str(resolved_script)
        if not any(resolved_script.is_relative_to(root) for root in trusted_roots):
            raise ValueError(
                f"Script path '{resolved_script}' is outside trusted directories. "
                f"Scripts must be relative to business_dir or pipeline/scripts/."
            )
        script_path = resolved_script

        argv: list[str]
        if script_path.suffix.lower() == ".py":
            argv = [sys.executable, str(script_path), *requirement.args]
        else:
            argv = [str(script_path), *requirement.args]

        log_dir = run.business_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._store.add_task(
            task_id=task_id,
            run_id=run.run_id,
            business_slug=run.business_slug,
            phase=phase,
            worker="script",
            prompt_file=script_path,
            workdir=run.business_dir,
            note=f"Running {script_path.name}",
        )
        self._write_active_tasks()

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(run.business_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            exit_code = proc.returncode or 0
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            exit_code = -1
            stdout = ""
            stderr = str(exc)

        duration_ms = (time.time() - started) * 1000
        result = {
            "task_id": task_id,
            "script": str(script_path),
            "args": requirement.args,
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "required": requirement.exit_code_0_required,
        }
        self._store.update_task(
            task_id,
            status="done" if exit_code == 0 else "failed",
            note=(stdout or stderr or script_path.name)[:240],
            result=result,
        )
        (log_dir / f"phase-{phase}-{script_path.stem}.log").write_text(
            "\n".join(
                [
                    f"script: {script_path}",
                    f"args: {' '.join(requirement.args)}",
                    f"exit_code: {exit_code}",
                    "",
                    "[stdout]",
                    stdout,
                    "",
                    "[stderr]",
                    stderr,
                ]
            ),
            encoding="utf-8",
        )
        self._write_active_tasks()
        return result

    async def execute_phase(
        self,
        *,
        business_slug: str,
        phase: int,
        prompt: Optional[str] = None,
        worker_override: Optional[str] = None,
        workdir: Optional[Path] = None,
        timeout_sec: float = 300.0,
        auto_requirements: bool = True,
    ) -> dict[str, Any]:
        run = self.get_run(business_slug)
        if run is None:
            raise KeyError(f"Unknown business: {business_slug}")

        bundle = self.build_phase_prompt(business_slug=business_slug, phase=phase)
        result = await self.dispatch_phase(
            business_slug=business_slug,
            phase=phase,
            prompt=prompt or bundle.prompt,
            worker_override=worker_override,
            workdir=workdir,
            timeout_sec=timeout_sec,
        )
        requirement_results: list[dict[str, Any]] = []
        if result.success and auto_requirements:
            refreshed = self.get_run(business_slug)
            if refreshed is None:
                raise KeyError(f"Unknown business: {business_slug}")
            run = refreshed
            for requirement in bundle.requirements:
                req_result = await self._run_requirement(
                    run=run,
                    phase=phase,
                    requirement=requirement,
                )
                requirement_results.append(req_result)
                if requirement.exit_code_0_required and not req_result["success"]:
                    result.success = False
                    result.degraded = True
                    result.error = (
                        f"{Path(req_result['script']).name} failed with exit code "
                        f"{req_result['exit_code']}"
                    )
                    break

        validation = self._validator.validate(run, bundle.spec) if result.success else None
        if validation is not None and not validation.success:
            result.success = False
            result.degraded = True
            result.error = "; ".join(validation.errors[:3])

        run = self.get_run(business_slug)
        if run is None:
            raise KeyError(f"Unknown business: {business_slug}")

        phase_history = dict(run.metadata.get("phase_history", {}))
        phase_history[str(phase)] = {
            "worker": result.worker,
            "success": result.success,
            "error": result.error,
            "required_outputs": bundle.spec.required_outputs,
            "requirement_results": requirement_results,
            "validation": {
                "success": validation.success if validation is not None else None,
                "errors": validation.errors if validation is not None else [],
                "warnings": validation.warnings if validation is not None else [],
                "checked_paths": validation.checked_paths if validation is not None else [],
            },
        }
        new_status = "RUNNING" if result.success else "FAILED"
        run = self._set_run_state(
            run,
            status=new_status,
            current_phase=phase,
            metadata_updates={"phase_history": phase_history},
        )
        return {
            "phase": phase,
            "worker_result": result,
            "requirement_results": requirement_results,
            "validation": validation,
            "run_status": run.status,
        }

    def _phase_is_complete(self, run: EmpireRun, phase: int) -> bool:
        """Return True if *phase* has a stored checkpoint that records success.

        A phase is considered complete when ``PipelineStore.get_phase_checkpoint``
        returns a dict with ``"success": True``.  Falls back to ``phase_history``
        in the run metadata for backwards compatibility with runs created before
        the checkpoint column existed.
        """
        # Primary: new checkpoint_json column
        checkpoint = self._store.get_phase_checkpoint(run.run_id, phase)
        if checkpoint is not None:
            return bool(checkpoint.get("success", False))
        # Fallback: legacy phase_history in metadata_json
        phase_history: dict = run.metadata.get("phase_history", {})
        entry = phase_history.get(str(phase))
        if entry is not None:
            return bool(entry.get("success", False))
        return False

    async def run_pipeline(
        self,
        *,
        business_slug: str,
        start_phase: int = 1,
        through_phase: int = 7,
        stop_for_approval: bool = True,
        timeout_sec: float = 300.0,
        skip_completed: bool = False,
    ) -> dict[str, Any]:
        run = self.get_run(business_slug)
        if run is None:
            raise KeyError(f"Unknown business: {business_slug}")

        summaries: list[dict[str, Any]] = []
        for phase in range(start_phase, through_phase + 1):
            if skip_completed and self._phase_is_complete(run, phase):
                # Re-fetch so caller sees fresh state; synthesise a minimal summary.
                run = self.get_run(business_slug) or run  # type: ignore[assignment]
                summaries.append(
                    {
                        "phase": phase,
                        "worker_result": type(
                            "WorkerResult",
                            (),
                            {"success": True, "worker": "skipped", "response": "", "error": None, "latency_ms": 0.0, "degraded": False},
                        )(),
                        "requirement_results": [],
                        "validation": None,
                        "run_status": run.status,
                        "skipped": True,
                    }
                )
                continue
            summary = await self.execute_phase(
                business_slug=business_slug,
                phase=phase,
                timeout_sec=timeout_sec,
            )
            summaries.append(summary)
            worker_result: WorkerResult = summary["worker_result"]
            if not worker_result.success:
                return {
                    "business_slug": business_slug,
                    "status": "FAILED",
                    "completed_phases": [item["phase"] for item in summaries if item["worker_result"].success],
                    "phase_summaries": summaries,
                    "stopped_at_phase": phase,
                }
            if phase == 7 and stop_for_approval and through_phase > 7:
                latest = self.get_run(business_slug)
                if latest is not None:
                    latest = self._set_run_state(
                        latest,
                        status="AWAITING_APPROVAL",
                        current_phase=7,
                        metadata_updates={"approval_gate": {"phase": 7, "approved": False}},
                    )
                return {
                    "business_slug": business_slug,
                    "status": "AWAITING_APPROVAL",
                    "completed_phases": [item["phase"] for item in summaries],
                    "phase_summaries": summaries,
                    "stopped_at_phase": 7,
                }

        latest = self.get_run(business_slug)
        if latest is not None:
            latest = self._set_run_state(
                latest,
                status="COMPLETED",
                current_phase=through_phase,
            )
        return {
            "business_slug": business_slug,
            "status": "COMPLETED",
            "completed_phases": [item["phase"] for item in summaries],
            "phase_summaries": summaries,
            "stopped_at_phase": through_phase,
        }

    def tasks_for(self, business_slug: str) -> list[dict]:
        run = self.get_run(business_slug)
        if run is None:
            return []
        return self._store.list_tasks(run.run_id)

    def _write_active_tasks(self) -> None:
        tasks = self._store.list_tasks()
        active = []
        for task in tasks:
            active.append(
                {
                    "id": task["task_id"],
                    "agent": task["worker"],
                    "business": task["business_slug"],
                    "phase": task["phase"],
                    "status": task["status"],
                    "workdir": task["workdir"],
                    "prompt_file": task["prompt_file"],
                    "note": task["note"],
                }
            )
        (self._pipeline_root / "active-tasks.json").write_text(
            json.dumps(active, indent=2),
            encoding="utf-8",
        )
