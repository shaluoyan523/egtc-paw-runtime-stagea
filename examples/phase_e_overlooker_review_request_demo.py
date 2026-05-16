from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def sandbox() -> dict[str, object]:
    return {
        "backend": "codex_native",
        "sandbox_mode": "workspace_write",
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": ["."],
        "resource_limits": {
            "wall_time_sec": 30,
            "memory_mb": 512,
            "disk_mb": 256,
            "max_processes": 32,
            "max_command_count": 1,
        },
    }


def permission_review_node() -> NodeCapsule:
    return NodeCapsule(
        node_id="permission-sensitive-branch",
        phase="implementation",
        goal="Complete branch candidate but ask Overlooker for permission review at integration.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "write",
            "permission-sensitive-branch",
            "0.0",
            "--request-permission-review",
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains diff, test, log, sandbox_events, and resource_report.",
            "Overlooker owns permission review escalation.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(),
    )


def main() -> int:
    runtime_root = ROOT / "phasee_overlooker_review_request_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-e-overlooker-review-request-demo",
        nodes=[permission_review_node()],
        edges=[],
        max_parallelism=1,
        max_attempts=1,
        retry_budget=0,
        replan_budget=0,
        overlooker_mode="deterministic",
        director_mode="deterministic",
        phase="E",
        integration_overlooker_mode="deterministic",
    )
    result = runtime.run_graph(spec, run_id="phase-e-overlooker-review-request-demo")
    node = result["nodes"]["permission-sensitive-branch"]
    integration = result["integration_result"] or {}
    report_body = integration.get("report") or {}
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "node_status": node["status"],
        "branch_candidate_ref": node["branch_candidate_ref"],
        "node_overlooker_action": node["overlooker_recommended_action"],
        "integration_accepted": integration.get("accepted"),
        "integration_verdict": report_body.get("verdict"),
        "integration_action": report_body.get("recommended_action"),
        "permission_escalation_required": node["permission_escalation_required"],
        "human_review_required": node["human_review_required"],
        "director_patch_events": [
            event
            for event in result["events"]
            if "DirectorGraphPatch" in event["event_type"]
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        not report["accepted"]
        and report["status"] == "blocked"
        and report["node_status"] == "NODE_BLOCKED"
        and report["branch_candidate_ref"]
        and report["node_overlooker_action"] == "request_permission_review"
        and report["integration_accepted"] is False
        and report["integration_verdict"] == "blocked"
        and report["integration_action"] == "request_permission_review"
        and report["permission_escalation_required"]
        and not report["human_review_required"]
        and not report["director_patch_events"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
