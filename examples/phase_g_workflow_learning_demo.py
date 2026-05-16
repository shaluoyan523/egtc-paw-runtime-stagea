from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def sandbox(read_only: bool) -> dict[str, object]:
    return {
        "backend": "codex_native",
        "sandbox_mode": "read_only" if read_only else "workspace_write",
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [] if read_only else ["."],
        "resource_limits": {
            "wall_time_sec": 30,
            "memory_mb": 512,
            "disk_mb": 256,
            "max_processes": 32,
            "max_command_count": 1,
        },
    }


def dynamic_replan_node(pattern_ids: list[str]) -> NodeCapsule:
    return NodeCapsule(
        node_id="phase-g-dynamic-node",
        phase="implementation",
        goal="Fail once with an overlooker replan request so workflow-level learning observes dynamic workflow updates.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "write",
            "phase-g-dynamic-node",
            "0.0",
            "--fail-once",
            "--request-replan-once",
        ],
        acceptance_criteria=[
            "First attempt requests Director replan.",
            "Retry must happen after the inserted diagnostic node.",
            "Workflow learning must record the dynamic update.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        experience_pattern_ids=pattern_ids,
        sandbox_profile=sandbox(read_only=False),
    )


def stable_node(pattern_ids: list[str]) -> NodeCapsule:
    return NodeCapsule(
        node_id="phase-g-stable-node",
        phase="implementation",
        goal="Complete without retry so workflow-level learning can promote the selected patterns.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "write",
            "phase-g-stable-node",
            "0.0",
        ],
        acceptance_criteria=[
            "Workflow completes without retry or replan.",
            "Workflow learning records a promotable accepted outcome.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        experience_pattern_ids=pattern_ids,
        sandbox_profile=sandbox(read_only=False),
    )


def failing_node(pattern_ids: list[str]) -> NodeCapsule:
    return NodeCapsule(
        node_id="phase-g-failed-node",
        phase="verification",
        goal="Fail without retry budget so workflow-level learning can demote the selected patterns.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "verify",
            "phase-g-failed-node",
            "0.0",
            "--fail-once",
        ],
        acceptance_criteria=[
            "Workflow ends rejected because no retry budget is available.",
            "Workflow learning records a demotable failed outcome.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        experience_pattern_ids=pattern_ids,
        sandbox_profile=sandbox(read_only=True),
    )


