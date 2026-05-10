from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.director import DirectorAgentV1
from egtc_runtime_stagea.phaseb_models import structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


PREFERRED_COMPLEX_REPOS = {
    "django/django",
    "matplotlib/matplotlib",
    "sympy/sympy",
    "scikit-learn/scikit-learn",
    "pytest-dev/pytest",
    "sphinx-doc/sphinx",
    "astropy/astropy",
}


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


def complexity_score(row: dict[str, Any]) -> int:
    repo_bonus = 5000 if row.get("repo") in PREFERRED_COMPLEX_REPOS else 0
    return (
        repo_bonus
        + len(row.get("patch") or "")
        + len(row.get("test_patch") or "")
        + len(row.get("problem_statement") or "") // 2
        + 1000 * len(parse_list(row.get("FAIL_TO_PASS")))
        + 50 * len((row.get("patch") or "").splitlines())
    )


def select_complex_cases(split: str, scan_limit: int, count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    dataset = load_swe_stream(split)
    iterator = iter(dataset)
    try:
        for index in range(scan_limit):
            try:
                row = next(iterator)
            except StopIteration:
                break
            if not row.get("instance_id") or not row.get("repo") or not row.get("patch"):
                continue
            item = dict(row)
            item["_complexity_score"] = complexity_score(item)
            item["_patch_lines"] = len((item.get("patch") or "").splitlines())
            item["_test_patch_lines"] = len((item.get("test_patch") or "").splitlines())
            item["_problem_chars"] = len(item.get("problem_statement") or "")
            item["_fail_to_pass_count"] = len(parse_list(item.get("FAIL_TO_PASS")))
            candidates.append(item)
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    candidates.sort(key=lambda item: item["_complexity_score"], reverse=True)
    return candidates[:count]


def objective_from_case(case: dict[str, Any]) -> str:
    return (
        "Use SWE-bench complex case to test Phase B Director Agent planning. "
        f"Repo: {case['repo']}. "
        f"Instance: {case['instance_id']}. "
        f"Base commit: {case.get('base_commit')}. "
        f"Patch lines: {case['_patch_lines']}. "
        f"Test patch lines: {case['_test_patch_lines']}. "
        f"Problem chars: {case['_problem_chars']}. "
        f"FAIL_TO_PASS count: {case['_fail_to_pass_count']}. "
        "Design an implementation workflow with TaskDiagnosis, WorkflowSkeleton, "
        "NodeInstantiation, PermissionGrounding, structured output, compiler validation, "
        "and repo-grounded verification. This is a complex code-change planning task."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--scan-limit", type=int, default=500)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--output", default=str(ROOT / "phaseb_complex_reports" / "swe_complex_phaseb_report.json"))
    parser.add_argument(
        "--no-fast-exit",
        action="store_true",
        help="Allow normal interpreter shutdown. By default the script uses os._exit after flushing to avoid a ModelScope/datasets finalizer crash seen in this environment.",
    )
    args = parser.parse_args()

    repo_policy = RepoPolicyInferencer().infer(ROOT)
    director = DirectorAgentV1()
    compiler = WorkflowCompiler()
    cases = select_complex_cases(args.split, args.scan_limit, args.count)

    results: list[dict[str, Any]] = []
    for case in cases:
        blueprint = director.plan(objective_from_case(case), repo_policy)
        compiled = compiler.compile(blueprint)
        results.append(
            {
                "instance_id": case["instance_id"],
                "repo": case["repo"],
                "complexity_score": case["_complexity_score"],
                "patch_lines": case["_patch_lines"],
                "test_patch_lines": case["_test_patch_lines"],
                "problem_chars": case["_problem_chars"],
                "fail_to_pass_count": case["_fail_to_pass_count"],
                "compiler_accepted": compiled.accepted,
                "task_diagnosis": structured(blueprint.task_diagnosis),
                "skeleton_nodes": [structured(node) for node in blueprint.workflow_skeleton.nodes],
                "node_permissions": [
                    {
                        "node_id": inst.node.node_id,
                        "phase": inst.node.phase,
                        "sandbox_profile": structured(inst.permission_grounding.sandbox_profile),
                        "grounded_by": inst.permission_grounding.grounded_by,
                    }
                    for inst in blueprint.node_instantiations
                ],
                "compiler_findings": structured(compiled.findings),
            }
        )

    report = {
        "dataset": "AI-ModelScope/SWE-bench",
        "split": args.split,
        "scan_limit": args.scan_limit,
        "selected_count": len(cases),
        "accepted_count": sum(1 for item in results if item["compiler_accepted"]),
        "selection": "highest complexity score by patch/test/problem size, FAIL_TO_PASS count, and preferred large repos",
        "results": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(rendered)
    code = 0 if results and report["accepted_count"] == len(results) else 1
    sys.stdout.flush()
    sys.stderr.flush()
    if not args.no_fast_exit:
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
