from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import to_plain_dict


PATTERN_TYPES = {
    "topology",
    "role_template",
    "handoff",
    "routing",
    "failure_policy",
    "review_loop",
}

PATTERN_STATUSES = {"draft", "active", "deprecated", "rejected"}

OBSERVATION_OUTCOMES = {"accepted", "rejected", "retried", "replanned", "aborted"}

UPDATE_TYPES = {"promote", "demote", "revise", "deprecate", "add_new"}

REVIEW_STATUSES = {"proposed", "accepted", "rejected"}

EVIDENCE_WEIGHT = {
    "direct": 3,
    "strong": 3,
    "mixed": 2,
    "medium": 2,
    "inference": 1,
    "weak": 1,
}


@dataclass
class ExperiencePattern:
    pattern_id: str
    pattern_type: str
    description: str
    applicability_signals: list[str]
    anti_signals: list[str]
    recommended_structure: dict[str, Any]
    required_evidence: list[str]
    risk_notes: list[str]
    source_refs: list[str]
    evidence_level: str
    confidence_score: float
    success_count: int = 0
    failure_count: int = 0
    version: int = 1
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    last_updated_at: float = field(default_factory=time.time)


@dataclass
class ExperienceObservation:
    observation_id: str
    run_id: str
    graph_id: str
    node_id: str
    pattern_ids_used: list[str]
    outcome: str
    validator_findings: list[dict[str, Any]]
    overlooker_verdict: str | None
    failure_type: str | None
    recommended_update: str
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class ExperienceUpdateProposal:
    proposal_id: str
    proposed_by: str
    target_pattern_id: str
    update_type: str
    evidence_refs: list[str]
    rationale: str
    review_status: str = "proposed"
    created_at: float = field(default_factory=time.time)


@dataclass
class ExperienceMatch:
    pattern: ExperiencePattern
    score: float
    matched_signals: list[str]


