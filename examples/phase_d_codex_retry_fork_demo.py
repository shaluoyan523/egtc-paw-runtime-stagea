from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def sandbox(read_only: bool, timeout_sec: int = 180) -> dict[str, object]:
    return {
        "backend": "codex_native",
        "sandbox_mode": "read_only" if read_only else "workspace_write",
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [] if read_only else ["."],
        "resource_limits": {
            "wall_time_sec": timeout_sec,
            "memory_mb": 1024,
            "disk_mb": 512,
            "max_processes": 48,
            "max_command_count": 1,
        },
    }


def baseline_node() -> NodeCapsule:
    return NodeCapsule(
        node_id="baseline",
        phase="baseline",
        goal="Create a clean accepted upstream workspace.",
        command=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import json; "
                "Path('baseline.txt').write_text('clean baseline\\n'); "
                "Path('phasea_test_result.json').write_text(json.dumps({'passed': True, 'name': 'baseline_contract'})); "
                "print(json.dumps({'type':'test_result','name':'baseline_contract','passed':True}))"
            ),
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains a passing test report.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(read_only=False, timeout_sec=30),
    )


def codex_prompt() -> str:
    return """
You are a Codex worker testing Phase D retry fork behavior.

Read ./.egtc_attempt.json.

If attempt is 1:
- Create ./codex-verify.poison with any text.
- Create ./phasea_test_result.json containing {"passed": false, "name": "codex_retry_first_attempt"}.
- Print one JSONL line: {"type":"test_result","name":"codex_retry_first_attempt","passed":false}.
- Exit with code 2.

If attempt is 2:
- If ./codex-verify.poison exists, create ./phasea_test_result.json containing {"passed": false, "name": "codex_retry_poison_guard"} and exit with code 4.
- Otherwise create ./codex_retry_success.txt.
- Create ./phasea_test_result.json containing {"passed": true, "name": "codex_retry_fork_clean"}.
- Print one JSONL line: {"type":"test_result","name":"codex_retry_fork_clean","passed":true}.
- Exit with code 0.

Rules:
- Do not clone repositories.
- Do not use network.
- Do not write outside this workspace.
""".strip()


def codex_verify_node() -> NodeCapsule:
    return NodeCapsule(
        node_id="codex-verify",
        phase="verification",
        goal="Use a real Codex session to verify retry forks from a clean accepted upstream workspace.",
        command=[],
        acceptance_criteria=[
            "Attempt 1 must fail after poisoning its workspace.",
            "Attempt 2 must run in a clean fork from accepted upstream evidence.",
            "Evidence contains sandbox_events and resource_report.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        executor_kind="codex_cli",
        prompt=codex_prompt(),
        sandbox_profile=sandbox(read_only=False, timeout_sec=180),
    )


def main() -> int:
    runtime_root = ROOT / "phased_codex_retry_fork_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-d-codex-retry-fork",
        nodes=[baseline_node(), codex_verify_node()],
        edges=[("baseline", "codex-verify")],
        max_parallelism=1,
        max_attempts=2,
        retry_budget=1,
        max_same_failure_retries=2,
        overlooker_mode="codex",
        director_mode="codex",
    )
    result = runtime.run_graph(spec, run_id="phase-d-codex-retry-fork")
    verify = result["nodes"]["codex-verify"]
    fork_history = verify["fork_history"]
    attempt1 = Path(fork_history[0]["target_workspace"])
    attempt2 = Path(fork_history[1]["target_workspace"]) if len(fork_history) > 1 else None
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "codex_attempts": verify["attempts"],
        "codex_status": verify["status"],
        "codex_worker_id": verify["current_worker_id"],
        "retry_fork_reason": fork_history[-1]["reason"] if fork_history else None,
        "retry_source_node": fork_history[-1]["source_node_id"] if fork_history else None,
        "attempt1_poison_exists": (attempt1 / "codex-verify.poison").exists(),
        "attempt2_poison_exists": (attempt2 / "codex-verify.poison").exists() if attempt2 else None,
        "attempt2_success_exists": (attempt2 / "codex_retry_success.txt").exists() if attempt2 else None,
        "fork_history": fork_history,
        "fork_advisor_history": verify["fork_advisor_history"],
        "graph_patch_history": verify["graph_patch_history"],
        "retry_events": result["retry_events"],
        "checkpoint_path": result["checkpoint_path"],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted"]
        and report["codex_attempts"] == 2
        and report["retry_fork_reason"] == "retry_from_accepted_dependency"
        and report["retry_source_node"] == "baseline"
        and len(report["fork_advisor_history"]) == 1
        and report["fork_advisor_history"][0]["selected_node_id"] == "baseline"
        and len(report["graph_patch_history"]) == 1
        and report["graph_patch_history"][0]["compiled"]["accepted"]
        and report["graph_patch_history"][0]["patch"]["operations"][0]["op"] == "retry_node"
        and report["attempt1_poison_exists"]
        and not report["attempt2_poison_exists"]
        and report["attempt2_success_exists"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
