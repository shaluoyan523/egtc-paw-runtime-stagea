from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    role = sys.argv[1]
    node_id = sys.argv[2]
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    fail_once = "--fail-once" in sys.argv

    time.sleep(delay)
    attempt_path = Path(f"{node_id}.attempts")
    attempts = int(attempt_path.read_text(encoding="utf-8")) if attempt_path.exists() else 0
    attempt_path.write_text(str(attempts + 1), encoding="utf-8")

    if fail_once and attempts == 0:
        print(
            json.dumps(
                {
                    "type": "test_result",
                    "name": f"{node_id}_first_attempt",
                    "passed": False,
                }
            )
        )
        return 2

    Path(f"{node_id}_{role}.txt").write_text(
        f"{node_id} {role} completed on attempt {attempts + 1}\n",
        encoding="utf-8",
    )
    Path("phasea_test_result.json").write_text(
        json.dumps({"passed": True, "name": f"{node_id}_contract"}),
        encoding="utf-8",
    )
    print(json.dumps({"type": "log", "message": f"{node_id} completed"}))
    print(json.dumps({"type": "test_result", "name": f"{node_id}_contract", "passed": True}))
    print(
        json.dumps(
            {
                "type": "worker_submitted",
                "summary": f"{node_id} submitted",
                "pid": os.getpid(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
