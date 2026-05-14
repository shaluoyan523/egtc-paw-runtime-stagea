from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .artifact_store import ArtifactStore
from typing import Any

from .models import ActorIdentity, CapabilityToken, NodeCapsule, WorkerResult
from .sandbox import SandboxRuntime


class CodexExecWrapper:
    """Stage A worker launcher.

    `executor_kind="subprocess"` runs a local command.
    `executor_kind="codex_cli"` launches a real `codex exec --json` session.
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        actor: ActorIdentity,
        token: CapabilityToken,
    ) -> None:
        self.artifact_store = artifact_store
        self.actor = actor
        self.token = token
        self.sandbox = SandboxRuntime()

    def run(
        self,
        node: NodeCapsule,
        cwd: Path,
        role: str = "worker",
        run_id: str | None = None,
    ) -> WorkerResult:
        cwd.mkdir(parents=True, exist_ok=True)
        agent_id = f"{role}-{uuid.uuid4().hex[:12]}"
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        spec = self.sandbox.prepare(node)
        command = self._build_command(node, cwd, spec.codex_sandbox)
        start_time = time.time()
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        timed_out = False
        sandbox_events = self.sandbox.start_events(run_id, node, agent_id, spec, cwd)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=spec.resource_limits.wall_time_sec,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stderr += f"\nSandbox timeout after {spec.resource_limits.wall_time_sec}s\n"
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        sandbox_events.extend(
            self.sandbox.finish_events(run_id, node, agent_id, exit_code, timed_out)
        )
        resource_report = self.sandbox.report(
            node,
            start_time,
            usage_before,
            usage_after,
            timed_out,
            command_count=spec.command_count,
        )
        parsed_events: list[dict[str, Any]] = []
        event_lines: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"type": "log", "stream": "stdout", "message": line}
            if isinstance(event, dict):
                event.setdefault("agent_id", agent_id)
                if role == "worker":
                    event.setdefault("worker_id", agent_id)
                elif role == "overlooker":
                    event.setdefault("overlooker_id", agent_id)
                parsed_events.append(event)
                event_lines.append(json.dumps(event, sort_keys=True))

        metadata = {"node_id": node.node_id, f"{role}_id": agent_id}
        event_ref = self.artifact_store.put_bytes(
            ("\n".join(event_lines) + "\n").encode("utf-8"),
            "application/jsonl",
            {"kind": f"{role}_events", **metadata},
            self.actor,
            self.token,
        )
        stdout_ref = self.artifact_store.put_bytes(
            stdout.encode("utf-8"),
            "text/plain",
            {"kind": f"{role}_stdout", **metadata},
            self.actor,
            self.token,
        )
        stderr_ref = self.artifact_store.put_bytes(
            stderr.encode("utf-8"),
            "text/plain",
            {"kind": f"{role}_stderr", **metadata},
            self.actor,
            self.token,
        )
        sandbox_event_ref = self.artifact_store.put_bytes(
            (
                "\n".join(json.dumps(event.__dict__, sort_keys=True) for event in sandbox_events)
                + "\n"
            ).encode("utf-8"),
            "application/jsonl",
            {"kind": f"{role}_sandbox_events", **metadata},
            self.actor,
            self.token,
        )
        resource_report_ref = self.artifact_store.put_json(
            resource_report,
            {"kind": f"{role}_resource_report", **metadata},
            self.actor,
            self.token,
        )
        return WorkerResult(
            worker_id=agent_id,
            status="submitted",
            exit_code=exit_code,
            event_refs=[event_ref],
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            parsed_events=parsed_events,
            sandbox_event_refs=[sandbox_event_ref],
            resource_report_ref=resource_report_ref,
        )

    def _build_command(self, node: NodeCapsule, cwd: Path, codex_sandbox: str) -> list[str]:
        if node.executor_kind == "subprocess":
            if not node.command:
                raise ValueError("subprocess node requires command")
            return node.command
        if node.executor_kind != "codex_cli":
            raise ValueError(f"unsupported executor_kind: {node.executor_kind}")

        prompt = node.prompt or node.goal
        codex_binary = node.codex_binary or self._find_codex_binary()
        return [
            codex_binary,
            "-a",
            "never",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-C",
            str(cwd.resolve()),
            "-s",
            codex_sandbox,
            prompt,
        ]

    def _find_codex_binary(self) -> str:
        configured = os.environ.get("CODEX_BIN")
        if configured:
            return configured
        found = shutil.which("codex")
        if found:
            return found
        candidates = [
            "/home/batchcom/.windsurf-server/extensions/openai.chatgpt-26.422.71525/bin/linux-x86_64/codex",
            "/home/batchcom/.windsurf-server/extensions/openai.chatgpt-26.409.20454-linux-x64/bin/linux-x86_64/codex",
        ]
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
        raise FileNotFoundError("codex binary not found; set CODEX_BIN")
