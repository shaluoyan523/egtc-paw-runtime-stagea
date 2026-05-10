from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.artifact_store import ArtifactStore
from egtc_runtime_stagea.codex_wrapper import CodexExecWrapper
from egtc_runtime_stagea.identity import IdentityService
from egtc_runtime_stagea.models import ArtifactRef, NodeCapsule, WorkerResult, to_plain_dict
from egtc_runtime_stagea.phaseb_models import CompilerFinding, structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer
from phaseb_codex_director_complex_demo import (
    director_prompt,
    validate_director_output,
)
from phaseb_swe_complex_demo import objective_from_case, select_complex_cases


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"_parse_error": str(exc)}
    return value if isinstance(value, dict) else {"_parse_error": "JSON root is not an object"}


def sandbox_profile(timeout_sec: int) -> dict[str, Any]:
    return {
        "backend": "codex_native",
        "sandbox_mode": "workspace_write",
        "network": "none",
        "resource_limits": {
            "wall_time_sec": timeout_sec,
            "memory_mb": 1024,
            "disk_mb": 1024,
            "max_processes": 64,
            "max_command_count": 1,
        },
    }


def codex_node(
    node_id: str,
    phase: str,
    goal: str,
    prompt: str,
    timeout_sec: int,
) -> NodeCapsule:
    return NodeCapsule(
        node_id=node_id,
        phase=phase,
        goal=goal,
        command=[],
        acceptance_criteria=[
            "Codex agent must submit only artifacts, not final acceptance.",
            "Phase C sandbox events must be captured.",
            "Phase C resource report must be captured.",
        ],
        required_evidence=["log", "sandbox_events", "resource_report"],
        executor_kind="codex_cli",
        prompt=prompt,
        sandbox_profile=sandbox_profile(timeout_sec),
    )


def worker_prompt(node: dict[str, Any]) -> str:
    node_id = str(node.get("node_id") or node.get("skeleton_node_id") or "worker")
    phase = str(node.get("phase") or "implementation")
    return f"""
You are a Codex worker agent for EGTC-PAW Runtime Phase C.

Read ./worker_input.json and create ./worker_report.json.
You are node {node_id} in phase {phase}.

Write strict JSON with this shape:
{{
  "node_id": "{node_id}",
  "phase": "{phase}",
  "status": "submitted",
  "summary": "short summary",
  "evidence": ["specific evidence item", "specific evidence item"]
}}

Rules:
- Do not clone repositories.
- Do not run external tests.
- Do not write outside this workspace.
- You are not allowed to mark the node accepted. Only submit evidence.
- Keep the JSON valid. No markdown.
""".strip()


def overlooker_prompt() -> str:
    return """
You are the Codex overlooker agent for EGTC-PAW Runtime Phase C.

Read ./overlooker_input.json and create ./overlooker_report.json.

Write strict JSON with this shape:
{
  "verdict": "pass",
  "rationale": "short reason",
  "accepted_worker_reports": ["..."],
  "phasec_checks": ["..."],
  "evidence_ref": "local://overlooker_input.json"
}

Pass only if:
- Director output has diagnose, implement, and verify nodes.
- Every node instantiation has one worker report with status=submitted.
- Every worker report includes evidence.
- Director validation findings are empty.
- Every Codex agent session has sandbox_events_collected=true and resource_report_collected=true.

If any required condition is missing, set verdict to "fail".
Do not run external tests. Do not clone repositories. No markdown.
""".strip()


def safe_path_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def artifact_jsonl(
    store: ArtifactStore,
    ref: ArtifactRef,
    actor,
    token,
) -> list[dict[str, Any]]:
    raw = store.get_bytes(ref, actor, token).decode("utf-8")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = {"_parse_error": line}
        if isinstance(event, dict):
            events.append(event)
    return events


