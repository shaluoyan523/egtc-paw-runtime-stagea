from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def sandbox(read_only: bool = False) -> dict[str, object]:
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


def branch_node(node_id: str, role: str, read_only: bool = False) -> NodeCapsule:
    return NodeCapsule(
        node_id=node_id,
        phase="verification" if read_only else "implementation",
        goal=f"Phase E branch candidate node {node_id}.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            role,
            node_id,
            "0.0",
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains diff, test, log, sandbox_events, and resource_report.",
            "Phase E must create a branch candidate before final integration.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(read_only=read_only),
    )


def main() -> int:
    runtime_root = ROOT / "phasee_branch_integration_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-e-branch-integration-demo",
        nodes=[
            branch_node("implement-branch", "write"),
            branch_node("verify-branch", "verify", read_only=True),
        ],
        edges=[("implement-branch", "verify-branch")],
        max_parallelism=1,
        max_attempts=1,
        retry_budget=0,
        overlooker_mode="deterministic",
        phase="E",
        integration_overlooker_mode="deterministic",
    )
    result = runtime.run_graph(spec, run_id="phase-e-branch-integration-demo")
    nodes = result["nodes"]
    integration = result["integration_result"] or {}
    workflow_observations = runtime.experience_library.load_workflow_observations()
    workflow_observation = workflow_observations[-1] if workflow_observations else None
    workflow_dynamic_events = [
        event["event_type"]
        for event in (workflow_observation.dynamic_workflow_events if workflow_observation else [])
    ]
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "node_statuses": {node_id: node["status"] for node_id, node in nodes.items()},
        "branch_names": {node_id: node["branch_name"] for node_id, node in nodes.items()},
        "branch_candidate_refs": {
            node_id: node["branch_candidate_ref"] for node_id, node in nodes.items()
        },
        "integration_accepted": integration.get("accepted"),
        "integration_verdict": (integration.get("report") or {}).get("verdict"),
        "integration_action": (integration.get("report") or {}).get("recommended_action"),
        "workflow_observation_count": len(workflow_observations),
        "workflow_branch_candidate_count": (
            workflow_observation.branch_candidate_count if workflow_observation else None
        ),
        "workflow_dynamic_events": workflow_dynamic_events,
        "integration_report_refs": {
            node_id: node["integration_report_ref"] for node_id, node in nodes.items()
        },
        "director_patch_events": [
            event
            for event in result["events"]
            if "DirectorGraphPatch" in event["event_type"]
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted"]
        and report["status"] == "accepted"
        and all(status == "NODE_ACCEPTED" for status in report["node_statuses"].values())
        and all(report["branch_names"].values())
        and all(report["branch_candidate_refs"].values())
        and report["integration_accepted"]
        and report["integration_verdict"] == "pass"
        and report["integration_action"] == "advance"
        and report["workflow_observation_count"] == 1
        and report["workflow_branch_candidate_count"] == 2
        and "PhaseEBranchCandidateCreated" in report["workflow_dynamic_events"]
        and "PhaseEIntegrationGateCompleted" in report["workflow_dynamic_events"]
        and all(report["integration_report_refs"].values())
        and not report["director_patch_events"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
