from __future__ import annotations

import json
from pathlib import Path

from .artifact_store import ArtifactStore
from .codex_wrapper import CodexExecWrapper
from .models import (
    ActorIdentity,
    CapabilityToken,
    EvidenceBundle,
    NodeCapsule,
    OverlookerReport,
    ValidatorReport,
    WorkerResult,
    to_plain_dict,
)


class CodexOverlooker:
    """Codex-backed node acceptance agent for Stage A."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        actor: ActorIdentity,
        token: CapabilityToken,
        launcher: CodexExecWrapper,
    ) -> None:
        self.artifact_store = artifact_store
        self.actor = actor
        self.token = token
        self.launcher = launcher

    def review(
        self,
        node: NodeCapsule,
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
        worker_result: WorkerResult,
        workspace_diff: dict[str, list[str]],
        overlooker_workspace: Path,
    ) -> OverlookerReport:
        overlooker_workspace.mkdir(parents=True, exist_ok=True)
        packet = self._acceptance_packet(
            node, evidence, validator_reports, worker_result, workspace_diff
        )
        packet_ref = self.artifact_store.put_json(
            packet,
            {"kind": "overlooker_acceptance_packet", "node_id": node.node_id},
            self.actor,
            self.token,
        )
        packet["acceptance_packet_ref"] = to_plain_dict(packet_ref)
        (overlooker_workspace / "acceptance_packet.json").write_text(
            json.dumps(to_plain_dict(packet), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        overlooker_node = NodeCapsule(
            node_id=f"{node.node_id}-overlooker",
            phase="Phase A Overlooker",
            goal="Review the acceptance packet and write overlooker_report.json.",
            command=[],
            acceptance_criteria=[
                "Overlooker must cite evidence_ref.",
                "Overlooker must fail if deterministic validators failed.",
                "Overlooker must write a strict JSON report.",
            ],
            executor_kind="codex_cli",
            prompt=self._prompt(),
        )
        overlooker_run = self.launcher.run(
            overlooker_node, overlooker_workspace, role="overlooker"
        )
        report_data = self._read_report(overlooker_workspace / "overlooker_report.json")
        normalized = self._normalize_report(report_data, evidence, validator_reports)
        report_ref = self.artifact_store.put_json(
            {
                **normalized,
                "overlooker_exit_code": overlooker_run.exit_code,
                "overlooker_event_refs": [
                    to_plain_dict(ref) for ref in overlooker_run.event_refs
                ],
                "acceptance_packet_ref": to_plain_dict(packet_ref),
            },
            {"kind": "overlooker_report", "node_id": node.node_id},
            self.actor,
            self.token,
        )
        return OverlookerReport(
            overlooker_id=overlooker_run.worker_id,
            verdict=normalized["verdict"],
            rationale=normalized["rationale"],
            evidence_ref=normalized.get("evidence_ref"),
            validator_refs=normalized["validator_refs"],
            report_ref=report_ref,
            codex_event_refs=overlooker_run.event_refs,
        )

    def _acceptance_packet(
        self,
        node: NodeCapsule,
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
        worker_result: WorkerResult,
        workspace_diff: dict[str, list[str]],
    ) -> dict[str, object]:
        return {
            "node": {
                "node_id": node.node_id,
                "phase": node.phase,
                "goal": node.goal,
                "acceptance_criteria": node.acceptance_criteria,
                "required_evidence": node.required_evidence,
                "executor_kind": node.executor_kind,
            },
            "worker": {
                "worker_id": worker_result.worker_id,
                "exit_code": worker_result.exit_code,
                "event_refs": [to_plain_dict(ref) for ref in worker_result.event_refs],
            },
            "evidence": to_plain_dict(evidence),
            "validator_reports": to_plain_dict(validator_reports),
            "workspace_diff": workspace_diff,
        }

    def _prompt(self) -> str:
        return """
You are the EGTC-PAW Runtime Stage A Overlooker.

Read ./acceptance_packet.json and create ./overlooker_report.json.
The file must be a strict JSON object with exactly these keys:
{
  "verdict": "pass" | "fail",
  "rationale": "short explanation",
  "evidence_ref": "artifact://...",
  "validator_refs": ["..."]
}

Pass only when:
- evidence.evidence_ref.uri exists and is copied into evidence_ref.
- every validator report has passed=true.
- required evidence includes diff, test, and log artifacts.
- the worker reached submitted state, but node acceptance is based on this Overlooker report.

Fail otherwise. Do not clone repositories. Do not run external tests.
""".strip()

    def _read_report(self, report_path: Path) -> dict[str, object]:
        if not report_path.exists():
            return {
                "verdict": "fail",
                "rationale": "Overlooker did not create overlooker_report.json.",
                "evidence_ref": None,
                "validator_refs": [],
            }
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "verdict": "fail",
                "rationale": f"Overlooker report is not valid JSON: {exc}",
                "evidence_ref": None,
                "validator_refs": [],
            }
        if not isinstance(data, dict):
            return {
                "verdict": "fail",
                "rationale": "Overlooker report is not a JSON object.",
                "evidence_ref": None,
                "validator_refs": [],
            }
        return data

    def _normalize_report(
        self,
        report_data: dict[str, object],
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
    ) -> dict[str, object]:
        evidence_ref = evidence.evidence_ref.uri if evidence.evidence_ref else None
        validator_refs = [report.validator_id for report in validator_reports]
        validators_passed = all(report.passed for report in validator_reports)
        reported_verdict = str(report_data.get("verdict", "")).lower()
        reported_evidence_ref = report_data.get("evidence_ref")

        can_pass = (
            reported_verdict == "pass"
            and validators_passed
            and bool(evidence_ref)
            and reported_evidence_ref == evidence_ref
        )
        if can_pass:
            return {
                "verdict": "pass",
                "rationale": str(report_data.get("rationale") or "Overlooker accepted."),
                "evidence_ref": evidence_ref,
                "validator_refs": validator_refs,
            }

        reasons: list[str] = []
        if reported_verdict != "pass":
            reasons.append(str(report_data.get("rationale") or "Overlooker did not pass."))
        if not validators_passed:
            reasons.append("One or more deterministic validators failed.")
        if not evidence_ref:
            reasons.append("Missing evidence_ref.")
        if reported_evidence_ref != evidence_ref:
            reasons.append("Overlooker did not cite the exact evidence_ref.")
        return {
            "verdict": "fail",
            "rationale": " ".join(reasons),
            "evidence_ref": evidence_ref if reported_evidence_ref == evidence_ref else None,
            "validator_refs": validator_refs,
        }