def phasec_artifact_check(
    store: ArtifactStore,
    actor,
    token,
    role: str,
    node: NodeCapsule,
    result: WorkerResult,
) -> dict[str, Any]:
    sandbox_events: list[dict[str, Any]] = []
    resource_report: dict[str, Any] = {}
    sandbox_ref = result.sandbox_event_refs[0] if result.sandbox_event_refs else None
    resource_ref = result.resource_report_ref

    sandbox_events_collected = bool(sandbox_ref and store.verify(sandbox_ref))
    if sandbox_ref:
        sandbox_events = artifact_jsonl(store, sandbox_ref, actor, token)

    resource_report_collected = bool(resource_ref and store.verify(resource_ref))
    if resource_ref:
        loaded = store.get_json(resource_ref, actor, token)
        if isinstance(loaded, dict):
            resource_report = loaded

    event_types = {event.get("event_type") for event in sandbox_events}
    required_event_types = {"sandbox_started", "network_policy_applied", "process_exit"}
    resource_fields = {"wall_time_sec", "cpu_time_sec", "command_count", "timeout_killed"}
    sandbox_summary = {
        "sandbox_events_collected": sandbox_events_collected,
        "sandbox_event_count": len(sandbox_events),
        "sandbox_event_types": sorted(str(item) for item in event_types if item),
        "required_sandbox_events_present": required_event_types.issubset(event_types),
        "network_none_event": any(
            event.get("event_type") == "network_policy_applied"
            and event.get("details", {}).get("mode") == "none"
            for event in sandbox_events
        ),
    }
    resource_summary = {
        "resource_report_collected": resource_report_collected,
        "resource_report_fields_present": resource_fields.issubset(resource_report),
        "timeout_killed": bool(resource_report.get("timeout_killed")),
        "command_count": resource_report.get("command_count"),
        "wall_time_sec": resource_report.get("wall_time_sec"),
    }
    passed = (
        result.exit_code == 0
        and sandbox_summary["sandbox_events_collected"]
        and sandbox_summary["required_sandbox_events_present"]
        and sandbox_summary["network_none_event"]
        and resource_summary["resource_report_collected"]
        and resource_summary["resource_report_fields_present"]
        and not resource_summary["timeout_killed"]
    )
    return {
        "role": role,
        "node_id": node.node_id,
        "codex_agent_id": result.worker_id,
        "exit_code": result.exit_code,
        "codex_event_count": len(result.parsed_events),
        "phasec_passed": passed,
        "sandbox": sandbox_summary,
        "resource": resource_summary,
        "sandbox_event_refs": [to_plain_dict(ref) for ref in result.sandbox_event_refs],
        "resource_report_ref": to_plain_dict(resource_ref) if resource_ref else None,
    }


