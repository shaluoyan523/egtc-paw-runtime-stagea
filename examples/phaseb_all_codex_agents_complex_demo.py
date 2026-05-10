from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.phaseb_models import CompilerFinding, structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer
from phaseb_codex_director_complex_demo import (
    CODEX_BIN,
    director_prompt,
    validate_director_output,
)
from phaseb_swe_complex_demo import objective_from_case, select_complex_cases


def run_codex(workspace: Path, prompt: str, role: str) -> dict[str, Any]:
    command = [
        CODEX_BIN,
        "-a",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
        "-s",
        "workspace-write",
        prompt,
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    events: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    (workspace / f"{role}_stdout.jsonl").write_text(completed.stdout, encoding="utf-8")
    (workspace / f"{role}_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    return {"exit_code": completed.returncode, "event_count": len(events)}


def worker_prompt(node: dict[str, Any]) -> str:
    phase = node.get("phase")
    node_id = node.get("node_id")
    return f"""
You are a Codex worker agent for EGTC-PAW Runtime Phase B.

Read ./worker_input.json and create ./worker_report.json.
You are node {node_id} in phase {phase}.

Output strict JSON:
{{
  "node_id": "{node_id}",
  "phase": "{phase}",
  "status": "submitted",
  "summary": "short summary",
  "evidence": ["..."]
}}

Rules:
- Do not clone repositories.
- Do not run external tests.
- Do not write outside this workspace.
- You are not allowed to mark the node accepted. Only submit evidence.
""".strip()


def overlooker_prompt() -> str:
    return """
You are the Codex Overlooker agent for EGTC-PAW Runtime Phase B.

Read ./overlooker_input.json and create ./overlooker_report.json.

Output strict JSON:
{
  "verdict": "pass" | "fail",
  "rationale": "short reason",
  "accepted_worker_reports": ["..."],
  "evidence_ref": "local://overlooker_input.json"
}

Pass only if:
- Director output has diagnose, implement, and verify nodes.
- Every node instantiation has one worker report with status=submitted.
- Every worker report includes evidence.
- Director validation findings are empty.

Do not run external tests. Do not clone repositories.
""".strip()


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--scan-limit", type=int, default=120)
    parser.add_argument("--case-index", type=int, default=0)
    parser.add_argument("--output-root", default=str(ROOT / "phaseb_all_codex_reports"))
    args = parser.parse_args()

    cases = select_complex_cases(args.split, args.scan_limit, args.case_index + 1)
    if len(cases) <= args.case_index:
        raise SystemExit("No complex SWE case selected.")
    case = cases[args.case_index]
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    objective = objective_from_case(case)

    run_id = f"all-codex-{uuid.uuid4().hex[:10]}"
    output_root = Path(args.output_root)
    run_root = output_root / "runs" / run_id
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True)

    director_workspace = run_root / "director"
    director_workspace.mkdir()
    director_input = {
        "objective": objective,
        "swe_case": {
            "instance_id": case["instance_id"],
            "repo": case["repo"],
            "base_commit": case.get("base_commit"),
            "patch_lines": case["_patch_lines"],
            "test_patch_lines": case["_test_patch_lines"],
            "problem_chars": case["_problem_chars"],
            "fail_to_pass_count": case["_fail_to_pass_count"],
            "complexity_score": case["_complexity_score"],
        },
        "repo_policy": structured(repo_policy),
    }
    write_json(director_workspace / "director_input.json", director_input)
    director_run = run_codex(director_workspace, director_prompt(), "director")
    director_output_path = director_workspace / "director_output.json"
    director_output = (
        json.loads(director_output_path.read_text(encoding="utf-8"))
        if director_output_path.exists()
        else {}
    )
    director_findings = validate_director_output(director_output, structured(repo_policy))

    worker_results: list[dict[str, Any]] = []
    for node in director_output.get("node_instantiations", []):
        node_id = str(node.get("node_id"))
        worker_workspace = run_root / "workers" / node_id
        worker_workspace.mkdir(parents=True)
        write_json(
            worker_workspace / "worker_input.json",
            {
                "node": node,
                "objective": objective,
                "swe_case": director_input["swe_case"],
                "director_findings": structured(director_findings),
            },
        )
        worker_run = run_codex(worker_workspace, worker_prompt(node), f"worker_{node_id}")
        report_path = worker_workspace / "worker_report.json"
        worker_report = (
            json.loads(report_path.read_text(encoding="utf-8"))
            if report_path.exists()
            else {}
        )
        worker_results.append(
            {
                "node_id": node_id,
                "workspace": str(worker_workspace),
                "codex": worker_run,
                "report_present": report_path.exists(),
                "worker_report": worker_report,
            }
        )

    overlooker_workspace = run_root / "overlooker"
    overlooker_workspace.mkdir()
    overlooker_input = {
        "director_output": director_output,
        "director_findings": structured(director_findings),
        "worker_results": worker_results,
    }
    write_json(overlooker_workspace / "overlooker_input.json", overlooker_input)
    overlooker_run = run_codex(overlooker_workspace, overlooker_prompt(), "overlooker")
    overlooker_report_path = overlooker_workspace / "overlooker_report.json"
    overlooker_report = (
        json.loads(overlooker_report_path.read_text(encoding="utf-8"))
        if overlooker_report_path.exists()
        else {}
    )

    local_findings: list[CompilerFinding] = []
    if director_run["exit_code"] != 0 or not director_output_path.exists():
        local_findings.append(CompilerFinding("error", "director_failed", "Director Codex did not produce output."))
    local_findings.extend(director_findings)
    for result in worker_results:
        if result["codex"]["exit_code"] != 0:
            local_findings.append(CompilerFinding("error", "worker_failed", "Worker Codex exited non-zero.", result["node_id"]))
        if result["worker_report"].get("status") != "submitted":
            local_findings.append(CompilerFinding("error", "worker_not_submitted", "Worker did not submit.", result["node_id"]))
        if not result["worker_report"].get("evidence"):
            local_findings.append(CompilerFinding("error", "worker_missing_evidence", "Worker report lacks evidence.", result["node_id"]))
    if overlooker_run["exit_code"] != 0 or overlooker_report.get("verdict") != "pass":
        local_findings.append(CompilerFinding("error", "overlooker_failed", "Overlooker did not pass."))

    report = {
        "run_id": run_id,
        "case": director_input["swe_case"],
        "accepted": not any(f.severity == "error" for f in local_findings),
        "agent_sessions": {
            "director": director_run,
            "workers": [
                {"node_id": result["node_id"], **result["codex"]}
                for result in worker_results
            ],
            "overlooker": overlooker_run,
        },
        "director_output_present": director_output_path.exists(),
        "worker_count": len(worker_results),
        "overlooker_report_present": overlooker_report_path.exists(),
        "overlooker_report": overlooker_report,
        "findings": structured(local_findings),
        "workspace": str(run_root),
    }
    report_path = output_root / "phaseb_all_codex_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
