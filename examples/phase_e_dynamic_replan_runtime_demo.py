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


def replan_node() -> NodeCapsule:
    return NodeCapsule(
        node_id="implement-replan",
        phase="implementation",
        goal="Fail once with an overlooker hint that requests Phase E Director replan.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "write",
            "implement-replan",
            "0.0",
            "--fail-once",
            "--request-replan-once",
        ],
        acceptance_criteria=[
            "First failure must request Director replan.",
            "Second attempt must run after inserted diagnostic evidence.",
            "Overlooker must cite evidence_ref before acceptance.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(read_only=False),
    )


def main() -> int:
    runtime_root = ROOT / "phasee_dynamic_replan_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-e-dynamic-replan-demo",
        nodes=[replan_node()],
        edges=[],
        max_parallelism=1,
        max_attempts=2,
        retry_budget=0,
        replan_budget=1,
        max_same_failure_retries=2,
        overlooker_mode="deterministic",
        director_mode="deterministic",
        phase="E",
    )
    result = runtime.run_graph(spec, run_id="phase-e-dynamic-replan-demo")
    main_node = result["nodes"]["implement-replan"]
    inserted_id = "implement-replan-phase-e-diagnostic"
    inserted = result["nodes"].get(inserted_id, {})
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "main_status": main_node["status"],
        "main_attempts": main_node["attempts"],
        "inserted_present": inserted_id in result["nodes"],
        "inserted_status": inserted.get("status"),
        "main_depends_on": main_node["depends_on"],
        "retry_events": result["retry_events"],
        "graph_patch_ops": (
            [
                item["op"]
                for item in main_node["graph_patch_history"][0]["patch"]["operations"]
            ]
            if main_node["graph_patch_history"]
            else []
        ),
        "fork_history": main_node["fork_history"],
        "checkpoint_path": result["checkpoint_path"],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted"]
        and report["main_status"] == "NODE_ACCEPTED"
        and report["main_attempts"] == 2
        and report["inserted_present"]
        and report["inserted_status"] == "NODE_ACCEPTED"
        and inserted_id in report["main_depends_on"]
        and report["graph_patch_ops"] == ["insert_node", "add_edge", "retry_node"]
        and report["retry_events"]
        and report["retry_events"][0]["recommended_action"] == "request_director_replan"
        and report["retry_events"][0]["remaining_replan_budget"] == 0
        and report["fork_history"][-1]["source_node_id"] == inserted_id
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
