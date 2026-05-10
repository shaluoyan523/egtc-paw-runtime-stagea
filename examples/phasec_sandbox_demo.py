from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea import StageARuntime
from egtc_runtime_stagea.models import NodeCapsule


def main() -> int:
    runtime = StageARuntime(ROOT / "phasec_sandbox_data")
    node = NodeCapsule(
        node_id="phasec-sandbox-demo",
        phase="Phase C",
        goal="Exercise sandbox backend mapping, network none event, and resource reporting.",
        command=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import json; "
                "Path('phasec_output.txt').write_text('sandbox ok\\n'); "
                "Path('phasea_test_result.json').write_text(json.dumps({'passed': True, 'name': 'phasec_sandbox_demo'})); "
                "print(json.dumps({'type':'test_result','name':'phasec_worker','passed':True}))"
            ),
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains sandbox_events and resource_report artifacts.",
            "Overlooker pass cites evidence_ref.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile={
            "backend": "codex_native",
            "sandbox_mode": "workspace_write",
            "network": "none",
            "resource_limits": {
                "wall_time_sec": 20,
                "memory_mb": 512,
                "disk_mb": 256,
                "max_processes": 32,
                "max_command_count": 5,
            },
        },
    )
    result = runtime.run_node(node)
    resource_ref = result["evidence"]["artifacts"]["resource_report"]
    sandbox_ref = result["evidence"]["artifacts"]["sandbox_events"]
    print(
        json.dumps(
            {
                "final_state": result["final_state"],
                "run_id": result["run_id"],
                "resource_report_ref": resource_ref["uri"],
                "sandbox_events_ref": sandbox_ref["uri"],
                "validators": [
                    (report["validator_id"], report["passed"])
                    for report in result["validator_reports"]
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result["final_state"] == "NodeAccepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
