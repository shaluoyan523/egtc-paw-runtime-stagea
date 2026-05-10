from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    args = parser.parse_args()

    case_path = Path(args.case)
    case = json.loads(case_path.read_text(encoding="utf-8"))
    patch = case.get("patch") or ""
    test_patch = case.get("test_patch") or ""
    fail_to_pass = case.get("FAIL_TO_PASS") or "[]"
    summary = {
        "instance_id": case.get("instance_id"),
        "repo": case.get("repo"),
        "base_commit": case.get("base_commit"),
        "problem_chars": len(case.get("problem_statement") or ""),
        "patch_chars": len(patch),
        "patch_lines": len(patch.splitlines()),
        "test_patch_chars": len(test_patch),
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": case.get("PASS_TO_PASS") or "[]",
    }
    Path("swe_case_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"type": "log", "message": "loaded SWE case", **summary}))
    print(
        json.dumps(
            {
                "type": "test_result",
                "name": "phasea_swe_case_data_contract",
                "passed": bool(case.get("instance_id") and case.get("repo") and patch),
                "checks": [
                    "instance_id_present",
                    "repo_present",
                    "gold_patch_present",
                ],
                "note": "Static Phase A smoke check; this does not run official SWE-bench tests.",
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "worker_submitted",
                "summary": "SWE case converted into Stage A evidence artifacts.",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
