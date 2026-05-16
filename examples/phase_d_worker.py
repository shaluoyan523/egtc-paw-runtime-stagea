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
    poison_on_fail = "--poison-on-fail" in sys.argv
    fail_if_poisoned = "--fail-if-poisoned" in sys.argv
    request_replan_once = "--request-replan-once" in sys.argv
    request_permission_review = "--request-permission-review" in sys.argv
    require_human_review = "--require-human-review" in sys.argv

    time.sleep(delay)
    runtime_attempt_path = Path(".egtc_attempt.json")
    runtime_attempt = 1
    if runtime_attempt_path.exists():
        try:
            runtime_attempt = int(json.loads(runtime_attempt_path.read_text(encoding="utf-8")).get("attempt", 1))
        except Exception:
            runtime_attempt = 1
    attempt_path = Path(f"{node_id}.attempts")
    attempts = int(attempt_path.read_text(encoding="utf-8")) if attempt_path.exists() else 0
    attempt_path.write_text(str(attempts + 1), encoding="utf-8")
    poison_path = Path(f"{node_id}.poison")

    if fail_if_poisoned and poison_path.exists():
        print(
            json.dumps(
                {
                    "type": "test_result",
                    "name": f"{node_id}_poison_guard",
                    "passed": False,
                    "poison_path": str(poison_path),
                }
            )
        )
        return 4

    if fail_once and runtime_attempt == 1:
        if poison_on_fail:
            poison_path.write_text("failed attempt contaminated this workspace\n", encoding="utf-8")
        if request_replan_once:
            Path("overlooker_hint.json").write_text(
                json.dumps(
                    {
                        "recommended_action": "request_director_replan",
                        "failure_type": "missing_diagnostic_node",
                    }
                ),
                encoding="utf-8",
            )
        print(
            json.dumps(
                {
                    "type": "test_result",
                    "name": f"{node_id}_first_attempt",
                    "passed": False,
                    "poisoned": poison_on_fail,
                    "runtime_attempt": runtime_attempt,
                }
            )
        )
        return 2

    if request_permission_review:
        Path("overlooker_hint.json").write_text(
            json.dumps(
                {
                    "recommended_action": "request_permission_review",
                    "failure_type": "permission_review_required",
                }
            ),
            encoding="utf-8",
        )
    elif require_human_review:
        Path("overlooker_hint.json").write_text(
            json.dumps(
                {
                    "recommended_action": "require_human_review",
                    "failure_type": "human_review_required",
                }
            ),
            encoding="utf-8",
        )

    Path(f"{node_id}_{role}.txt").write_text(
        f"{node_id} {role} completed on runtime attempt {runtime_attempt}\n",
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
