from __future__ import annotations

from .models import ConflictResolution, DecisionConflict


class ConflictResolver:
    """Phase E conflict resolver.

    Priority order is intentionally simple and auditable:
    policy / deterministic validator > Overlooker consensus > Director preference.
    """

    def resolve(self, conflict: DecisionConflict) -> ConflictResolution:
        if conflict.policy_findings:
            return ConflictResolution(
                conflict_id=conflict.conflict_id,
                node_id=conflict.node_id,
                decision="blocked",
                rationale="Policy findings override Director and Overlooker preferences.",
                priority="policy",
                required_action="request_permission_review",
                human_review_required=True,
                permission_escalation_required=True,
            )

        failed_validators = [
            report
            for report in conflict.validator_reports
            if not report.passed
        ]
        if failed_validators:
            return ConflictResolution(
                conflict_id=conflict.conflict_id,
                node_id=conflict.node_id,
                decision="rejected",
                rationale="Deterministic validator failure overrides Overlooker pass.",
                priority="validator",
                required_action="retry_or_replan",
            )

        pass_reports = [
            report
            for report in conflict.overlooker_reports
            if report.verdict == "pass" and report.release_overlooker
        ]
        fail_reports = [
            report
            for report in conflict.overlooker_reports
            if report.verdict != "pass" or not report.release_overlooker
        ]
        if pass_reports and not fail_reports:
            return ConflictResolution(
                conflict_id=conflict.conflict_id,
                node_id=conflict.node_id,
                decision="accepted",
                rationale="All Overlookers passed after validator and policy checks.",
                priority="overlooker_consensus",
                required_action="advance",
                accepted_overlooker_id=pass_reports[-1].overlooker_id,
                release_node=True,
            )
        if pass_reports and fail_reports:
            return ConflictResolution(
                conflict_id=conflict.conflict_id,
                node_id=conflict.node_id,
                decision="uncertain",
                rationale="Overlookers disagreed; Phase E requires human review placeholder.",
                priority="overlooker_conflict",
                required_action="require_human_review",
                human_review_required=True,
            )

        action = "retry_or_replan"
        if conflict.overlooker_reports:
            recommended = conflict.overlooker_reports[-1].recommended_action
            if recommended in {
                "request_permission_review",
                "require_human_review",
                "require_second_overlooker",
            }:
                action = recommended
        return ConflictResolution(
            conflict_id=conflict.conflict_id,
            node_id=conflict.node_id,
            decision="rejected",
            rationale="No Overlooker produced a releasable pass.",
            priority="overlooker",
            required_action=action,
            human_review_required=action == "require_human_review",
            permission_escalation_required=action == "request_permission_review",
        )
