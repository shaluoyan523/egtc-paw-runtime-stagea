from __future__ import annotations

import shutil
import time
import uuid
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore
from .codex_wrapper import CodexExecWrapper
from .event_log import EventLog
from .evidence import EvidenceCollector
from .identity import IdentityService
from .models import (
    EvidenceBundle,
    NodeCapsule,
    NodeState,
    OverlookerReport,
    ValidatorReport,
    WorkerResult,
    to_plain_dict,
)
from .overlooker import CodexOverlooker
from .validators import DeterministicValidator
from .workspace_diff import diff_snapshots, snapshot_workspace


TERMINAL_STATUSES = {
    "NODE_ACCEPTED",
    "NODE_REJECTED",
    "NODE_BLOCKED",
    "NODE_ABORTED",
}


@dataclass
class GraphRunSpec:
    graph_id: str
    nodes: list[NodeCapsule]
    edges: list[tuple[str, str]]
    max_parallelism: int = 2
    max_attempts: int = 1
    retry_budget: int = 0
    max_same_failure_retries: int = 2
    overlooker_mode: str = "deterministic"


@dataclass
class GraphNodeRecord:
    node_id: str
    status: str = "NODE_PLANNED"
    attempts: int = 0
    depends_on: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    read_only: bool = True
    write_paths: list[str] = field(default_factory=list)
    workspace: str | None = None
    current_worker_id: str | None = None
    final_state: str | None = None
    failure_code: str | None = None
    failure_counts: dict[str, int] = field(default_factory=dict)
    evidence_ref: str | None = None
    resource_report_ref: str | None = None
    sandbox_events_ref: str | None = None
    current_workspace: str | None = None
    accepted_workspace: str | None = None
    fork_source_node_id: str | None = None
    fork_source_workspace: str | None = None
    fork_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NodeExecutionResult:
    node_id: str
    attempt: int
    workspace: str
    final_state: str
    worker_result: WorkerResult
    evidence: EvidenceBundle
    validator_reports: list[ValidatorReport]
    overlooker_report: OverlookerReport
    workspace_diff: dict[str, list[str]]


@dataclass(frozen=True)
class WorkspaceForkPlan:
    attempt: int
    workspace: Path
    source_node_id: str | None
    source_workspace: Path | None
    candidate_node_ids: list[str]
    reason: str