def append_phasec_findings(
    findings: list[CompilerFinding],
    check: dict[str, Any],
) -> None:
    node_id = str(check["node_id"])
    if check["exit_code"] != 0:
        findings.append(CompilerFinding("error", "codex_agent_failed", "Codex session exited non-zero.", node_id))
    if not check["sandbox"]["sandbox_events_collected"]:
        findings.append(CompilerFinding("error", "sandbox_events_missing", "No sandbox event artifact was collected.", node_id))
    if not check["sandbox"]["required_sandbox_events_present"]:
        findings.append(CompilerFinding("error", "sandbox_events_incomplete", "Required sandbox lifecycle events are missing.", node_id))
    if not check["sandbox"]["network_none_event"]:
        findings.append(CompilerFinding("error", "network_none_missing", "No network:none sandbox event was recorded.", node_id))
    if not check["resource"]["resource_report_collected"]:
        findings.append(CompilerFinding("error", "resource_report_missing", "No resource report artifact was collected.", node_id))
    if not check["resource"]["resource_report_fields_present"]:
        findings.append(CompilerFinding("error", "resource_report_incomplete", "Resource report is missing required fields.", node_id))
    if check["resource"]["timeout_killed"]:
        findings.append(CompilerFinding("error", "agent_timeout", "Codex session hit the Phase C wall-time limit.", node_id))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--scan-limit", type=int, default=120)
    parser.add_argument("--case-index", type=int, default=0)
    parser.add_argument("--agent-timeout-sec", type=int, default=240)
    parser.add_argument("--output-root", default=str(ROOT / "phasec_complex_reports"))
    parser.add_argument(
        "--no-fast-exit",
        action="store_true",
        help="Allow normal interpreter shutdown instead of os._exit after flushing output.",
    )
    args = parser.parse_args()

    cases = select_complex_cases(args.split, args.scan_limit, args.case_index + 1)
    if len(cases) <= args.case_index:
        raise SystemExit("No complex SWE case selected.")
    case = cases[args.case_index]
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    objective = objective_from_case(case)

    run_id = f"phasec-all-codex-{uuid.uuid4().hex[:10]}"
    output_root = Path(args.output_root)
    run_root = output_root / "runs" / run_id
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True)

    identity = IdentityService()
    runtime_actor = identity.actor("runtime-phasec-complex", "runtime")
    runtime_token = identity.issue_token(runtime_actor, ["artifact:read", "artifact:write"])
    artifacts = ArtifactStore(run_root / "artifacts", identity)
    wrapper = CodexExecWrapper(artifacts, runtime_actor, runtime_token)

    swe_case = {
        "instance_id": case["instance_id"],
        "repo": case["repo"],
        "base_commit": case.get("base_commit"),
        "patch_lines": case["_patch_lines"],
        "test_patch_lines": case["_test_patch_lines"],
        "problem_chars": case["_problem_chars"],
        "fail_to_pass_count": case["_fail_to_pass_count"],
        "complexity_score": case["_complexity_score"],
    }

    director_workspace = run_root / "director"
    director_workspace.mkdir()
    director_input = {
        "objective": objective,
        "swe_case": swe_case,
        "repo_policy": structured(repo_policy),
        "phasec_test_requirement": "All agents must run through CodexExecWrapper and emit sandbox/resource artifacts.",
    }
    write_json(director_workspace / "director_input.json", director_input)
    director_node = codex_node(
        "phasec-director-agent",
        "director",
        "Create a structured workflow for a complex SWE-bench task.",
        director_prompt(),
        args.agent_timeout_sec,
    )
    director_result = wrapper.run(
        director_node,
        director_workspace,
        role="director",
        run_id=run_id,
    )
    director_output_path = director_workspace / "director_output.json"
    director_output = read_json(director_output_path)
    director_findings = validate_director_output(director_output, structured(repo_policy))
    phasec_checks = [
        phasec_artifact_check(
            artifacts,
            runtime_actor,
            runtime_token,
            "director",
            director_node,
            director_result,
        )
    ]

    worker_results: list[dict[str, Any]] = []
    for node_spec in director_output.get("node_instantiations", []):
        if not isinstance(node_spec, dict):
            continue
        node_id = str(node_spec.get("node_id") or node_spec.get("skeleton_node_id") or "worker")
        worker_workspace = run_root / "workers" / safe_path_name(node_id)
        worker_workspace.mkdir(parents=True)
        write_json(
            worker_workspace / "worker_input.json",
            {
                "node": node_spec,
                "objective": objective,
                "swe_case": swe_case,
                "director_findings": structured(director_findings),
                "phasec_requirement": "Submit evidence while CodexExecWrapper records sandbox_events and resource_report artifacts.",
            },
        )
        worker_node = codex_node(
            f"phasec-worker-{safe_path_name(node_id)}",
            str(node_spec.get("phase") or "worker"),
            str(node_spec.get("goal") or "Submit worker evidence for this workflow node."),
            worker_prompt(node_spec),
            args.agent_timeout_sec,
        )
        worker_result = wrapper.run(
            worker_node,
            worker_workspace,
            role="worker",
            run_id=run_id,
        )
        report_path = worker_workspace / "worker_report.json"
        worker_report = read_json(report_path)
        worker_check = phasec_artifact_check(
            artifacts,
            runtime_actor,
            runtime_token,
            "worker",
            worker_node,
            worker_result,
        )
        phasec_checks.append(worker_check)
        worker_results.append(
            {
                "node_id": node_id,
                "workspace": str(worker_workspace),
                "report_present": report_path.exists(),
                "worker_report": worker_report,
                "phasec": worker_check,
            }
        )

    overlooker_workspace = run_root / "overlooker"
    overlooker_workspace.mkdir()
    overlooker_input = {
        "director_output": director_output,
        "director_findings": structured(director_findings),
        "worker_results": worker_results,
        "phasec_checks": phasec_checks,
    }
    write_json(overlooker_workspace / "overlooker_input.json", overlooker_input)
    overlooker_node = codex_node(
        "phasec-overlooker",
        "overlooker",
        "Review all Codex worker reports and Phase C sandbox/resource checks.",
        overlooker_prompt(),
        args.agent_timeout_sec,
    )
    overlooker_result = wrapper.run(
        overlooker_node,
        overlooker_workspace,
        role="overlooker",
        run_id=run_id,
    )
    overlooker_report_path = overlooker_workspace / "overlooker_report.json"
    overlooker_report = read_json(overlooker_report_path)
    overlooker_check = phasec_artifact_check(
        artifacts,
        runtime_actor,
        runtime_token,
        "overlooker",
        overlooker_node,
        overlooker_result,
    )
    phasec_checks.append(overlooker_check)

    local_findings: list[CompilerFinding] = []
    if director_result.exit_code != 0 or not director_output_path.exists():
        local_findings.append(
            CompilerFinding("error", "director_failed", "Director Agent did not produce director_output.json.")
        )
    local_findings.extend(director_findings)
    for check in phasec_checks:
        append_phasec_findings(local_findings, check)
    for result in worker_results:
        node_id = str(result["node_id"])
        report = result["worker_report"]
        if not result["report_present"]:
            local_findings.append(CompilerFinding("error", "worker_report_missing", "Worker did not write worker_report.json.", node_id))
        if report.get("status") != "submitted":
            local_findings.append(CompilerFinding("error", "worker_not_submitted", "Worker report status is not submitted.", node_id))
        if not report.get("evidence"):
            local_findings.append(CompilerFinding("error", "worker_missing_evidence", "Worker report lacks evidence.", node_id))
    if overlooker_result.exit_code != 0 or overlooker_report.get("verdict") != "pass":
        local_findings.append(
            CompilerFinding("error", "overlooker_failed", "overlooker did not pass.")
        )

    report = {
        "run_id": run_id,
        "dataset": "AI-ModelScope/SWE-bench",
        "case": swe_case,
        "accepted": not any(finding.severity == "error" for finding in local_findings),
        "all_agents_are_codex": True,
        "codex_sessions": {
            "director": {
                "node_id": director_node.node_id,
                "codex_agent_id": director_result.worker_id,
                "exit_code": director_result.exit_code,
                "event_count": len(director_result.parsed_events),
            },
            "workers": [
                {
                    "node_id": item["phasec"]["node_id"],
                    "codex_agent_id": item["phasec"]["codex_agent_id"],
                    "exit_code": item["phasec"]["exit_code"],
                    "event_count": item["phasec"]["codex_event_count"],
                }
                for item in worker_results
            ],
            "overlooker": {
                "node_id": overlooker_node.node_id,
                "codex_agent_id": overlooker_result.worker_id,
                "exit_code": overlooker_result.exit_code,
                "event_count": len(overlooker_result.parsed_events),
            },
        },
        "director_output_present": director_output_path.exists(),
        "director_validation_findings": structured(director_findings),
        "worker_count": len(worker_results),
        "worker_results": worker_results,
        "overlooker_report_present": overlooker_report_path.exists(),
        "overlooker_report": overlooker_report,
        "phasec_checks": phasec_checks,
        "findings": structured(local_findings),
        "workspace": str(run_root),
    }
    report_path = output_root / "phasec_all_codex_complex_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    code = 0 if report["accepted"] else 1
    sys.stdout.flush()
    sys.stderr.flush()
    if not args.no_fast_exit:
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
