"""Mission routing through the existing bounded, tool-free cognitive worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from ironman.learning.workers import CodexWorker, dump


class WorkerInterrupted(RuntimeError):
    def __init__(self, status):
        self.worker_status = status
        super().__init__(f"WORKER_{status}")


class MissionWorker(CodexWorker):
    """One explicit catalog configuration, a fresh process per bounded call.

    Catalog priority selects the installed CLI's first visible model. It is not
    evidence of comparative model quality or multi-model routing. Authentication
    stays with the installed CLI; this adapter never opens credential files.

    ``ask(..., images=[local_path, ...])`` attaches ordered image bytes through
    the CLI's native image input. Inherited records bind each saved image by
    hash; attachment input does not grant a worker file-reading tools.
    """

    def __init__(self, directory: Path, max_calls: int, timeout: int = 240,
                 cancel_check=None, lifecycle=None, model=None,
                 reasoning_effort=None):
        super().__init__(directory, max_calls, timeout)
        self.cancel_check = cancel_check
        self.lifecycle = lifecycle
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("CODEX_CLI_UNAVAILABLE")
        catalog = subprocess.run(
            [executable, "debug", "models"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if catalog.returncode:
            raise RuntimeError(f"CODEX_MODEL_DISCOVERY_EXIT_{catalog.returncode}")
        visible = [entry for entry in json.loads(catalog.stdout)["models"]
                   if entry.get("visibility") == "list"]
        if not visible:
            raise RuntimeError("CODEX_MODEL_CATALOG_EMPTY")
        if model is None:
            selected = min(visible, key=lambda entry: entry["priority"])
            selection_source = "codex debug models visible catalog priority"
        else:
            selected = next((entry for entry in visible if entry["slug"] == model), None)
            if selected is None:
                raise RuntimeError("REQUESTED_CODEX_MODEL_UNAVAILABLE")
            selection_source = "explicit runtime model configuration"
        effort = reasoning_effort or selected["default_reasoning_level"]
        supported = {item["effort"] for item in selected.get("supported_reasoning_levels", [])}
        if supported and effort not in supported:
            raise RuntimeError("REQUESTED_REASONING_EFFORT_UNAVAILABLE")
        self.configuration = {
            "adapter": "installed_codex_cli", "route": "single_catalog_model",
            "model": selected["slug"],
            "reasoning_effort": effort,
            "selection_source": selection_source,
            "configuration_source": "explicit CLI arguments; user config ignored",
            "model_access_verified": False,
            "sandbox": "read-only", "tools": "disabled", "ephemeral": True,
            "timeout_seconds": timeout, "max_calls": max_calls,
        }

    def _command(self, work):
        command = super()._command(work)
        command[-1:-1] = ["--model", self.configuration["model"], "-c",
                          "model_reasoning_effort=" + json.dumps(
                              self.configuration["reasoning_effort"])]
        return command

    def _notify(self, stage, record):
        if stage == 'prepared':
            record['configuration'] = dict(self.configuration)
        super()._notify(stage, record)

    def _execute(self, command, prompt, record):
        record["configuration"] = dict(self.configuration)
        record["worker_pid"] = None
        if self.cancel_check and self.cancel_check():
            raise WorkerInterrupted("CANCELLED")
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        record["worker_pid"] = process.pid
        record["process_status"] = "RUNNING"
        self._record(record)
        deadline = time.monotonic() + self.timeout
        first_input = prompt
        try:
            self._notify('started', record)
            while True:
                if self.cancel_check and self.cancel_check():
                    raise WorkerInterrupted("CANCELLED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerInterrupted("TIMED_OUT")
                try:
                    stdout, stderr = process.communicate(
                        input=first_input, timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    first_input = None
        except BaseException:
            process.kill()
            stdout, _ = process.communicate(timeout=5)
            self._usage_from_events(stdout, record)
            raise
        finally:
            record["process_status"] = ("EXITED" if process.returncode is not None
                                        else "EXIT_UNCONFIRMED")
            record["returncode"] = process.returncode
        self._usage_from_events(stdout, record)
        if process.returncode:
            # stderr can contain host diagnostics. Persist the concrete exit code,
            # not unfiltered host content or reasoning-bearing JSONL output.
            raise RuntimeError(f"CODEX_WORKER_EXIT_{process.returncode}")
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item_type = event.get("item", {}).get("type")
            if item_type and item_type not in ("agent_message", "reasoning"):
                raise RuntimeError("WORKER_ISOLATION_VIOLATION")
        self.configuration["model_access_verified"] = True
        record["configuration"]["model_access_verified"] = True
        return subprocess.CompletedProcess(command, process.returncode, stdout, "")

    @staticmethod
    def _usage_from_events(stdout, record):
        record["usage_reported"] = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                record["usage"] = event.get("usage", {})
                record["usage_reported"] = True
