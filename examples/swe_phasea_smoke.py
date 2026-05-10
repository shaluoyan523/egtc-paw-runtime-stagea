from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea import StageARuntime
from egtc_runtime_stagea.models import NodeCapsule


def load_swe_stream(split: str):
    from modelscope.msdatasets import MsDataset

    return MsDataset.load(
        "SWE-bench",
        namespace="AI-ModelScope",
        split=split,
        use_streaming=True,
    )


def parse_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def simplicity_score(row: dict[str, Any]) -> int:
    return (
        len(row.get("patch") or "")
        + len(row.get("test_patch") or "") // 2
        + len(row.get("problem_statement") or "") // 4
        + 500 * len(parse_list(row.get("FAIL_TO_PASS")))
    )


def select_simple_cases(split: str, scan_limit: int, count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    dataset = load_swe_stream(split)
    for index, row in enumerate(dataset):
        if index >= scan_limit:
            break
        patch = row.get("patch") or ""
        test_patch = row.get("test_patch") or ""
        fail_to_pass = parse_list(row.get("FAIL_TO_PASS"))
        if not row.get("instance_id") or not row.get("repo") or not patch:
            continue
        if fail_to_pass and len(fail_to_pass) > 3:
            continue
        item = dict(row)
        item["_simplicity_score"] = simplicity_score(item)
        item["_patch_lines"] = len(patch.splitlines())
        item["_test_patch_lines"] = len(test_patch.splitlines())
        item["_problem_chars"] = len(row.get("problem_statement") or "")
        item["_fail_to_pass_count"] = len(fail_to_pass)
        candidates.append(item)
    candidates.sort(key=lambda item: item["_simplicity_score"])
    return candidates[:count]


def write_case(case: dict[str, Any], case_dir: Path) -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    safe_id = str(case["instance_id"]).replace("/", "__")
    case_path = case_dir / f"{safe_id}.json"
    case_path.write_text(json.dumps(case, indent=2, sort_keys=True), encoding="utf-8")
    return case_path


def run_case(runtime: StageARuntime, case: dict[str, Any], case_path: Path) -> dict[str, Any]:
    node = NodeCapsule(
        node_id=f"swe-{case['instance_id']}",
        phase="Phase A SWE smoke",
        goal="Convert one simple SWE-bench case into evidence and overlooker-gated acceptance.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "swe_case_worker.py"),
            "--case",
            str(case_path),
        ],
        acceptance_criteria=[
            "Worker only reaches WorkerSubmitted after processing the case.",
            "Evidence contains log, diff, and passing test report artifacts.",
            "Overlooker pass cites evidence_ref.",
        ],
    )
    return runtime.run_node(node)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--scan-limit", type=int, default=80)
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()

    selected = select_simple_cases(args.split, args.scan_limit, args.count)
    output_root = ROOT / "swe_smoke_data"
    cases_dir = output_root / "cases"
    runtime = StageARuntime(output_root / "runtime_data")

    results = []
    for case in selected:
        case_path = write_case(case, cases_dir)
        run_result = run_case(runtime, case, case_path)
        results.append(
            {
                "instance_id": case["instance_id"],
                "repo": case["repo"],
                "simplicity_score": case["_simplicity_score"],
                "patch_lines": case["_patch_lines"],
                "test_patch_lines": case["_test_patch_lines"],
                "problem_chars": case["_problem_chars"],
                "fail_to_pass_count": case["_fail_to_pass_count"],
                "final_state": run_result["final_state"],
                "run_id": run_result["run_id"],
                "evidence_ref": run_result["evidence"]["evidence_ref"]["uri"],
                "overlooker_verdict": run_result["overlooker_report"]["verdict"],
                "overlooker_evidence_ref": run_result["overlooker_report"]["evidence_ref"],
                "validator_passed": all(
                    report["passed"] for report in run_result["validator_reports"]
                ),
            }
        )

    report = {
        "dataset": "AI-ModelScope/SWE-bench",
        "mode": "static Phase A smoke; official SWE-bench tests are not executed",
        "split": args.split,
        "scan_limit": args.scan_limit,
        "selected_count": len(selected),
        "accepted_count": sum(1 for item in results if item["final_state"] == "NodeAccepted"),
        "results": results,
    }
    report_path = output_root / "swe_phasea_smoke_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted_count"] == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