class GraphRuntime:
    """Phase D DAG scheduler built on the Stage A/C node execution chain."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = IdentityService()
        self.runtime_actor = self.identity.actor("runtime-phased", "runtime")
        self.runtime_token = self.identity.issue_token(
            self.runtime_actor,
            ["artifact:read", "artifact:write"],
        )
        self.artifacts = ArtifactStore(self.root / "artifacts", self.identity)
        self.event_log = EventLog(self.root / "events.sqlite3")
        self.wrapper = CodexExecWrapper(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.collector = EvidenceCollector(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.validator = DeterministicValidator(self.artifacts)
        self.overlooker = CodexOverlooker(
            self.artifacts, self.runtime_actor, self.runtime_token, self.wrapper
        )

    def run_graph(
        self,
        spec: GraphRunSpec,
        run_id: str | None = None,
        stop_after_accepted: int | None = None,
    ) -> dict[str, Any]:
        self._validate_spec(spec)
        run_id = run_id or f"graph-{uuid.uuid4().hex[:12]}"
        records = self._initial_records(spec)
        return self._run_loop(spec, run_id, records, stop_after_accepted)

    def resume_graph(
        self,
        run_id: str,
        stop_after_accepted: int | None = None,
    ) -> dict[str, Any]:
        checkpoint = self._read_checkpoint(run_id)
        spec = self._spec_from_checkpoint(checkpoint)
        records = {
            node_id: GraphNodeRecord(**record)
            for node_id, record in checkpoint["nodes"].items()
        }
        for record in records.values():
            if record.status == "WORKER_RUNNING":
                record.status = "NODE_PLANNED"
                record.current_worker_id = None
        return self._run_loop(spec, run_id, records, stop_after_accepted)

    def _run_loop(
        self,
        spec: GraphRunSpec,
        run_id: str,
        records: dict[str, GraphNodeRecord],
        stop_after_accepted: int | None,
    ) -> dict[str, Any]:
        nodes = {node.node_id: node for node in spec.nodes}
        active: dict[Future[NodeExecutionResult], str] = {}
        active_writer: str | None = None
        max_observed_parallelism = 0
        write_lock_waits: list[dict[str, Any]] = []
        retry_events: list[dict[str, Any]] = []
        pause_requested = False

        self._record(run_id, spec.graph_id, "GraphRunStarted", {"graph_id": spec.graph_id})
        self._write_checkpoint(run_id, spec, records, "running")

        with ThreadPoolExecutor(max_workers=max(1, spec.max_parallelism)) as executor:
            while True:
                accepted_count = sum(
                    1 for record in records.values() if record.status == "NODE_ACCEPTED"
                )
                if (
                    stop_after_accepted is not None
                    and accepted_count >= stop_after_accepted
                    and not active
                ):
                    pause_requested = True
                    break

                made_progress = False
                while len(active) < spec.max_parallelism:
                    candidate = self._next_runnable(records)
                    if candidate is None:
                        break
                    record = records[candidate]
                    if not self._can_start(record, active, active_writer):
                        wait_event = {
                            "node_id": record.node_id,
                            "active_writer": active_writer,
                            "active_nodes": sorted(active.values()),
                            "write_paths": record.write_paths,
                        }
                        if not write_lock_waits or write_lock_waits[-1] != wait_event:
                            write_lock_waits.append(wait_event)
                            self._record(run_id, record.node_id, "WriteConflictWait", wait_event)
                        break
                    record.status = "WORKER_RUNNING"
                    record.attempts += 1
                    fork_plan = self._select_fork_plan(
                        run_id,
                        nodes[candidate],
                        record,
                        records,
                        spec.overlooker_mode,
                    )
                    if not record.read_only:
                        active_writer = record.node_id
                        self._record(
                            run_id,
                            record.node_id,
                            "WriteLockAcquired",
                            {"write_paths": record.write_paths},
                        )
                    self._record(
                        run_id,
                        record.node_id,
                        "NodeStateChanged",
                        {"state": "WORKER_RUNNING", "attempt": record.attempts},
                    )
                    future = executor.submit(
                        self._execute_node,
                        run_id,
                        nodes[candidate],
                        spec.overlooker_mode,
                        fork_plan,
                    )
                    active[future] = candidate
                    max_observed_parallelism = max(max_observed_parallelism, len(active))
                    made_progress = True

                if not active:
                    if self._all_terminal(records):
                        break
                    if not made_progress:
                        self._mark_blocked(run_id, records)
                        break
                    continue

                done, _pending = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = active.pop(future)
                    record = records[node_id]
                    if active_writer == node_id:
                        active_writer = None
                        self._record(
                            run_id,
                            node_id,
                            "WriteLockReleased",
                            {"write_paths": record.write_paths},
                        )
                    try:
                        result = future.result()
                    except Exception as exc:  # pragma: no cover - defensive runtime path
                        result = None
                        failure_code = type(exc).__name__
                        record.failure_code = failure_code
                        record.failure_counts[failure_code] = (
                            record.failure_counts.get(failure_code, 0) + 1
                        )
                        self._record(
                            run_id,
                            node_id,
                            "NodeExecutionErrored",
                            {"failure_code": failure_code, "message": str(exc)},
                        )
                    if result is not None:
                        self._apply_node_result(run_id, record, result)
                    if record.status == "NODE_REJECTED":
                        retry_event = self._maybe_retry(run_id, spec, record)
                        if retry_event:
                            retry_events.append(retry_event)
                    self._write_checkpoint(run_id, spec, records, "running")

        status = "paused" if pause_requested else self._graph_status(records)
        self._write_checkpoint(run_id, spec, records, status)
        summary = {
            "run_id": run_id,
            "graph_id": spec.graph_id,
            "status": status,
            "accepted": status == "accepted",
            "max_parallelism": spec.max_parallelism,
            "max_observed_parallelism": max_observed_parallelism,
            "write_lock_waits": write_lock_waits,
            "retry_events": retry_events,
            "checkpoint_path": str(self._checkpoint_path(run_id)),
            "nodes": {node_id: to_plain_dict(record) for node_id, record in records.items()},
            "events": self.event_log.list_events(run_id),
        }
        self._record(run_id, spec.graph_id, "GraphRunCompleted", summary)
        return summary

    def _execute_node(
        self,
        run_id: str,
        node: NodeCapsule,
        overlooker_mode: str,
        fork_plan: WorkspaceForkPlan,
    ) -> NodeExecutionResult:
        workspace = self._prepare_workspace(node, fork_plan)
        before = snapshot_workspace(workspace)
        worker_result = self.wrapper.run(node, workspace, run_id=run_id)
        self._record(
            run_id,
            node.node_id,
            "NodeStateChanged",
            {"state": "WORKER_SUBMITTED", "worker_id": worker_result.worker_id},
        )
        after = snapshot_workspace(workspace)
        workspace_diff = diff_snapshots(before, after)
        evidence = self.collector.collect(node, worker_result, workspace_diff, workspace)
        self._record(run_id, node.node_id, "EvidenceCollected", to_plain_dict(evidence))
        validator_reports = self.validator.run(evidence, node)
        self._record(
            run_id,
            node.node_id,
            "DeterministicValidationCompleted",
            {"reports": to_plain_dict(validator_reports)},
        )
        if overlooker_mode == "codex":
            overlooker_report = self.overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root / "runs" / run_id / "nodes" / node.node_id / "overlooker",
            )
        else:
            overlooker_report = self._deterministic_review(
                node,
                evidence,
                validator_reports,
                worker_result,
            )
        final_state = (
            NodeState.NODE_ACCEPTED.value
            if overlooker_report.verdict == "pass"
            else NodeState.NODE_REJECTED.value
        )
        return NodeExecutionResult(
            node_id=node.node_id,
            attempt=fork_plan.attempt,
            workspace=str(workspace),
            final_state=final_state,
            worker_result=worker_result,
            evidence=evidence,
            validator_reports=validator_reports,
            overlooker_report=overlooker_report,
            workspace_diff=workspace_diff,
        )

    def _apply_node_result(
        self,
        run_id: str,
        record: GraphNodeRecord,
        result: NodeExecutionResult,
    ) -> None:
        record.current_worker_id = result.worker_result.worker_id
        record.final_state = result.final_state
        record.current_workspace = result.workspace
        record.evidence_ref = result.evidence.evidence_ref.uri
        resource_ref = result.evidence.artifacts.get("resource_report")
        sandbox_ref = result.evidence.artifacts.get("sandbox_events")
        record.resource_report_ref = resource_ref.uri if resource_ref else None
        record.sandbox_events_ref = sandbox_ref.uri if sandbox_ref else None
        validators_passed = all(report.passed for report in result.validator_reports)
        if result.final_state == NodeState.NODE_ACCEPTED.value:
            record.status = "NODE_ACCEPTED"
            record.failure_code = None
            record.workspace = result.workspace
            record.accepted_workspace = result.workspace
        else:
            record.status = "NODE_REJECTED"
            record.failure_code = self._failure_code(result.worker_result, result.validator_reports)
            record.failure_counts[record.failure_code] = (
                record.failure_counts.get(record.failure_code, 0) + 1
            )
        self._record(
            run_id,
            record.node_id,
            "NodeStateChanged",
            {
                "state": record.status,
                "worker_id": record.current_worker_id,
                "validators_passed": validators_passed,
                "overlooker_verdict": result.overlooker_report.verdict,
            },
        )

    def _maybe_retry(
        self,
        run_id: str,
        spec: GraphRunSpec,
        record: GraphNodeRecord,
    ) -> dict[str, Any] | None:
        failure_code = record.failure_code or "unknown_failure"
        repeated = record.failure_counts.get(failure_code, 0)
        if repeated > spec.max_same_failure_retries:
            record.status = "NODE_ABORTED"
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "repeated": repeated,
                "action": "abort_livelock",
            }
            self._record(run_id, record.node_id, "LivelockDetected", event)
            return event
        if record.attempts < spec.max_attempts and spec.retry_budget > 0:
            spec.retry_budget -= 1
            record.status = "NODE_PLANNED"
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "attempts": record.attempts,
                "remaining_retry_budget": spec.retry_budget,
                "action": "retry",
            }
            self._record(run_id, record.node_id, "NodeRetryScheduled", event)
            return event
        return None

    def _deterministic_review(
        self,
        node: NodeCapsule,
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
        worker_result: WorkerResult,
    ) -> OverlookerReport:
        validators_passed = all(report.passed for report in validator_reports)
        can_pass = worker_result.exit_code == 0 and validators_passed and bool(evidence.evidence_ref.uri)
        report = {
            "verdict": "pass" if can_pass else "fail",
            "rationale": (
                "Deterministic Phase D overlooker accepted validator-backed evidence."
                if can_pass
                else "Deterministic Phase D overlooker rejected the node."
            ),
            "evidence_ref": evidence.evidence_ref.uri if can_pass else None,
            "validator_refs": [report.validator_id for report in validator_reports],
        }
        report_ref = self.artifacts.put_json(
            report,
            {"kind": "phased_deterministic_overlooker_report", "node_id": node.node_id},
            self.runtime_actor,
            self.runtime_token,
        )
        return OverlookerReport(
            overlooker_id=f"deterministic-overlooker-{uuid.uuid4().hex[:12]}",
            verdict=str(report["verdict"]),
            rationale=str(report["rationale"]),
            evidence_ref=report["evidence_ref"],
            validator_refs=list(report["validator_refs"]),
            report_ref=report_ref,
            codex_event_refs=[],
        )

    def _initial_records(self, spec: GraphRunSpec) -> dict[str, GraphNodeRecord]:
        dependencies: dict[str, list[str]] = {node.node_id: [] for node in spec.nodes}
        dependents: dict[str, list[str]] = {node.node_id: [] for node in spec.nodes}
        for source, target in spec.edges:
            dependencies[target].append(source)
            dependents[source].append(target)
        return {
            node.node_id: GraphNodeRecord(
                node_id=node.node_id,
                depends_on=sorted(dependencies[node.node_id]),
                dependents=sorted(dependents[node.node_id]),
                read_only=self._is_read_only(node),
                write_paths=self._write_paths(node),
            )
            for node in spec.nodes
        }

    def _select_fork_plan(
        self,
        run_id: str,
        node: NodeCapsule,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
        overlooker_mode: str,
    ) -> WorkspaceForkPlan:
        candidates = [
            parent_id
            for parent_id in record.depends_on
            if records[parent_id].status == "NODE_ACCEPTED"
            and records[parent_id].accepted_workspace
        ]
        source_node_id: str | None = None
        source_workspace: Path | None = None
        if candidates:
            source_node_id = sorted(candidates)[-1]
            source_workspace = Path(records[source_node_id].accepted_workspace or "")
            reason = (
                "retry_from_accepted_dependency"
                if record.attempts > 1
                else "initial_from_accepted_dependency"
            )
        elif node.workspace:
            source_workspace = Path(node.workspace)
            reason = (
                "retry_from_initial_workspace"
                if record.attempts > 1
                else "initial_from_node_workspace"
            )
        else:
            reason = "retry_from_empty_workspace" if record.attempts > 1 else "initial_empty_workspace"

        if source_workspace is not None and not source_workspace.exists():
            source_workspace = None
            source_node_id = None
            reason = "source_workspace_missing_empty_fallback"

        workspace = self._attempt_workspace(run_id, node.node_id, record.attempts)
        event = {
            "node_id": node.node_id,
            "attempt": record.attempts,
            "selected_by": f"{overlooker_mode}_overlooker_policy",
            "source_node_id": source_node_id,
            "source_workspace": str(source_workspace) if source_workspace else None,
            "target_workspace": str(workspace),
            "candidate_node_ids": sorted(candidates),
            "previous_failure_code": record.failure_code,
            "reason": reason,
        }
        record.current_workspace = str(workspace)
        record.fork_source_node_id = source_node_id
        record.fork_source_workspace = str(source_workspace) if source_workspace else None
        record.fork_history.append(event)
        self._record(run_id, node.node_id, "OverlookerForkPointSelected", event)
        return WorkspaceForkPlan(
            attempt=record.attempts,
            workspace=workspace,
            source_node_id=source_node_id,
            source_workspace=source_workspace,
            candidate_node_ids=sorted(candidates),
            reason=reason,
        )

    def _validate_spec(self, spec: GraphRunSpec) -> None:
        if spec.max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")
        if spec.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        node_ids = [node.node_id for node in spec.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Graph node ids must be unique")
        known = set(node_ids)
        for source, target in spec.edges:
            if source not in known or target not in known:
                raise ValueError(f"Unknown graph edge: {source!r} -> {target!r}")
        self._topological_order(spec)

    def _topological_order(self, spec: GraphRunSpec) -> list[str]:
        dependencies: dict[str, set[str]] = {node.node_id: set() for node in spec.nodes}
        dependents: dict[str, set[str]] = {node.node_id: set() for node in spec.nodes}
        for source, target in spec.edges:
            dependencies[target].add(source)
            dependents[source].add(target)
        ready = sorted(node_id for node_id, deps in dependencies.items() if not deps)
        ordered: list[str] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(node_id)
            for child in sorted(dependents[node_id]):
                dependencies[child].remove(node_id)
                if not dependencies[child]:
                    ready.append(child)
                    ready.sort()
        if len(ordered) != len(spec.nodes):
            raise ValueError("Workflow graph must be a DAG")
        return ordered

    def _next_runnable(self, records: dict[str, GraphNodeRecord]) -> str | None:
        for node_id in sorted(records):
            record = records[node_id]
            if record.status != "NODE_PLANNED":
                continue
            if all(records[parent].status == "NODE_ACCEPTED" for parent in record.depends_on):
                return node_id
        return None

    def _can_start(
        self,
        record: GraphNodeRecord,
        active: dict[Future[NodeExecutionResult], str],
        active_writer: str | None,
    ) -> bool:
        if record.read_only:
            return active_writer is None
        return not active and active_writer is None

    def _mark_blocked(self, run_id: str, records: dict[str, GraphNodeRecord]) -> None:
        for record in records.values():
            if record.status == "NODE_PLANNED":
                record.status = "NODE_BLOCKED"
                record.failure_code = "dependency_or_scheduler_deadlock"
                self._record(
                    run_id,
                    record.node_id,
                    "DeadlockDetected",
                    {
                        "depends_on": record.depends_on,
                        "dependency_statuses": {
                            parent: records[parent].status for parent in record.depends_on
                        },
                    },
                )

    def _all_terminal(self, records: dict[str, GraphNodeRecord]) -> bool:
        return all(record.status in TERMINAL_STATUSES for record in records.values())

    def _graph_status(self, records: dict[str, GraphNodeRecord]) -> str:
        if all(record.status == "NODE_ACCEPTED" for record in records.values()):
            return "accepted"
        if any(record.status == "NODE_ABORTED" for record in records.values()):
            return "aborted"
        if any(record.status == "NODE_BLOCKED" for record in records.values()):
            return "blocked"
        if any(record.status == "NODE_REJECTED" for record in records.values()):
            return "rejected"
        return "running"

    def _failure_code(
        self,
        worker_result: WorkerResult,
        validator_reports: list[ValidatorReport],
    ) -> str:
        if worker_result.exit_code != 0:
            return f"worker_exit_{worker_result.exit_code}"
        failed = [report.validator_id for report in validator_reports if not report.passed]
        return failed[0] if failed else "overlooker_rejected"

    def _is_read_only(self, node: NodeCapsule) -> bool:
        return not self._write_paths(node)

    def _write_paths(self, node: NodeCapsule) -> list[str]:
        profile = node.sandbox_profile or {}
        raw = profile.get("allowed_write_paths")
        if isinstance(raw, list):
            return sorted(str(path) for path in raw if str(path))
        if node.phase.lower() == "implementation":
            return ["."]
        return []

    def _attempt_workspace(self, run_id: str, node_id: str, attempt: int) -> Path:
        return self.root / "runs" / run_id / "nodes" / node_id / f"attempt-{attempt}" / "workspace"

    def _prepare_workspace(self, node: NodeCapsule, fork_plan: WorkspaceForkPlan) -> Path:
        workspace = fork_plan.workspace
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        source = fork_plan.source_workspace
        if source is None and node.workspace:
            source = Path(node.workspace)
        if source and source.exists():
            shutil.copytree(source, workspace, dirs_exist_ok=True)
        (workspace / ".egtc_attempt.json").write_text(
            json.dumps(
                {
                    "attempt": fork_plan.attempt,
                    "source_node_id": fork_plan.source_node_id,
                    "source_workspace": str(fork_plan.source_workspace)
                    if fork_plan.source_workspace
                    else None,
                    "candidate_node_ids": fork_plan.candidate_node_ids,
                    "fork_reason": fork_plan.reason,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return workspace

    def _checkpoint_path(self, run_id: str) -> Path:
        return self.root / "checkpoints" / f"{run_id}.json"

    def _write_checkpoint(
        self,
        run_id: str,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        status: str,
    ) -> None:
        checkpoint = {
            "run_id": run_id,
            "status": status,
            "updated_at": time.time(),
            "spec": self._spec_to_plain(spec),
            "nodes": {node_id: asdict(record) for node_id, record in records.items()},
        }
        path = self._checkpoint_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _read_checkpoint(self, run_id: str) -> dict[str, Any]:
        path = self._checkpoint_path(run_id)
        if not path.exists():
            raise FileNotFoundError(f"No checkpoint for run_id {run_id}: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _spec_to_plain(self, spec: GraphRunSpec) -> dict[str, Any]:
        return {
            "graph_id": spec.graph_id,
            "nodes": [to_plain_dict(node) for node in spec.nodes],
            "edges": [list(edge) for edge in spec.edges],
            "max_parallelism": spec.max_parallelism,
            "max_attempts": spec.max_attempts,
            "retry_budget": spec.retry_budget,
            "max_same_failure_retries": spec.max_same_failure_retries,
            "overlooker_mode": spec.overlooker_mode,
        }

    def _spec_from_checkpoint(self, checkpoint: dict[str, Any]) -> GraphRunSpec:
        raw = checkpoint["spec"]
        return GraphRunSpec(
            graph_id=raw["graph_id"],
            nodes=[self._node_from_plain(node) for node in raw["nodes"]],
            edges=[tuple(edge) for edge in raw["edges"]],
            max_parallelism=int(raw["max_parallelism"]),
            max_attempts=int(raw["max_attempts"]),
            retry_budget=int(raw["retry_budget"]),
            max_same_failure_retries=int(raw["max_same_failure_retries"]),
            overlooker_mode=str(raw["overlooker_mode"]),
        )

    def _node_from_plain(self, raw: dict[str, Any]) -> NodeCapsule:
        allowed = {item.name for item in fields(NodeCapsule)}
        return NodeCapsule(**{key: value for key, value in raw.items() if key in allowed})

    def _record(
        self,
        run_id: str,
        node_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.event_log.append(run_id, node_id, event_type, payload)
