from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def sandbox(high_risk: bool = False) -> dict[str, object]:
    return {
        "backend": "codex_native",
        "sandbox_mode": "workspace_write",
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": ["."],
        "high_risk": high_risk,
        "requires_second_overlooker": high_risk,
        "resource_limits": {
            "wall_time_sec": 30,
            "memory_mb": 512,
            "disk_mb": 256,
            "max_processes": 32,
            "max_command_count": 1,
        },
    }


def high_risk_node() -> NodeCapsule:
    return NodeCapsule(
        node_id="release-check",
        phase="release",
        goal="Phase E high-risk node requiring second Overlooker consensus.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "release",
            "release-check",
            "0.0",
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains diff, test, log, sandbox_events, and resource_report.",
            "High-risk node requires second Overlooker consensus before release.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(high_risk=True),
    )


def main() -> int:
    runtime_root = ROOT / "phasee_conflict_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-e-conflict-demo",
        nodes=[high_risk_node()],
        edges=[],
        max_parallelism=1,
        max_attempts=1,
        retry_budget=0,
        overlooker_mode="deterministic",
        phase="E",
        second_overlooker_mode="deterministic",
    )
    result = runtime.run_graph(spec, run_id="phase-e-conflict-demo")
    node = result["nodes"]["release-check"]
    resolution = node["conflict_resolution"] or {}
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "node_status": node["status"],
        "high_risk": node["high_risk"],
        "second_overlooker_report_ref": node["second_overlooker_report_ref"],
        "conflict_history_len": len(node["conflict_history"]),
        "resolution_decision": resolution.get("decision"),
        "resolution_priority": resolution.get("priority"),
        "release_node": resolution.get("release_node"),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted"]
        and report["high_risk"]
        and report["second_overlooker_report_ref"]
        and report["conflict_history_len"] == 1
        and report["resolution_decision"] == "accepted"
        and report["resolution_priority"] == "overlooker_consensus"
        and report["release_node"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
