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


def node(
    node_id: str,
    phase: str,
    role: str,
    delay: float,
    read_only: bool,
    *extra: str,
) -> NodeCapsule:
    return NodeCapsule(
        node_id=node_id,
        phase=phase,
        goal=f"Phase D demo node {node_id}",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            role,
            node_id,
            str(delay),
            *extra,
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains diff, test, log, sandbox_events, and resource_report.",
            "Phase D overlooker pass cites validator-backed evidence.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(read_only),
    )


def main() -> int:
    runtime_root = ROOT / "phased_graph_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-d-demo-graph",
        nodes=[
            node("diagnose-api", "diagnosis", "read", 0.3, True),
            node("diagnose-tests", "diagnosis", "read", 0.3, True),
            node("implement-core", "implementation", "write", 0.4, False),
            node("implement-docs", "implementation", "write", 0.4, False),
            node(
                "verify-flaky",
                "verification",
                "read",
                0.1,
                True,
                "--fail-once",
                "--poison-on-fail",
                "--fail-if-poisoned",
            ),
        ],
        edges=[
            ("diagnose-api", "implement-core"),
            ("diagnose-tests", "implement-core"),
            ("diagnose-api", "implement-docs"),
            ("diagnose-tests", "implement-docs"),
            ("implement-core", "verify-flaky"),
            ("implement-docs", "verify-flaky"),
        ],
        max_parallelism=3,
        max_attempts=2,
        retry_budget=1,
        max_same_failure_retries=2,
        overlooker_mode="deterministic",
    )

    first = runtime.run_graph(spec, run_id="phase-d-demo-run", stop_after_accepted=2)
    resumed = runtime.resume_graph("phase-d-demo-run")
    result = {
        "first_status": first["status"],
        "final_status": resumed["status"],
        "accepted": resumed["accepted"],
        "max_observed_parallelism": max(
            first["max_observed_parallelism"],
            resumed["max_observed_parallelism"],
        ),
        "write_lock_wait_count": len(resumed["write_lock_waits"]),
        "retry_events": resumed["retry_events"],
        "fork_history": {
            node_id: record["fork_history"] for node_id, record in resumed["nodes"].items()
        },
        "accepted_workspaces": {
            node_id: record["accepted_workspace"] for node_id, record in resumed["nodes"].items()
        },
        "checkpoint_path": resumed["checkpoint_path"],
        "node_statuses": {
            node_id: record["status"] for node_id, record in resumed["nodes"].items()
        },
        "attempts": {
            node_id: record["attempts"] for node_id, record in resumed["nodes"].items()
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        result["accepted"]
        and result["first_status"] == "paused"
        and result["max_observed_parallelism"] >= 2
        and result["write_lock_wait_count"] >= 1
        and len(result["fork_history"]["verify-flaky"]) == 2
        and result["fork_history"]["verify-flaky"][-1]["reason"] == "retry_from_accepted_dependency"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
