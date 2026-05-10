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

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.phaseb_models import CompilerFinding, structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer
from phaseb_swe_complex_demo import objective_from_case, select_complex_cases


CODEX_BIN = "/home/batchcom/.windsurf-server/extensions/openai.chatgpt-26.422.71525/bin/linux-x86_64/codex"


def director_prompt() -> str:
    return """
You are Director Agent v1 for EGTC-PAW Runtime Phase B.

Read ./director_input.json and create ./director_output.json.
The output must be a strict JSON object with exactly these top-level keys:
- task_diagnosis
- workflow_skeleton
- node_instantiations

Required shape:
{
  "task_diagnosis": {
    "task_kind": "director_planning" | "implementation" | "analysis",
    "risk_level": "low" | "medium" | "high",
    "requires_code_change": true | false,
    "requires_tests": true | false,
    "repo_touchpoints": ["..."],
    "unknowns": ["..."]
  },
  "workflow_skeleton": {
    "topology": "linear",
    "nodes": [
      {"node_id": "diagnose", "phase": "diagnosis", "role": "worker", "goal": "...", "depends_on": [], "expected_outputs": ["analysis_log"]},
      {"node_id": "implement", "phase": "implementation", "role": "worker", "goal": "...", "depends_on": ["diagnose"], "expected_outputs": ["diff", "worker_log"]},
      {"node_id": "verify", "phase": "verification", "role": "worker", "goal": "...", "depends_on": ["implement"], "expected_outputs": ["test_report", "validator_ready_evidence"]}
    ],
    "edges": [["diagnose", "implement"], ["implement", "verify"]]
  },
  "node_instantiations": [
    {
      "skeleton_node_id": "diagnose|implement|verify",
      "node_id": "director-codex-...",
      "phase": "diagnosis|implementation|verification",
      "executor_kind": "codex_cli",
      "command": [],
      "acceptance_criteria": ["Worker may only submit results.", "Evidence must include log, diff, and test artifacts when required.", "Overlooker acceptance must cite evidence_ref."],
      "permission_grounding": {
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [] or ["."],
        "allowed_commands": [],
        "grounded_by": ["repo_policy.allowed_read_paths", "repo_policy.allowed_write_paths", "repo_policy.test_commands", "repo_policy.network_allowed_by_default"],
        "justification": "..."
      }
    }
  ]
}

Rules:
- This is a complex SWE-bench code-change planning task, so include diagnose, implement, and verify.
- Do not ask for network access.
- Do not write to sensitive paths listed in repo_policy.sensitive_paths.
- Verification must be read-only and use repo-grounded test commands in allowed_commands.
- Keep the JSON valid. No markdown.
""".strip()


def run_codex_director(workspace: Path) -> tuple[int, list[dict[str, Any]], str, str]:
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
        director_prompt(),
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
    return completed.returncode, events, completed.stdout, completed.stderr


def validate_director_output(output: dict[str, Any], repo_policy: dict[str, Any]) -> list[CompilerFinding]:
    findings: list[CompilerFinding] = []
    for key in ["task_diagnosis", "workflow_skeleton", "node_instantiations"]:
        if key not in output:
            findings.append(CompilerFinding("error", "missing_director_key", f"Missing {key}."))
    if findings:
        return findings

    skeleton_nodes = output["workflow_skeleton"].get("nodes", [])
    skeleton_ids = {node.get("node_id") for node in skeleton_nodes}
    if {"diagnose", "implement", "verify"} - skeleton_ids:
        findings.append(
            CompilerFinding("error", "incomplete_skeleton", "Complex SWE case requires diagnose, implement, and verify.")
        )

    instantiations = output.get("node_instantiations", [])
    instantiated = {item.get("skeleton_node_id") for item in instantiations}
    if skeleton_ids - instantiated:
        findings.append(
            CompilerFinding("error", "missing_instantiation", "Every skeleton node must be instantiated.")
        )

    sensitive = {path.strip("/") for path in repo_policy.get("sensitive_paths", [])}
    test_commands = repo_policy.get("test_commands", [])
    for item in instantiations:
        node_id = item.get("node_id")
        grounding = item.get("permission_grounding", {})
        if grounding.get("network") != "none":
            findings.append(
                CompilerFinding("error", "network_not_grounded", "Director requested network access.", node_id)
            )
        for path in grounding.get("allowed_write_paths", []):
            normalized = str(path).strip("/")
            if normalized in sensitive:
                findings.append(
                    CompilerFinding("error", "sensitive_write_path", f"Sensitive write path: {path}", node_id)
                )
        if item.get("phase") == "verification":
            if grounding.get("allowed_write_paths", []) != []:
                findings.append(
                    CompilerFinding("error", "verification_not_read_only", "Verification must be read-only.", node_id)
                )
            allowed_commands = grounding.get("allowed_commands", [])
            if test_commands and not any(command in test_commands for command in allowed_commands):
                findings.append(
                    CompilerFinding("error", "verification_command_not_grounded", "Verification command must come from repo policy.", node_id)
                )
        if not grounding.get("grounded_by"):
            findings.append(
                CompilerFinding("error", "missing_permission_grounding", "Permission grounding must cite policy sources.", node_id)
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--scan-limit", type=int, default=120)
    parser.add_argument("--case-index", type=int, default=0)
    parser.add_argument("--output-root", default=str(ROOT / "phaseb_codex_director_reports"))
    args = parser.parse_args()

    cases = select_complex_cases(args.split, args.scan_limit, args.case_index + 1)
    if len(cases) <= args.case_index:
        raise SystemExit("No complex SWE case selected.")
    case = cases[args.case_index]
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    objective = objective_from_case(case)

    run_id = f"codex-director-{uuid.uuid4().hex[:10]}"
    output_root = Path(args.output_root)
    workspace = output_root / "runs" / run_id
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    input_packet = {
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
    (workspace / "director_input.json").write_text(
        json.dumps(input_packet, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    exit_code, events, stdout, stderr = run_codex_director(workspace)
    (workspace / "codex_stdout.jsonl").write_text(stdout, encoding="utf-8")
    (workspace / "codex_stderr.txt").write_text(stderr, encoding="utf-8")
    output_path = workspace / "director_output.json"
    if output_path.exists():
        director_output = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        director_output = {}

    findings = validate_director_output(director_output, structured(repo_policy))
    report = {
        "run_id": run_id,
        "codex_exit_code": exit_code,
        "codex_event_count": len(events),
        "case": input_packet["swe_case"],
        "director_output_present": output_path.exists(),
        "accepted": exit_code == 0 and output_path.exists() and not any(f.severity == "error" for f in findings),
        "findings": structured(findings),
        "workspace": str(workspace),
        "director_output": director_output,
    }
    report_path = output_root / "phaseb_codex_director_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
