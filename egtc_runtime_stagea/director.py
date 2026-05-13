from __future__ import annotations

import re
import uuid

from .experience import ExperienceLibrary, ExperienceMatch
from .models import NodeCapsule
from .phaseb_models import (
    NodeInstantiation,
    TaskDiagnosis,
    WorkflowBlueprint,
    WorkflowSkeleton,
    WorkflowSkeletonNode,
)
from .repo_policy import RepoPolicy
from .compiler import PermissionGrounder


class DirectorAgentV1:
    """Deterministic Director Agent v1 scaffold.

    The Director emits structured planning objects in three stages:
    TaskDiagnosis -> WorkflowSkeleton -> NodeInstantiation.
    """

    director_id = "director-agent-v1"

    def __init__(self, experience_library: ExperienceLibrary | None = None) -> None:
        self.experience_library = experience_library

    def diagnose(self, objective: str, repo_policy: RepoPolicy) -> TaskDiagnosis:
        lower = objective.lower()
        requires_code_change = any(
            word in lower
            for word in [
                "implement",
                "design",
                "phase b",
                "phaseb",
                "fix",
                "add",
                "change",
                "modify",
                "refactor",
                "build",
                "实现",
                "设计",
                "新增",
                "修改",
                "落地",
                "中控",
            ]
        )
        requires_tests = requires_code_change or any(
            word in lower for word in ["test", "verify", "validate", "校验", "验证", "测试"]
        )
        task_kind = "director_planning" if "director" in lower or "中控" in objective else (
            "implementation" if requires_code_change else "analysis"
        )
        risk_level = "medium" if requires_code_change else "low"
        touchpoints = self._guess_touchpoints(objective, repo_policy)
        unknowns = []
        if not touchpoints:
            unknowns.append("No concrete repo path was named by the task.")
        matches = (
            self.experience_library.retrieve(objective, limit=6)
            if self.experience_library
            else []
        )
        return TaskDiagnosis(
            task_id=f"task-{uuid.uuid4().hex[:10]}",
            objective=objective,
            task_kind=task_kind,
            risk_level=risk_level,
            repo_touchpoints=touchpoints,
            requires_code_change=requires_code_change,
            requires_tests=requires_tests,
            unknowns=unknowns,
            experience_matches=self._serialize_matches(matches),
        )

    def select_skeleton(self, diagnosis: TaskDiagnosis) -> WorkflowSkeleton:
        topology_pattern_ids = self._matched_pattern_ids(diagnosis, "topology")
        review_pattern_ids = self._matched_pattern_ids(diagnosis, "review_loop")
        handoff_pattern_ids = self._matched_pattern_ids(diagnosis, "handoff")
        failure_pattern_ids = self._matched_pattern_ids(diagnosis, "failure_policy")
        matched_pattern_ids = self._matched_pattern_ids(diagnosis)
        if diagnosis.requires_code_change and topology_pattern_ids:
            return self._experience_guided_skeleton(
                diagnosis,
                topology_pattern_ids,
                review_pattern_ids,
                handoff_pattern_ids,
                failure_pattern_ids,
                matched_pattern_ids,
            )

        nodes = [
            WorkflowSkeletonNode(
                node_id="diagnose",
                phase="diagnosis",
                role="worker",
                goal="Inspect task context and identify implementation surface.",
                expected_outputs=["analysis_log"],
                experience_pattern_ids=matched_pattern_ids,
            )
        ]
        edges: list[tuple[str, str]] = []
        if diagnosis.requires_code_change:
            nodes.append(
                WorkflowSkeletonNode(
                    node_id="implement",
                    phase="implementation",
                    role="worker",
                    goal="Make the minimal code change required by the objective.",
                    depends_on=["diagnose"],
                    expected_outputs=["diff", "worker_log"],
                    experience_pattern_ids=matched_pattern_ids,
                )
            )
            edges.append(("diagnose", "implement"))
        if diagnosis.requires_tests:
            nodes.append(
                WorkflowSkeletonNode(
                    node_id="verify",
                    phase="verification",
                    role="worker",
                    goal="Run repo-grounded checks and report test evidence.",
                    depends_on=["implement"] if diagnosis.requires_code_change else ["diagnose"],
                    expected_outputs=["test_report", "validator_ready_evidence"],
                    experience_pattern_ids=review_pattern_ids or matched_pattern_ids,
                )
            )
            edges.append(("implement" if diagnosis.requires_code_change else "diagnose", "verify"))
        return WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology="linear",
            nodes=nodes,
            edges=edges,
            rationale="Director v1 uses a conservative linear workflow for Phase B.",
            experience_pattern_ids=matched_pattern_ids,
            experience_rationale=[
                "No active topology pattern was strong enough to change the conservative linear skeleton."
            ] if matched_pattern_ids else [],
        )

    def instantiate_nodes(
        self,
        diagnosis: TaskDiagnosis,
        skeleton: WorkflowSkeleton,
        repo_policy: RepoPolicy,
    ) -> list[NodeInstantiation]:
        grounder = PermissionGrounder(repo_policy)
        instantiations: list[NodeInstantiation] = []
        for skeleton_node in skeleton.nodes:
            command = self._command_for(skeleton_node.phase, repo_policy)
            node = NodeCapsule(
                node_id=f"{diagnosis.task_id}-{skeleton_node.node_id}",
                phase=skeleton_node.phase,
                goal=skeleton_node.goal,
                command=command,
                acceptance_criteria=[
                    "Worker may only submit results.",
                    "Evidence must include log, diff, and test artifacts when required.",
                    "Overlooker acceptance must cite evidence_ref.",
                ],
                experience_pattern_ids=skeleton_node.experience_pattern_ids,
                executor_kind="subprocess",
            )
            grounding = grounder.derive(node, skeleton_node.phase)
            node.sandbox_profile = grounding.sandbox_profile.__dict__
            instantiations.append(
                NodeInstantiation(
                    node=node,
                    skeleton_node_id=skeleton_node.node_id,
                    permission_grounding=grounding,
                )
            )
        return instantiations

    def plan(self, objective: str, repo_policy: RepoPolicy) -> WorkflowBlueprint:
        diagnosis = self.diagnose(objective, repo_policy)
        skeleton = self.select_skeleton(diagnosis)
        instantiations = self.instantiate_nodes(diagnosis, skeleton, repo_policy)
        return WorkflowBlueprint(
            blueprint_id=f"blueprint-{uuid.uuid4().hex[:10]}",
            director_id=self.director_id,
            task_diagnosis=diagnosis,
            repo_policy=repo_policy,
            workflow_skeleton=skeleton,
            node_instantiations=instantiations,
            experience_pattern_ids=skeleton.experience_pattern_ids,
        )

    def _experience_guided_skeleton(
        self,
        diagnosis: TaskDiagnosis,
        topology_pattern_ids: list[str],
        review_pattern_ids: list[str],
        handoff_pattern_ids: list[str],
        failure_pattern_ids: list[str],
        matched_pattern_ids: list[str],
    ) -> WorkflowSkeleton:
        explorer_patterns = topology_pattern_ids + handoff_pattern_ids
        writer_patterns = topology_pattern_ids + handoff_pattern_ids
        verifier_patterns = review_pattern_ids + failure_pattern_ids + handoff_pattern_ids
        nodes = [
            WorkflowSkeletonNode(
                node_id="explore-context",
                phase="exploration",
                role="explorer",
                goal="Inspect the repository surface and summarize likely implementation touchpoints.",
                expected_outputs=["analysis_log", "touchpoint_map"],
                experience_pattern_ids=explorer_patterns,
            ),
            WorkflowSkeletonNode(
                node_id="explore-tests",
                phase="exploration",
                role="explorer",
                goal="Inspect available tests and validation commands without writing files.",
                expected_outputs=["test_plan", "risk_notes"],
                experience_pattern_ids=explorer_patterns,
            ),
            WorkflowSkeletonNode(
                node_id="implement",
                phase="implementation",
                role="worker",
                goal="Apply the minimal code change after read-only exploration has completed.",
                depends_on=["explore-context", "explore-tests"],
                expected_outputs=["diff", "worker_log"],
                experience_pattern_ids=writer_patterns,
            ),
            WorkflowSkeletonNode(
                node_id="verify",
                phase="verification",
                role="worker",
                goal="Run repo-grounded checks and prepare validator-ready evidence.",
                depends_on=["implement"],
                expected_outputs=["test_report", "validator_ready_evidence"],
                experience_pattern_ids=verifier_patterns,
            ),
        ]
        return WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology="parallel_explore_then_single_writer_then_verify",
            nodes=nodes,
            edges=[
                ("explore-context", "implement"),
                ("explore-tests", "implement"),
                ("implement", "verify"),
            ],
            rationale=(
                "Director selected an experience-guided skeleton: parallel read-only "
                "exploration, one writer, then verification."
            ),
            experience_pattern_ids=matched_pattern_ids,
            experience_rationale=[
                f"Matched topology patterns: {', '.join(topology_pattern_ids)}.",
                "Experience use only shapes workflow structure; compiler and permission grounding remain authoritative.",
            ],
        )

    def _guess_touchpoints(self, objective: str, repo_policy: RepoPolicy) -> list[str]:
        mentioned = re.findall(r"[\w./-]+\\.py|[\w./-]+\\.md|[\w./-]+\\.toml", objective)
        return mentioned or ["."]

    def _command_for(self, phase: str, repo_policy: RepoPolicy) -> list[str]:
        if phase == "verification":
            return repo_policy.test_commands[0]
        return ["python3", "-c", "print('Director Agent v1 node submitted')"]

    def _serialize_matches(self, matches: list[ExperienceMatch]) -> list[dict[str, object]]:
        return [
            {
                "pattern_id": match.pattern.pattern_id,
                "pattern_type": match.pattern.pattern_type,
                "score": match.score,
                "matched_signals": match.matched_signals,
                "description": match.pattern.description,
                "evidence_level": match.pattern.evidence_level,
                "confidence_score": match.pattern.confidence_score,
                "source_refs": match.pattern.source_refs,
            }
            for match in matches
        ]

    def _matched_pattern_ids(
        self,
        diagnosis: TaskDiagnosis,
        pattern_type: str | None = None,
    ) -> list[str]:
        ids: list[str] = []
        for match in diagnosis.experience_matches:
            if pattern_type is not None and match.get("pattern_type") != pattern_type:
                continue
            pattern_id = str(match.get("pattern_id") or "")
            if pattern_id and pattern_id not in ids:
                ids.append(pattern_id)
        return ids
