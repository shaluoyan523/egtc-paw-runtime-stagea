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
from .compiler import WorkflowCompiler
from .conflict import ConflictResolver
from .event_log import EventLog
from .experience import ExperienceLibrary, ExperienceObservation
from .evidence import EvidenceCollector
from .identity import IdentityService
from .models import (
    CompiledGraphPatch,
    ConflictResolution,
    DecisionConflict,
    EvidenceBundle,
    GraphPatch,
    GraphPatchOperation,
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
    director_mode: str = "deterministic"
    replan_budget: int = 0
    phase: str = "D"
    second_overlooker_mode: str = "deterministic"


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
    overlooker_verdict: str | None = None
    overlooker_report_ref: str | None = None
    overlooker_recommended_action: str | None = None
    overlooker_failure_type: str | None = None
    overlooker_confidence: str | None = None
    release_overlooker: bool = False
    high_risk: bool = False
    second_overlooker_report_ref: str | None = None
    conflict_resolution: dict[str, Any] | None = None
    conflict_history: list[dict[str, Any]] = field(default_factory=list)
    current_workspace: str | None = None
    accepted_workspace: str | None = None
    fork_source_node_id: str | None = None
    fork_source_workspace: str | None = None
    fork_history: list[dict[str, Any]] = field(default_factory=list)
    fork_advisor_history: list[dict[str, Any]] = field(default_factory=list)
    graph_patch_history: list[dict[str, Any]] = field(default_factory=list)
    experience_pattern_ids: list[str] = field(default_factory=list)
    experience_observations: list[dict[str, Any]] = field(default_factory=list)
    experience_update_proposals: list[dict[str, Any]] = field(default_factory=list)


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
    second_overlooker_report: OverlookerReport | None
    conflict: DecisionConflict | None
    conflict_resolution: ConflictResolution | None
    workspace_diff: dict[str, list[str]]
    experience_pattern_ids: list[str]


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
        self.director_actor = self.identity.actor("director-phased", "director")
        self.director_token = self.identity.issue_token(
            self.director_actor,
            ["artifact:read", "artifact:write"],
        )
        self.artifacts = ArtifactStore(self.root / "artifacts", self.identity)
        self.event_log = EventLog(self.root / "events.sqlite3")
        self.wrapper = CodexExecWrapper(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.director_wrapper = CodexExecWrapper(
            self.artifacts, self.director_actor, self.director_token
        )
        self.compiler = WorkflowCompiler()
        self.conflict_resolver = ConflictResolver()
        self.experience_library = ExperienceLibrary(self.root / "experience")
        self.experience_library.seed_defaults()
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
                        spec.phase,
                        spec.second_overlooker_mode,
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
                        self._apply_node_result(run_id, spec.graph_id, record, result)
                    if record.status == "NODE_REJECTED":
                        retry_event = self._maybe_retry(run_id, spec, record, records)
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
        phase: str,
        second_overlooker_mode: str,
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
        if overlooker_mode in {"codex", "codex_phase_e"}:
            overlooker_report = self.overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root
                / "runs"
                / run_id
                / "nodes"
                / node.node_id
                / f"attempt-{fork_plan.attempt}"
                / "overlooker",
            )
        else:
            overlooker_report = self._deterministic_review(
                node,
                evidence,
                validator_reports,
                worker_result,
            )
        second_overlooker_report: OverlookerReport | None = None
        conflict: DecisionConflict | None = None
        resolution: ConflictResolution | None = None
        high_risk = self._is_high_risk(node)
        if self._phase_e_enabled(phase, overlooker_mode) and high_risk:
            second_overlooker_report = self._second_overlooker_review(
                run_id,
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                fork_plan,
                second_overlooker_mode,
            )
            conflict = self._build_conflict(
                run_id,
                node,
                validator_reports,
                [overlooker_report, second_overlooker_report],
                high_risk,
            )
            resolution = self.conflict_resolver.resolve(conflict)
            self._record(
                run_id,
                node.node_id,
                "DecisionConflictResolved",
                {
                    "conflict": to_plain_dict(conflict),
                    "resolution": to_plain_dict(resolution),
                },
            )
        elif self._phase_e_enabled(phase, overlooker_mode):
            conflict = self._build_conflict(
                run_id,
                node,
                validator_reports,
                [overlooker_report],
                high_risk,
            )
            resolution = self.conflict_resolver.resolve(conflict)

        if resolution is not None:
            final_state = (
                NodeState.NODE_ACCEPTED.value
                if resolution.release_node
                else NodeState.NODE_REJECTED.value
            )
        else:
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
            second_overlooker_report=second_overlooker_report,
            conflict=conflict,
            conflict_resolution=resolution,
            workspace_diff=workspace_diff,
            experience_pattern_ids=list(node.experience_pattern_ids),
        )

    def _apply_node_result(
        self,
        run_id: str,
        graph_id: str,
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
        record.overlooker_verdict = result.overlooker_report.verdict
        record.overlooker_report_ref = (
            result.overlooker_report.report_ref.uri
            if result.overlooker_report.report_ref
            else None
        )
        record.overlooker_recommended_action = result.overlooker_report.recommended_action
        record.overlooker_failure_type = result.overlooker_report.failure_type
        record.overlooker_confidence = result.overlooker_report.confidence
        record.release_overlooker = result.overlooker_report.release_overlooker
        record.high_risk = result.conflict.details.get("high_risk", False) if result.conflict else False
        record.second_overlooker_report_ref = (
            result.second_overlooker_report.report_ref.uri
            if result.second_overlooker_report and result.second_overlooker_report.report_ref
            else None
        )
        if result.conflict and result.conflict_resolution:
            conflict_event = {
                "conflict": to_plain_dict(result.conflict),
                "resolution": to_plain_dict(result.conflict_resolution),
            }
            record.conflict_history.append(conflict_event)
            record.conflict_resolution = to_plain_dict(result.conflict_resolution)
            if result.conflict_resolution.required_action == "request_permission_review":
                record.overlooker_recommended_action = "request_permission_review"
            elif result.conflict_resolution.required_action == "require_human_review":
                record.overlooker_recommended_action = "require_human_review"
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
                "overlooker_report_ref": record.overlooker_report_ref,
                "overlooker_recommended_action": record.overlooker_recommended_action,
                "overlooker_failure_type": record.overlooker_failure_type,
                "release_overlooker": record.release_overlooker,
                "second_overlooker_report_ref": record.second_overlooker_report_ref,
                "conflict_resolution": record.conflict_resolution,
            },
        )
        self._record_experience_observation(run_id, graph_id, record, result)

    def _maybe_retry(
        self,
        run_id: str,
        spec: GraphRunSpec,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
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
        recommended_action = record.overlooker_recommended_action or "retry_same_node"
        retry_actions = {"retry_same_node", "retry_with_modified_instruction"}
        replan_actions = {"request_director_replan"}
        if recommended_action not in retry_actions | replan_actions:
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "recommended_action": recommended_action,
                "action": "blocked_by_overlooker_recommendation",
            }
            self._record(run_id, record.node_id, "NodeRetryNotScheduled", event)
            return event
        if recommended_action in replan_actions and spec.replan_budget <= 0:
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "recommended_action": recommended_action,
                "action": "blocked_replan_budget_exhausted",
            }
            self._record(run_id, record.node_id, "NodeRetryNotScheduled", event)
            return event
        if record.attempts < spec.max_attempts and spec.retry_budget > 0:
            patch = self._propose_retry_patch(run_id, spec, record, records)
            compiled = self.compiler.validate_patch(
                patch,
                set(records),
                spec.graph_id,
                spec.phase,
            )
            patch_ref = self.artifacts.put_json(
                to_plain_dict(patch),
                {"kind": "stage_d_graph_patch", "graph_id": spec.graph_id, "node_id": record.node_id},
                self.director_actor,
                self.director_token,
            )
            compiled_ref = self.artifacts.put_json(
                to_plain_dict(compiled),
                {
                    "kind": "stage_d_compiled_graph_patch",
                    "graph_id": spec.graph_id,
                    "node_id": record.node_id,
                    "patch_id": patch.patch_id,
                },
                self.runtime_actor,
                self.runtime_token,
            )
            patch_event = {
                "patch": to_plain_dict(patch),
                "compiled": to_plain_dict(compiled),
                "patch_ref": to_plain_dict(patch_ref),
                "compiled_patch_ref": to_plain_dict(compiled_ref),
            }
            record.graph_patch_history.append(patch_event)
            self._record(run_id, record.node_id, "DirectorGraphPatchProposed", patch_event)
            if not compiled.accepted:
                record.status = "NODE_ABORTED"
                event = {
                    "node_id": record.node_id,
                    "failure_code": failure_code,
                    "patch_id": patch.patch_id,
                    "findings": compiled.findings,
                    "action": "abort_invalid_graph_patch",
                }
                self._record(run_id, record.node_id, "GraphPatchRejected", event)
                return event

            applied = self._apply_graph_patch(run_id, spec, compiled, record)
            if not applied:
                event = {
                    "node_id": record.node_id,
                    "failure_code": failure_code,
                    "patch_id": patch.patch_id,
                    "action": "graph_patch_noop",
                }
                self._record(run_id, record.node_id, "GraphPatchNoop", event)
                return event
            spec.retry_budget -= 1
            if recommended_action in replan_actions:
                spec.replan_budget = max(0, spec.replan_budget - patch.replan_budget_cost)
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "attempts": record.attempts,
                "remaining_retry_budget": spec.retry_budget,
                "remaining_replan_budget": spec.replan_budget,
                "patch_id": patch.patch_id,
                "recommended_action": recommended_action,
                "action": "retry_via_director_graph_patch",
            }
            self._record(run_id, record.node_id, "NodeRetryScheduled", event)
            return event
        return None

    def _propose_retry_patch(
        self,
        run_id: str,
        spec: GraphRunSpec,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
    ) -> GraphPatch:
        if spec.director_mode != "codex":
            return GraphPatch(
                patch_id=f"graph-patch-{uuid.uuid4().hex[:12]}",
                director_id=self.director_actor.actor_id,
                graph_id=spec.graph_id,
                triggering_node_id=record.node_id,
                triggering_event="overlooker_rejected_node",
                overlooker_report_ref=record.overlooker_report_ref,
                operations=[
                    GraphPatchOperation(
                        op="retry_node",
                        node_id=record.node_id,
                        value={
                            "failure_code": record.failure_code,
                            "recommended_action": record.overlooker_recommended_action,
                        },
                        rationale="Retry the rejected node through a compiler-validated Stage D GraphPatch.",
                    )
                ],
                rationale="Deterministic Director selected bounded retry for the rejected node.",
            )

        director_workspace = (
            self.root
            / "runs"
            / run_id
            / "nodes"
            / record.node_id
            / f"after-attempt-{record.attempts}"
            / "director"
        )
        director_workspace.mkdir(parents=True, exist_ok=True)
        runtime_state = {
            "run_id": run_id,
            "graph_id": spec.graph_id,
            "triggering_node": to_plain_dict(record),
            "known_node_ids": sorted(records),
            "node_statuses": {
                node_id: {
                    "status": node_record.status,
                    "attempts": node_record.attempts,
                    "accepted_workspace": node_record.accepted_workspace,
                    "failure_code": node_record.failure_code,
                }
                for node_id, node_record in records.items()
            },
            "policy": {
                "stage": "Phase D",
                "allowed_operations": ["retry_node"],
                "forbidden": [
                    "Do not change permissions or sandbox_profile.",
                    "Do not add or remove graph edges in Phase D.",
                    "Do not skip the Overlooker gate.",
                    "Retry only the triggering node.",
                ],
            },
        }
        (director_workspace / "director_runtime_state.json").write_text(
            json.dumps(runtime_state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        director_node = NodeCapsule(
            node_id=f"{record.node_id}-director-graph-patch",
            phase="Phase D Director",
            goal="Create a compiler-valid Stage D GraphPatch for the rejected node.",
            command=[],
            acceptance_criteria=[
                "Director must write strict JSON to graph_patch.json.",
                "Director may only use retry_node in Phase D.",
                "Director must not change permissions, sandbox policy, or edges.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="codex_cli",
            prompt=self._director_patch_prompt(),
            sandbox_profile={
                "backend": "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": 120,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 48,
                    "max_command_count": 1,
                },
            },
        )
        director_result = self.director_wrapper.run(
            director_node,
            director_workspace,
            role="director",
            run_id=run_id,
        )
        data = self._read_graph_patch(director_workspace / "graph_patch.json")
        patch = self._graph_patch_from_data(
            data,
            spec,
            record,
        )
        self._record(
            run_id,
            record.node_id,
            "DirectorGraphPatchSessionCompleted",
            {
                "director_id": self.director_actor.actor_id,
                "director_session_id": director_result.worker_id,
                "exit_code": director_result.exit_code,
                "workspace": str(director_workspace),
                "event_refs": [to_plain_dict(ref) for ref in director_result.event_refs],
                "patch_id": patch.patch_id,
            },
        )
        return patch

    def _director_patch_prompt(self) -> str:
        return """
You are the EGTC-PAW Phase D Director Agent.

Read ./director_runtime_state.json and create ./graph_patch.json.

Output strict JSON:
{
  "patch_id": "graph-patch-...",
  "director_id": "codex-director",
  "graph_id": "...",
  "triggering_node_id": "...",
  "triggering_event": "overlooker_rejected_node",
  "overlooker_report_ref": "artifact://..." | null,
  "operations": [
    {
      "op": "retry_node",
      "node_id": "...",
      "source_node_id": null,
      "target_node_id": null,
      "value": {
        "failure_code": "...",
        "recommended_action": "..."
      },
      "rationale": "short reason"
    }
  ],
  "rationale": "short reason",
  "replan_budget_cost": 1
}

Rules:
- Use exactly one retry_node operation.
- Retry only the triggering node from director_runtime_state.json.
- Do not modify permissions, sandbox_profile, network policy, graph edges, or Overlooker gates.
- Do not clone repositories.
- Do not use network.
- Do not write outside this workspace.
""".strip()

    def _read_graph_patch(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"operations": [{"op": "missing_director_output"}]}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "rationale": f"Director graph_patch.json was not valid JSON: {exc}",
                "operations": [{"op": "invalid_director_output"}],
            }
        return data if isinstance(data, dict) else {"operations": [{"op": "invalid_director_output"}]}

    def _graph_patch_from_data(
        self,
        data: dict[str, Any],
        spec: GraphRunSpec,
        record: GraphNodeRecord,
    ) -> GraphPatch:
        raw_operations = data.get("operations")
        operations: list[GraphPatchOperation] = []
        if isinstance(raw_operations, list):
            for raw in raw_operations:
                if not isinstance(raw, dict):
                    continue
                value = raw.get("value")
                operations.append(
                    GraphPatchOperation(
                        op=str(raw.get("op") or ""),
                        node_id=str(raw["node_id"]) if raw.get("node_id") is not None else None,
                        source_node_id=str(raw["source_node_id"])
                        if raw.get("source_node_id") is not None
                        else None,
                        target_node_id=str(raw["target_node_id"])
                        if raw.get("target_node_id") is not None
                        else None,
                        value=value if isinstance(value, dict) else {},
                        rationale=str(raw.get("rationale") or ""),
                    )
                )
        if not operations:
            operations = [GraphPatchOperation(op="missing_director_output", node_id=record.node_id)]

        return GraphPatch(
            patch_id=str(data.get("patch_id") or f"graph-patch-{uuid.uuid4().hex[:12]}"),
            director_id=self.director_actor.actor_id,
            graph_id=str(data.get("graph_id") or spec.graph_id),
            triggering_node_id=str(data.get("triggering_node_id") or record.node_id),
            triggering_event=str(data.get("triggering_event") or "overlooker_rejected_node"),
            overlooker_report_ref=(
                str(data["overlooker_report_ref"])
                if data.get("overlooker_report_ref") is not None
                else record.overlooker_report_ref
            ),
            operations=operations,
            rationale=str(data.get("rationale") or "Director proposed a Stage D retry patch."),
            replan_budget_cost=int(data.get("replan_budget_cost") or 1),
        )

    def _apply_graph_patch(
        self,
        run_id: str,
        spec: GraphRunSpec,
        compiled: CompiledGraphPatch,
        record: GraphNodeRecord,
    ) -> bool:
        applied = False
        for operation in compiled.operations:
            target_node_id = operation.node_id or operation.target_node_id
            if operation.op == "retry_node" and target_node_id == record.node_id:
                record.status = "NODE_PLANNED"
                record.current_worker_id = None
                record.final_state = None
                applied = True
        if applied:
            self._record(
                run_id,
                record.node_id,
                "GraphPatchApplied",
                {
                    "graph_id": spec.graph_id,
                    "patch_id": compiled.patch_id,
                    "operations": to_plain_dict(compiled.operations),
                },
            )
        return applied

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
            "cited_evidence": [evidence.evidence_ref.uri] if can_pass else [],
            "validator_refs": [report.validator_id for report in validator_reports],
            "confidence": "high",
            "failure_type": None if can_pass else self._failure_code(worker_result, validator_reports),
            "recommended_action": "advance" if can_pass else "retry_same_node",
            "release_overlooker": can_pass,
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
            confidence=str(report["confidence"]),
            cited_evidence=list(report["cited_evidence"]),
            failure_type=report["failure_type"],
            recommended_action=str(report["recommended_action"]),
            release_overlooker=bool(report["release_overlooker"]),
        )

    def _phase_e_enabled(self, phase: str, overlooker_mode: str) -> bool:
        return phase.upper() == "E" or overlooker_mode in {"phase_e", "codex_phase_e"}

    def _is_high_risk(self, node: NodeCapsule) -> bool:
        profile = node.sandbox_profile or {}
        if bool(profile.get("high_risk")):
            return True
        risk = str(profile.get("risk_level") or "").lower()
        if risk in {"high", "critical"}:
            return True
        if node.phase.lower() in {"release", "deployment", "permission", "security"}:
            return True
        return bool(self._write_paths(node)) and bool(profile.get("requires_second_overlooker"))

    def _second_overlooker_review(
        self,
        run_id: str,
        node: NodeCapsule,
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
        worker_result: WorkerResult,
        workspace_diff: dict[str, list[str]],
        fork_plan: WorkspaceForkPlan,
        second_overlooker_mode: str,
    ) -> OverlookerReport:
        if second_overlooker_mode == "codex":
            return self.overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root
                / "runs"
                / run_id
                / "nodes"
                / node.node_id
                / f"attempt-{fork_plan.attempt}"
                / "second-overlooker",
            )
        return self._deterministic_review(
            node,
            evidence,
            validator_reports,
            worker_result,
        )

    def _build_conflict(
        self,
        run_id: str,
        node: NodeCapsule,
        validator_reports: list[ValidatorReport],
        overlooker_reports: list[OverlookerReport],
        high_risk: bool,
    ) -> DecisionConflict:
        policy_findings = self._policy_findings(node)
        conflict_type = "high_risk_second_overlooker" if high_risk else "phase_e_gate"
        if policy_findings:
            conflict_type = "policy_conflict"
        elif any(not report.passed for report in validator_reports):
            conflict_type = "validator_conflict"
        elif len({report.verdict for report in overlooker_reports}) > 1:
            conflict_type = "overlooker_disagreement"
        return DecisionConflict(
            conflict_id=f"conflict-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            node_id=node.node_id,
            conflict_type=conflict_type,
            policy_findings=policy_findings,
            validator_reports=validator_reports,
            overlooker_reports=overlooker_reports,
            director_intent="advance",
            risk_level="high" if high_risk else "normal",
            details={
                "high_risk": high_risk,
                "policy_source": "sandbox_profile",
            },
        )

    def _policy_findings(self, node: NodeCapsule) -> list[dict[str, Any]]:
        profile = node.sandbox_profile or {}
        findings: list[dict[str, Any]] = []
        if profile.get("network") not in {None, "none"}:
            findings.append(
                {
                    "severity": "error",
                    "code": "network_requires_permission_review",
                    "message": "Phase E blocks non-none network until permission review.",
                    "node_id": node.node_id,
                }
            )
        sensitive_paths = profile.get("sensitive_paths") or [".env", ".git", "secrets"]
        write_paths = profile.get("allowed_write_paths") or []
        for path in write_paths:
            text = str(path).strip("/")
            if any(text == str(s).strip("/") or text.startswith(f"{str(s).strip('/')}/") for s in sensitive_paths):
                findings.append(
                    {
                        "severity": "error",
                        "code": "sensitive_write_requires_permission_review",
                        "message": f"Write path requires permission review: {path}",
                        "node_id": node.node_id,
                    }
                )
        return findings

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
                experience_pattern_ids=list(node.experience_pattern_ids),
            )
            for node in spec.nodes
        }

    def _record_experience_observation(
        self,
        run_id: str,
        graph_id: str,
        record: GraphNodeRecord,
        result: NodeExecutionResult,
    ) -> None:
        pattern_ids = list(result.experience_pattern_ids or record.experience_pattern_ids)
        if not pattern_ids:
            return
        if record.status == "NODE_ACCEPTED":
            outcome = "accepted"
            recommended_update = "promote"
        elif record.status == "NODE_ABORTED":
            outcome = "aborted"
            recommended_update = "demote"
        else:
            outcome = "rejected"
            recommended_update = "demote"
        evidence_refs = [result.evidence.evidence_ref.uri]
        if record.overlooker_report_ref:
            evidence_refs.append(record.overlooker_report_ref)
        observation = ExperienceObservation(
            observation_id=f"exp-observation-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            graph_id=graph_id,
            node_id=result.node_id,
            pattern_ids_used=pattern_ids,
            outcome=outcome,
            validator_findings=[
                {
                    "validator_id": report.validator_id,
                    "passed": report.passed,
                    "findings": report.findings,
                    "evidence_ref": report.evidence_ref,
                }
                for report in result.validator_reports
            ],
            overlooker_verdict=result.overlooker_report.verdict,
            failure_type=result.overlooker_report.failure_type or record.failure_code,
            recommended_update=recommended_update,
            evidence_refs=evidence_refs,
        )
        self.experience_library.record_observation(observation)
        proposals = self.experience_library.update_from_observation(
            observation,
            proposed_by="runtime",
        )
        observation_event = {
            "observation": to_plain_dict(observation),
            "update_proposals": [to_plain_dict(proposal) for proposal in proposals],
        }
        record.experience_observations.append(to_plain_dict(observation))
        record.experience_update_proposals.extend(
            to_plain_dict(proposal) for proposal in proposals
        )
        self._record(
            run_id,
            record.node_id,
            "ExperienceObservationRecorded",
            observation_event,
        )

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
            source_node_id = self._choose_fork_source(
                run_id,
                node,
                record,
                records,
                sorted(candidates),
                overlooker_mode,
            )
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

    def _choose_fork_source(
        self,
        run_id: str,
        node: NodeCapsule,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
        candidates: list[str],
        overlooker_mode: str,
    ) -> str:
        default_choice = candidates[-1]
        if overlooker_mode != "codex" or record.attempts <= 1:
            return default_choice
        advisor_workspace = (
            self.root
            / "runs"
            / run_id
            / "nodes"
            / node.node_id
            / f"attempt-{record.attempts}"
            / "fork-overlooker"
        )
        advisor_workspace.mkdir(parents=True, exist_ok=True)
        input_packet = {
            "node_id": node.node_id,
            "attempt": record.attempts,
            "previous_failure_code": record.failure_code,
            "candidate_nodes": [
                {
                    "node_id": candidate,
                    "status": records[candidate].status,
                    "accepted_workspace": records[candidate].accepted_workspace,
                    "failure_code": records[candidate].failure_code,
                }
                for candidate in candidates
            ],
            "policy": {
                "allowed_selection": "Choose one candidate with status NODE_ACCEPTED and a non-empty accepted_workspace.",
                "goal": "Fork the retry from a known-good upstream workspace, not from the failed attempt workspace.",
            },
        }
        (advisor_workspace / "fork_advisor_input.json").write_text(
            json.dumps(input_packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        advisor_node = NodeCapsule(
            node_id=f"{node.node_id}-fork-overlooker",
            phase="Phase D Fork Overlooker",
            goal="Select the accepted upstream workspace to fork for retry.",
            command=[],
            acceptance_criteria=[
                "Fork advisor must choose an accepted candidate only.",
                "Fork advisor must write strict JSON.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="codex_cli",
            prompt=self._fork_advisor_prompt(),
            sandbox_profile={
                "backend": "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": 120,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 48,
                    "max_command_count": 1,
                },
            },
        )
        advisor_result = self.wrapper.run(
            advisor_node,
            advisor_workspace,
            role="overlooker",
            run_id=run_id,
        )
        decision = self._read_fork_decision(advisor_workspace / "fork_decision.json")
        selected = str(decision.get("selected_node_id") or default_choice)
        if selected not in candidates:
            selected = default_choice
        if records[selected].status != "NODE_ACCEPTED" or not records[selected].accepted_workspace:
            selected = default_choice
        event = {
            "node_id": node.node_id,
            "attempt": record.attempts,
            "overlooker_id": advisor_result.worker_id,
            "overlooker_exit_code": advisor_result.exit_code,
            "candidate_node_ids": candidates,
            "selected_node_id": selected,
            "decision": decision,
            "workspace": str(advisor_workspace),
        }
        record.fork_advisor_history.append(event)
        self._record(run_id, node.node_id, "OverlookerForkDecision", event)
        return selected

    def _fork_advisor_prompt(self) -> str:
        return """
You are the Phase D Codex overlooker for retry fork selection.

Read ./fork_advisor_input.json and create ./fork_decision.json.

Output strict JSON:
{
  "selected_node_id": "...",
  "rationale": "short reason",
  "confidence": "low" | "medium" | "high"
}

Rules:
- Select only one candidate whose status is NODE_ACCEPTED and accepted_workspace is present.
- Prefer a direct upstream accepted workspace over any failed attempt workspace.
- Do not clone repositories.
- Do not use network.
- Do not write outside this workspace.
""".strip()

    def _read_fork_decision(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

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
            "director_mode": spec.director_mode,
            "replan_budget": spec.replan_budget,
            "phase": spec.phase,
            "second_overlooker_mode": spec.second_overlooker_mode,
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
            director_mode=str(raw.get("director_mode", "deterministic")),
            replan_budget=int(raw.get("replan_budget", 0)),
            phase=str(raw.get("phase", "D")),
            second_overlooker_mode=str(raw.get("second_overlooker_mode", "deterministic")),
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