class ExperienceLibrary:
    """Versioned JSONL-backed experience library for Director planning."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.pattern_path = self.root / "patterns.jsonl"
        self.observation_path = self.root / "observations.jsonl"
        self.proposal_path = self.root / "update_proposals.jsonl"

    def seed_defaults(self) -> list[ExperiencePattern]:
        existing = {pattern.pattern_id for pattern in self.load_patterns(include_inactive=True)}
        seeded: list[ExperiencePattern] = []
        for pattern in default_seed_patterns():
            if pattern.pattern_id in existing:
                continue
            self.add_pattern(pattern)
            seeded.append(pattern)
        return seeded

    def add_pattern(self, pattern: ExperiencePattern) -> None:
        self._validate_pattern(pattern)
        self._append_jsonl(self.pattern_path, to_plain_dict(pattern))

    def load_patterns(self, include_inactive: bool = False) -> list[ExperiencePattern]:
        latest: dict[str, ExperiencePattern] = {}
        for raw in self._read_jsonl(self.pattern_path):
            pattern = ExperiencePattern(**raw)
            current = latest.get(pattern.pattern_id)
            if current is None or (
                pattern.version,
                pattern.last_updated_at,
            ) >= (
                current.version,
                current.last_updated_at,
            ):
                latest[pattern.pattern_id] = pattern
        patterns = sorted(latest.values(), key=lambda pattern: pattern.pattern_id)
        if include_inactive:
            return patterns
        return [pattern for pattern in patterns if pattern.status == "active"]

    def get_patterns(self, pattern_ids: list[str]) -> list[ExperiencePattern]:
        wanted = set(pattern_ids)
        return [pattern for pattern in self.load_patterns() if pattern.pattern_id in wanted]

    def retrieve(
        self,
        objective: str,
        *,
        limit: int = 5,
        pattern_types: set[str] | None = None,
    ) -> list[ExperienceMatch]:
        text = objective.lower()
        matches: list[ExperienceMatch] = []
        for pattern in self.load_patterns():
            if pattern_types and pattern.pattern_type not in pattern_types:
                continue
            anti_hits = [signal for signal in pattern.anti_signals if signal.lower() in text]
            if anti_hits:
                continue
            signal_hits = [
                signal for signal in pattern.applicability_signals if signal.lower() in text
            ]
            tag_hits = [tag for tag in pattern.tags if tag.lower() in text]
            if not signal_hits and not tag_hits:
                continue
            evidence = EVIDENCE_WEIGHT.get(pattern.evidence_level.lower(), 1)
            outcome_score = pattern.success_count - pattern.failure_count
            score = (
                len(signal_hits) * 2
                + len(tag_hits)
                + evidence
                + pattern.confidence_score
                + max(-3, min(3, outcome_score))
            )
            matches.append(
                ExperienceMatch(
                    pattern=pattern,
                    score=score,
                    matched_signals=signal_hits + tag_hits,
                )
            )
        matches.sort(key=lambda match: (-match.score, match.pattern.pattern_id))
        return matches[:limit]

    def record_observation(self, observation: ExperienceObservation) -> None:
        self._validate_observation(observation)
        self._append_jsonl(self.observation_path, to_plain_dict(observation))

    def propose_update(self, proposal: ExperienceUpdateProposal) -> None:
        self._validate_proposal(proposal)
        self._append_jsonl(self.proposal_path, to_plain_dict(proposal))

    def load_observations(self) -> list[ExperienceObservation]:
        return [
            ExperienceObservation(**raw)
            for raw in self._read_jsonl(self.observation_path)
        ]

    def load_update_proposals(self) -> list[ExperienceUpdateProposal]:
        return [
            ExperienceUpdateProposal(**raw)
            for raw in self._read_jsonl(self.proposal_path)
        ]

    def accept_update_proposal(
        self,
        proposal: ExperienceUpdateProposal,
        *,
        reviewer: str,
    ) -> ExperiencePattern | None:
        if proposal.review_status != "proposed":
            raise ValueError("Only proposed experience updates can be accepted")
        active = {pattern.pattern_id: pattern for pattern in self.load_patterns()}
        target = active.get(proposal.target_pattern_id)
        if target is None:
            if proposal.update_type != "add_new":
                raise ValueError(f"Unknown active pattern: {proposal.target_pattern_id}")
            return None

        confidence_delta = {
            "promote": 0.5,
            "demote": -1.0,
            "revise": -0.25,
            "deprecate": -2.0,
            "add_new": 0.0,
        }[proposal.update_type]
        status = "deprecated" if proposal.update_type == "deprecate" else target.status
        updated = ExperiencePattern(
            pattern_id=target.pattern_id,
            pattern_type=target.pattern_type,
            description=target.description,
            applicability_signals=target.applicability_signals,
            anti_signals=target.anti_signals,
            recommended_structure=target.recommended_structure,
            required_evidence=target.required_evidence,
            risk_notes=target.risk_notes
            + [
                f"Accepted {proposal.update_type} by {reviewer}: {proposal.rationale}",
            ],
            source_refs=target.source_refs + proposal.evidence_refs,
            evidence_level=target.evidence_level,
            confidence_score=max(
                0.0,
                min(10.0, target.confidence_score + confidence_delta),
            ),
            success_count=target.success_count
            + (1 if proposal.update_type == "promote" else 0),
            failure_count=target.failure_count
            + (1 if proposal.update_type in {"demote", "deprecate"} else 0),
            version=target.version + 1,
            status=status,
            tags=target.tags,
            last_updated_at=time.time(),
        )
        self.add_pattern(updated)
        reviewed = ExperienceUpdateProposal(
            proposal_id=proposal.proposal_id,
            proposed_by=proposal.proposed_by,
            target_pattern_id=proposal.target_pattern_id,
            update_type=proposal.update_type,
            evidence_refs=proposal.evidence_refs,
            rationale=f"{proposal.rationale} Reviewed by {reviewer}.",
            review_status="accepted",
            created_at=time.time(),
        )
        self.propose_update(reviewed)
        return updated

    def update_from_observation(
        self,
        observation: ExperienceObservation,
        *,
        proposed_by: str = "runtime",
    ) -> list[ExperienceUpdateProposal]:
        proposals: list[ExperienceUpdateProposal] = []
        if not observation.pattern_ids_used:
            return proposals
        if observation.outcome in {"accepted", "retried"}:
            update_type = "promote"
            rationale = "Pattern usage produced an accepted or recoverable node outcome."
        elif observation.outcome in {"rejected", "aborted"}:
            update_type = "demote"
            rationale = "Pattern usage contributed to a rejected or aborted node outcome."
        else:
            update_type = "revise"
            rationale = "Pattern usage required replanning and should be reviewed."
        if observation.failure_type:
            rationale += f" failure_type={observation.failure_type}."
        for pattern_id in observation.pattern_ids_used:
            proposal = ExperienceUpdateProposal(
                proposal_id=f"exp-proposal-{uuid.uuid4().hex[:12]}",
                proposed_by=proposed_by,
                target_pattern_id=pattern_id,
                update_type=update_type,
                evidence_refs=observation.evidence_refs,
                rationale=rationale,
            )
            self.propose_update(proposal)
            proposals.append(proposal)
        return proposals

    def _validate_pattern(self, pattern: ExperiencePattern) -> None:
        if pattern.pattern_type not in PATTERN_TYPES:
            raise ValueError(f"Unknown experience pattern_type: {pattern.pattern_type}")
        if pattern.status not in PATTERN_STATUSES:
            raise ValueError(f"Unknown experience status: {pattern.status}")
        if not pattern.pattern_id:
            raise ValueError("ExperiencePattern requires pattern_id")
        if not pattern.description:
            raise ValueError("ExperiencePattern requires description")
        if not 0 <= pattern.confidence_score <= 10:
            raise ValueError("confidence_score must be between 0 and 10")

    def _validate_observation(self, observation: ExperienceObservation) -> None:
        if observation.outcome not in OBSERVATION_OUTCOMES:
            raise ValueError(f"Unknown experience outcome: {observation.outcome}")
        if not observation.observation_id:
            raise ValueError("ExperienceObservation requires observation_id")

    def _validate_proposal(self, proposal: ExperienceUpdateProposal) -> None:
        if proposal.update_type not in UPDATE_TYPES:
            raise ValueError(f"Unknown experience update_type: {proposal.update_type}")
        if proposal.review_status not in REVIEW_STATUSES:
            raise ValueError(f"Unknown experience review_status: {proposal.review_status}")
        if not proposal.target_pattern_id:
            raise ValueError("ExperienceUpdateProposal requires target_pattern_id")

    def _append_jsonl(self, path: Path, item: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if isinstance(raw, dict):
                rows.append(raw)
        return rows


def default_seed_patterns() -> list[ExperiencePattern]:
    """Seed patterns distilled from the bundled multi-agent orchestration report."""

    source = "多agent编排报告_20260421.zip"
    return [
        ExperiencePattern(
            pattern_id="seed-topology-parallel-explore-implement-verify",
            pattern_type="topology",
            description="Use parallel read-only exploration before a single writer and verification gate for complex engineering work.",
            applicability_signals=[
                "复杂",
                "complex",
                "swe",
                "多 worker",
                "多agent",
                "测试",
                "验证",
                "实现",
                "修改",
            ],
            anti_signals=["简单", "single file", "只读总结"],
            recommended_structure={
                "topology": "parallel_explore_then_single_writer_then_verify",
                "nodes": ["explore", "implement", "verify"],
                "join_policy": "all_explorers_before_writer",
            },
            required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
            risk_notes=[
                "Parallelism should be read-only before the writer lock.",
                "Do not create extra workers when the task has no dependency or uncertainty signal.",
            ],
            source_refs=[
                f"{source}: research/comparison_matrix_source.md",
                f"{source}: site/open_source_router_scheduler_task_graph.html",
            ],
            evidence_level="strong",
            confidence_score=7.0,
            tags=["director", "workflow", "parallel", "worker"],
        ),
        ExperiencePattern(
            pattern_id="seed-role-overlooker-review-rework",
            pattern_type="review_loop",
            description="Treat review and rework as a first-class loop with explicit evidence citation and re-entry target.",
            applicability_signals=[
                "review",
                "overlooker",
                "校验",
                "验证",
                "返工",
                "失败",
                "retry",
                "rework",
            ],
            anti_signals=["无需测试", "no review"],
            recommended_structure={
                "review_object": "evidence_bundle",
                "gate": "overlooker_acceptance",
                "reentry": ["retry_same_node", "request_director_replan"],
            },
            required_evidence=["evidence_ref", "validator_refs", "overlooker_report"],
            risk_notes=[
                "Review must cite evidence_ref.",
                "Repeated same-failure retries should demote the pattern or request replan.",
            ],
            source_refs=[
                f"{source}: site/engineering_review_and_rework_loops.html",
                f"{source}: site/open_source_failure_escalation_retry_and_fallback.html",
            ],
            evidence_level="strong",
            confidence_score=8.0,
            tags=["overlooker", "review", "retry", "validation"],
        ),
        ExperiencePattern(
            pattern_id="seed-handoff-artifact-chain",
            pattern_type="handoff",
            description="Use artifact-chain handoff for software engineering nodes where the next step consumes code, logs, test output, and evidence refs.",
            applicability_signals=[
                "artifact",
                "evidence",
                "工件",
                "证据",
                "swe",
                "patch",
                "代码",
            ],
            anti_signals=["pure chat", "无产物"],
            recommended_structure={
                "handoff_unit": "artifact_chain",
                "state_transfer": ["workspace_diff", "test_report", "evidence_ref"],
                "receiver": "next_graph_node_or_overlooker",
            },
            required_evidence=["diff", "test", "log"],
            risk_notes=[
                "Artifact handoff is not a substitute for checkpointed workspace state.",
            ],
            source_refs=[
                f"{source}: site/handoff_contracts_and_state_transfer.html",
                f"{source}: research/software_company_orchestration_source.md",
            ],
            evidence_level="mixed",
            confidence_score=6.0,
            tags=["handoff", "artifact", "state"],
        ),
        ExperiencePattern(
            pattern_id="seed-failure-watchdog-budget",
            pattern_type="failure_policy",
            description="Bound retries with same-failure counters, attempt limits, timeout/resource reports, and explicit stop or replan events.",
            applicability_signals=[
                "retry",
                "失败",
                "超时",
                "budget",
                "livelock",
                "deadlock",
                "重试",
                "重规划",
            ],
            anti_signals=["one shot", "不重试"],
            recommended_structure={
                "guards": ["max_attempts", "retry_budget", "max_same_failure_retries"],
                "escalation": ["abort_livelock", "request_director_replan"],
            },
            required_evidence=["resource_report", "sandbox_events", "failure_type"],
            risk_notes=[
                "Do not retry indefinitely.",
                "Fork retries from accepted upstream workspaces when possible.",
            ],
            source_refs=[
                f"{source}: site/open_source_stop_timeout_watchdog_and_budget_guards.html",
                f"{source}: site/open_source_failure_escalation_retry_and_fallback.html",
            ],
            evidence_level="strong",
            confidence_score=8.0,
            tags=["retry", "watchdog", "budget", "replan"],
        ),
        ExperiencePattern(
            pattern_id="seed-routing-message-or-graph-edge",
            pattern_type="routing",
            description="Select routing surfaces by task shape: graph edges for explicit dependencies, message/team routing for deliberation, planner recipient routing for tool-specialized workers.",
            applicability_signals=[
                "routing",
                "调度",
                "任务图",
                "graph",
                "依赖",
                "worker",
                "specialist",
            ],
            anti_signals=["单步", "linear only"],
            recommended_structure={
                "dependency_tasks": "graph_edges",
                "deliberation_tasks": "message_publication",
                "tool_specialized_tasks": "planner_recipient_set",
            },
            required_evidence=["workflow_skeleton", "edges", "node_roles"],
            risk_notes=[
                "Routing choice must be compiled against known node ids and DAG rules.",
            ],
            source_refs=[
                f"{source}: site/open_source_router_scheduler_task_graph.html",
                f"{source}: research/open_source_control_loops_source.md",
            ],
            evidence_level="mixed",
            confidence_score=6.5,
            tags=["routing", "scheduler", "graph"],
        ),
    ]
