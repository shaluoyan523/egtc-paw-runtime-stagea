from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea import StageARuntime
from egtc_runtime_stagea.models import NodeCapsule


def main() -> int:
    runtime = StageARuntime(ROOT / "runtime_data")
    node = NodeCapsule(
        node_id="stageA-demo-node",
        phase="Phase A",
        goal="Run one worker, collect evidence, validate, and overlooker-accept.",
        command=[sys.executable, str(ROOT / "examples" / "demo_worker.py")],
        acceptance_criteria=[
            "Worker completion only reaches WorkerSubmitted.",
            "Evidence bundle contains diff, test, and log artifact refs.",
            "Overlooker pass cites evidence_ref.",
        ],
    )
    result = runtime.run_node(node)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["final_state"] == "NodeAccepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
