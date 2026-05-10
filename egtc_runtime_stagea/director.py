from __future__ import annotations

import re
import uuid

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
        return TaskDiagnosis(
            task_id=f"task-{uuid.uuid4().hex[:10]}",
            objective=objective,
            task_kind=task_kind,
            risk_level=risk_level,
            repo_touchpoints=touchpoints,
            requires_code_change=requires_code_change,
            requires_tests=requires_tests,
            unknowns=unknowns,
        )

    def select_skeleton(self, diagnosis: TaskDiagnosis) -> WorkflowSkeleton:
        nodes = [
            WorkflowSkeletonNode(
                node_id="diagnose",
                phase="diagnosis",
                role="worker",
                goal="Inspect task context and identify implementation surface.",
                expected_outputs=["analysis_log"],
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
                )
            )
            edges.append(("implement" if diagnosis.requires_code_change else "diagnose", "verify"))
        return WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology="linear",
            nodes=nodes,
            edges=edges,
            rationale="Director v1 uses a conservative linear workflow for Phase B.",
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
                executor_kind="subprocess",
            )
            instantiations.append(
                NodeInstantiation(
                    node=node,
                    skeleton_node_id=skeleton_node.node_id,
                    permission_grounding=grounder.derive(node, skeleton_node.phase),
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
        )

    def _guess_touchpoints(self, objective: str, repo_policy: RepoPolicy) -> list[str]:
        mentioned = re.findall(r"[\w./-]+\\.py|[\w./-]+\\.md|[\w./-]+\\.toml", objective)
        return mentioned or ["."]

    def _command_for(self, phase: str, repo_policy: RepoPolicy) -> list[str]:
        if phase == "verification":
            return repo_policy.test_commands[0]
        return ["python3", "-c", "print('Director Agent v1 node submitted')"]