def main() -> int:
    runtime_root = ROOT / "phaseg_workflow_learning_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    stable_pattern_ids = [
        "seed-topology-parallel-explore-implement-verify",
        "seed-handoff-artifact-chain",
    ]
    dynamic_pattern_ids = [
        "seed-failure-watchdog-budget",
        "seed-failure-intervention-debugging-loop",
        "seed-memory-hierarchical-task-experience-planner",
    ]
    failed_pattern_ids = [
        "seed-failure-watchdog-budget",
        "seed-review-verification-aware-planning",
    ]
    stable_spec = GraphRunSpec(
        graph_id="phase-g-stable-learning-demo",
        nodes=[stable_node(stable_pattern_ids)],
        edges=[],
        max_parallelism=1,
        max_attempts=1,
        retry_budget=0,
        replan_budget=0,
        overlooker_mode="deterministic",
        phase="G",
    )
    dynamic_spec = GraphRunSpec(
        graph_id="phase-g-workflow-learning-demo",
        nodes=[dynamic_replan_node(dynamic_pattern_ids)],
        edges=[],
        max_parallelism=1,
        max_attempts=2,
        retry_budget=0,
        replan_budget=1,
        max_same_failure_retries=2,
        overlooker_mode="deterministic",
        director_mode="deterministic",
        phase="G",
    )
    failed_spec = GraphRunSpec(
        graph_id="phase-g-failed-learning-demo",
        nodes=[failing_node(failed_pattern_ids)],
        edges=[],
        max_parallelism=1,
        max_attempts=1,
        retry_budget=0,
        replan_budget=0,
        overlooker_mode="deterministic",
        phase="G",
    )
    stable_result = runtime.run_graph(stable_spec, run_id="phase-g-stable-learning-demo")
    dynamic_result = runtime.run_graph(dynamic_spec, run_id="phase-g-workflow-learning-demo")
    failed_result = runtime.run_graph(failed_spec, run_id="phase-g-failed-learning-demo")
    workflow_observations = runtime.experience_library.load_workflow_observations()
    proposals = runtime.experience_library.load_update_proposals()
    observations_by_graph = {
        observation.graph_id: observation for observation in workflow_observations
    }
    stable_observation = observations_by_graph.get(stable_spec.graph_id)
    dynamic_observation = observations_by_graph.get(dynamic_spec.graph_id)
    failed_observation = observations_by_graph.get(failed_spec.graph_id)
    dynamic_event_types = [
        event["event_type"]
        for event in (
            dynamic_observation.dynamic_workflow_events
            if dynamic_observation else []
        )
    ]
    workflow_proposals = [
        proposal
        for proposal in proposals
        if proposal.proposed_by == "workflow-runtime"
    ]
    report = {
        "stable_accepted": stable_result["accepted"],
        "dynamic_accepted": dynamic_result["accepted"],
        "failed_accepted": failed_result["accepted"],
        "failed_status": failed_result["status"],
        "workflow_learning_present": all(
            bool(item.get("workflow_learning"))
            for item in [stable_result, dynamic_result, failed_result]
        ),
        "workflow_observation_count": len(workflow_observations),
        "workflow_outcomes": {
            "stable": stable_observation.outcome if stable_observation else None,
            "dynamic": dynamic_observation.outcome if dynamic_observation else None,
            "failed": failed_observation.outcome if failed_observation else None,
        },
        "workflow_recommended_updates": {
            "stable": stable_observation.recommended_update if stable_observation else None,
            "dynamic": dynamic_observation.recommended_update if dynamic_observation else None,
            "failed": failed_observation.recommended_update if failed_observation else None,
        },
        "dynamic_retry_count": dynamic_observation.retry_count if dynamic_observation else None,
        "dynamic_replan_count": dynamic_observation.replan_count if dynamic_observation else None,
        "dynamic_event_types": dynamic_event_types,
        "dynamic_pattern_ids_used": (
            dynamic_observation.pattern_ids_used if dynamic_observation else []
        ),
        "failed_retry_count": failed_observation.retry_count if failed_observation else None,
        "workflow_update_proposal_count": len(workflow_proposals),
        "workflow_update_types": sorted({proposal.update_type for proposal in workflow_proposals}),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["stable_accepted"]
        and report["dynamic_accepted"]
        and not report["failed_accepted"]
        and report["failed_status"] == "rejected"
        and report["workflow_learning_present"]
        and report["workflow_observation_count"] == 3
        and report["workflow_outcomes"]["stable"] == "accepted"
        and report["workflow_outcomes"]["dynamic"] == "replanned"
        and report["workflow_outcomes"]["failed"] == "rejected"
        and report["workflow_recommended_updates"]["stable"] == "promote"
        and report["workflow_recommended_updates"]["dynamic"] == "revise"
        and report["workflow_recommended_updates"]["failed"] == "demote"
        and report["dynamic_retry_count"] == 1
        and report["dynamic_replan_count"] == 1
        and "GraphPatchApplied" in report["dynamic_event_types"]
        and "NodeRetryScheduled" in report["dynamic_event_types"]
        and set(dynamic_pattern_ids).issubset(set(report["dynamic_pattern_ids_used"]))
        and report["failed_retry_count"] == 0
        and report["workflow_update_proposal_count"] >= (
            len(stable_pattern_ids) + len(dynamic_pattern_ids) + len(failed_pattern_ids)
        )
        and "promote" in report["workflow_update_types"]
        and "revise" in report["workflow_update_types"]
        and "demote" in report["workflow_update_types"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
