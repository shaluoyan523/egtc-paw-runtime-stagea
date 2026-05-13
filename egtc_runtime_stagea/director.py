from __future__ import annotations

import re
import uuid
import json
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore
from .codex_wrapper import CodexExecWrapper
from .experience import ExperienceLibrary, ExperienceMatch
from .identity import IdentityService
from .models import NodeCapsule, to_plain_dict
from .phaseb_models import (
    PermissionGroundingReport,
    NodeInstantiation,
    SandboxProfile,
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

    def plan_with_codex_director(
        self,
        objective: str,
        repo_policy: RepoPolicy,
        workspace: Path,
        *,
        codex_binary: str | None = None,
        timeout_sec: int = 240,
    ) -> WorkflowBlueprint:
        """Launch a real Codex Director session for Stage F experience selection."""

        if self.experience_library is None:
            raise ValueError("plan_with_codex_director requires an ExperienceLibrary")
        workspace.mkdir(parents=True, exist_ok=True)
        seed_matches = self.experience_library.retrieve(objective, limit=16)
        input_packet = {
            "objective": objective,
            "repo_policy": to_plain_dict(repo_policy),
            "experience_candidates": [
                {
                    "pattern_id": match.pattern.pattern_id,
                    "pattern_type": match.pattern.pattern_type,
                    "description": match.pattern.description,
                    "score": match.score,
                    "matched_signals": match.matched_signals,
                    "recommended_structure": match.pattern.recommended_structure,
                    "required_evidence": match.pattern.required_evidence,
                    "risk_notes": match.pattern.risk_notes[:2],
                    "evidence_level": match.pattern.evidence_level,
                    "confidence_score": match.pattern.confidence_score,
                    "source_refs": match.pattern.source_refs[:3],
                }
                for match in seed_matches
            ],
            "director_rules": [
                "Director must choose how many agents/nodes are needed.",
                "Director must compare multiple candidate workflow skeletons before selecting one.",
                "Director must define a scaling policy for tasks that exceed the current corpus.",
                "Director must cite selected experience pattern ids.",
                "Director must not request network or sandbox/permission expansion.",
                "Director must keep verification read-only.",
                "Director structured output is compiled before execution.",
            ],
        }
        (workspace / "director_input.json").write_text(
            json.dumps(input_packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        identity = IdentityService()
        actor = identity.actor("director-phasef", "director")
        token = identity.issue_token(actor, ["artifact:read", "artifact:write"])
        artifacts = ArtifactStore(workspace / "artifacts", identity)
        wrapper = CodexExecWrapper(artifacts, actor, token)
        director_node = NodeCapsule(
            node_id="phasef-director-agent",
            phase="Phase F Director",
            goal="Select and apply experience patterns, including agent allocation.",
            command=[],
            acceptance_criteria=[
                "Director must write strict JSON to director_output.json.",
                "Director chooses topology and number of worker agents.",
                "Director cites experience pattern ids used for the workflow.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="codex_cli",
            prompt=self._phase_f_director_prompt(),
            codex_binary=codex_binary,
            sandbox_profile={
                "backend": "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": timeout_sec,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 64,
                    "max_command_count": 1,
                },
            },
        )
        director_result = wrapper.run(
            director_node,
            workspace,
            role="director",
        )
        if director_result.exit_code != 0:
            raise RuntimeError(
                f"Codex Director session failed with exit_code={director_result.exit_code}"
            )
        output = self._read_director_output(workspace / "director_output.json")
        if not output:
            raise RuntimeError("Codex Director did not create a valid director_output.json")
        blueprint = self._blueprint_from_codex_director_output(
            output,
            objective,
            repo_policy,
            seed_matches,
            director_result.worker_id,
        )
        blueprint.director_mode = "codex"
        blueprint.director_session_id = director_result.worker_id
        return blueprint

    def _phase_f_director_prompt(self) -> str:
        return """
You are the EGTC-PAW Phase F Director Agent.

Read ./director_input.json and create ./director_output.json.

You must choose and apply experience-library patterns yourself. This includes:
- selecting which experience pattern ids to use,
- choosing topology,
- choosing how many worker agents/nodes to instantiate,
- assigning roles to each node,
- assigning experience_pattern_ids to the skeleton and each node.
- comparing multiple candidate workflow skeletons before committing.
- defining how the workflow should scale if the task needs tens or hundreds of agents.

Output strict JSON:
{
  "task_diagnosis": {
    "task_kind": "implementation" | "analysis" | "director_planning",
    "risk_level": "low" | "medium" | "high",
    "requires_code_change": true | false,
    "requires_tests": true | false,
    "repo_touchpoints": ["."],
    "unknowns": [],
    "experience_matches": [
      {
        "pattern_id": "...",
        "pattern_type": "...",
        "score": 0,
        "matched_signals": ["..."],
        "description": "...",
        "evidence_level": "...",
        "confidence_score": 0,
        "source_refs": ["..."]
      }
    ]
  },
  "workflow_skeleton": {
    "topology": "director_selected_topology_name",
    "agent_allocation": {
      "total_agents": 0,
      "roles": {"role_name": 0},
      "allocation_rationale": ["why this number of agents is enough for the current task"],
      "agent_count_confidence": "low" | "medium" | "high"
    },
    "alternative_skeletons": [
      {
        "name": "candidate topology name",
        "estimated_agents": 0,
        "strengths": ["..."],
        "weaknesses": ["..."],
        "selected": false,
        "rejection_reason": "why this was not chosen, or empty when selected"
      }
    ],
    "scaling_policy": {
      "scale_triggers": ["signals that require more agents/nodes"],
      "max_planned_agents_for_current_task": 0,
      "expansion_strategy": ["how to add more explorers/workers/verifiers/overlookers if complexity grows"],
      "requires_replan_when": ["conditions that force Director replan"]
    },
    "deliberation_trace": [
      "compare evidence and task signals, then explain a planning judgment",
      "explain why selected topology is better than alternatives"
    ],
    "experience_pattern_ids": ["..."],
    "experience_rationale": ["..."],
    "nodes": [
      {
        "node_id": "explore-context",
        "phase": "exploration",
        "role": "explorer",
        "goal": "...",
        "depends_on": [],
        "expected_outputs": ["analysis_log"],
        "experience_pattern_ids": ["..."]
      }
    ],
    "edges": [["explore-context", "implement"]]
  },
  "node_instantiations": [
    {
      "skeleton_node_id": "explore-context",
      "node_id": "phasef-explore-context",
      "phase": "exploration",
      "goal": "...",
      "executor_kind": "codex_cli",
      "command": [],
      "prompt": "Worker-specific instruction for this node.",
      "required_evidence": ["diff", "test", "log"],
      "acceptance_criteria": ["Worker may only submit results.", "Overlooker acceptance must cite evidence_ref."],
      "experience_pattern_ids": ["..."],
      "permission_grounding": {
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [],
        "allowed_commands": [["python3", "-c", "print('Director-selected node submitted')"]],
        "grounded_by": ["repo_policy.allowed_read_paths", "repo_policy.allowed_write_paths", "repo_policy.test_commands", "repo_policy.network_allowed_by_default"],
        "justification": "Read-only exploration."
      }
    }
  ]
}

Rules:
- Use only pattern ids present in director_input.experience_candidates.
- Do not assume a fixed number of agents. Derive total_agents from task complexity, uncertainty, dependency breadth, validation surface, risk, and available evidence.
- The current task may need 1 agent, 4 agents, dozens of agents, or a staged plan that can grow toward hundreds. If the full scale is not needed now, explain the scale triggers.
- Compare at least three candidate skeletons, including a small conservative plan, a medium plan, and a larger scalable plan.
- Pick the smallest plan that has enough coverage, but explicitly describe when it should be expanded.
- Node instantiations should normally use executor_kind="codex_cli" because workers are agents.
- Do not request network access.
- Do not write sensitive paths.
- Verification nodes must be read-only.
- No markdown. Do not clone repositories. Do not run tests.
""".strip()

    def _read_director_output(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _blueprint_from_codex_director_output(
        self,
        output: dict[str, Any],
        objective: str,
        repo_policy: RepoPolicy,
        seed_matches: list[ExperienceMatch],
        director_session_id: str,
    ) -> WorkflowBlueprint:
        if not output:
            raise ValueError("Codex Director output is empty")
        raw_diagnosis = output.get("task_diagnosis") if isinstance(output.get("task_diagnosis"), dict) else {}
        raw_skeleton = output.get("workflow_skeleton") if isinstance(output.get("workflow_skeleton"), dict) else {}
        raw_nodes = raw_skeleton.get("nodes") if isinstance(raw_skeleton.get("nodes"), list) else []
        raw_instantiations = output.get("node_instantiations") if isinstance(output.get("node_instantiations"), list) else []
        selected_pattern_ids = self._active_director_pattern_ids(output, seed_matches)
        if not raw_diagnosis:
            raise ValueError("Codex Director output is missing task_diagnosis")
        if not raw_skeleton:
            raise ValueError("Codex Director output is missing workflow_skeleton")
        if not raw_nodes:
            raise ValueError("Codex Director output has no skeleton nodes")
        if not raw_instantiations:
            raise ValueError("Codex Director output has no node instantiations")
        diagnosis = TaskDiagnosis(
            task_id=f"task-{uuid.uuid4().hex[:10]}",
            objective=objective,
            task_kind=str(raw_diagnosis.get("task_kind") or "implementation"),
            risk_level=str(raw_diagnosis.get("risk_level") or "medium"),
            repo_touchpoints=[
                str(path) for path in raw_diagnosis.get("repo_touchpoints", ["."])
            ],
            requires_code_change=bool(raw_diagnosis.get("requires_code_change", True)),
            requires_tests=bool(raw_diagnosis.get("requires_tests", True)),
            unknowns=[str(item) for item in raw_diagnosis.get("unknowns", [])],
            experience_matches=(
                raw_diagnosis.get("experience_matches")
                if isinstance(raw_diagnosis.get("experience_matches"), list)
                else self._serialize_matches(seed_matches)
            ),
        )
        skeleton_nodes: list[WorkflowSkeletonNode] = []
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                continue
            skeleton_nodes.append(
                WorkflowSkeletonNode(
                    node_id=str(raw.get("node_id") or f"node-{len(skeleton_nodes)+1}"),
                    phase=str(raw.get("phase") or "analysis"),
                    role=str(raw.get("role") or "worker"),
                    goal=str(raw.get("goal") or "Director-selected node."),
                    depends_on=[str(item) for item in raw.get("depends_on", [])],
                    expected_outputs=[str(item) for item in raw.get("expected_outputs", [])],
                    experience_pattern_ids=self._filter_known_patterns(
                        raw.get("experience_pattern_ids", selected_pattern_ids),
                        selected_pattern_ids,
                    ),
                )
            )
        if not skeleton_nodes:
            raise ValueError("Codex Director output yielded no valid skeleton nodes")
        edges = []
        for edge in raw_skeleton.get("edges", []):
            if isinstance(edge, list | tuple) and len(edge) == 2:
                edges.append((str(edge[0]), str(edge[1])))
        skeleton = WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology=str(raw_skeleton.get("topology") or "director_selected"),
            nodes=skeleton_nodes,
            edges=edges,
            rationale="Codex Director selected topology and agent allocation from experience candidates.",
            agent_allocation=(
                raw_skeleton.get("agent_allocation")
                if isinstance(raw_skeleton.get("agent_allocation"), dict)
                else {"total_agents": len(skeleton_nodes)}
            ),
            alternative_skeletons=(
                raw_skeleton.get("alternative_skeletons")
                if isinstance(raw_skeleton.get("alternative_skeletons"), list)
                else []
            ),
            scaling_policy=(
                raw_skeleton.get("scaling_policy")
                if isinstance(raw_skeleton.get("scaling_policy"), dict)
                else {}
            ),
            deliberation_trace=[
                str(item) for item in raw_skeleton.get("deliberation_trace", [])
            ],
            experience_pattern_ids=self._filter_known_patterns(
                raw_skeleton.get("experience_pattern_ids", selected_pattern_ids),
                selected_pattern_ids,
            ),
            experience_rationale=[
                str(item) for item in raw_skeleton.get("experience_rationale", [])
            ],
        )
        instantiations: list[NodeInstantiation] = []
        for raw in raw_instantiations:
            if not isinstance(raw, dict):
                continue
            skeleton_node_id = str(raw.get("skeleton_node_id") or "")
            if not skeleton_node_id:
                continue
            command = raw.get("command")
            command_list = [str(item) for item in command] if isinstance(command, list) else self._command_for(str(raw.get("phase") or ""), repo_policy)
            pattern_ids = self._filter_known_patterns(
                raw.get("experience_pattern_ids", skeleton.experience_pattern_ids),
                selected_pattern_ids,
            )
            node = NodeCapsule(
                node_id=str(raw.get("node_id") or f"{diagnosis.task_id}-{skeleton_node_id}"),
                phase=str(raw.get("phase") or "analysis"),
                goal=str(raw.get("goal") or "Director-selected node."),
                command=command_list,
                acceptance_criteria=[
                    str(item) for item in raw.get("acceptance_criteria", [])
                ] or [
                    "Worker may only submit results.",
                    "Overlooker acceptance must cite evidence_ref.",
                ],
                required_evidence=[
                    str(item) for item in raw.get("required_evidence", ["diff", "test", "log"])
                ],
                experience_pattern_ids=pattern_ids,
                executor_kind=str(raw.get("executor_kind") or "subprocess"),
                prompt=str(raw.get("prompt") or raw.get("goal") or "Submit evidence for this Director-selected node."),
            )
            grounding = self._grounding_from_director(raw, node, repo_policy)
            node.sandbox_profile = grounding.sandbox_profile.__dict__
            instantiations.append(
                NodeInstantiation(
                    node=node,
                    skeleton_node_id=skeleton_node_id,
                    permission_grounding=grounding,
                )
            )
        if not instantiations:
            raise ValueError("Codex Director output yielded no valid node instantiations")
        return WorkflowBlueprint(
            blueprint_id=f"blueprint-{uuid.uuid4().hex[:10]}",
            director_id=self.director_id,
            task_diagnosis=diagnosis,
            repo_policy=repo_policy,
            workflow_skeleton=skeleton,
            node_instantiations=instantiations,
            experience_pattern_ids=skeleton.experience_pattern_ids,
            director_mode="codex",
            director_session_id=director_session_id,
        )

    def _grounding_from_director(
        self,
        raw: dict[str, Any],
        node: NodeCapsule,
        repo_policy: RepoPolicy,
    ) -> PermissionGroundingReport:
        raw_grounding = raw.get("permission_grounding")
        if not isinstance(raw_grounding, dict):
            return PermissionGrounder(repo_policy).derive(node, node.phase)
        allowed_commands = raw_grounding.get("allowed_commands")
        return PermissionGroundingReport(
            node_id=node.node_id,
            sandbox_profile=SandboxProfile(
                network=str(raw_grounding.get("network") or "none"),
                allowed_read_paths=[
                    str(item) for item in raw_grounding.get("allowed_read_paths", ["."])
                ],
                allowed_write_paths=[
                    str(item) for item in raw_grounding.get("allowed_write_paths", [])
                ],
                allowed_commands=(
                    [
                        [str(part) for part in command]
                        for command in allowed_commands
                        if isinstance(command, list)
                    ]
                    if isinstance(allowed_commands, list)
                    else []
                ),
                justification=str(raw_grounding.get("justification") or "Director-provided grounding."),
            ),
            grounded_by=[
                str(item) for item in raw_grounding.get("grounded_by", [])
            ],
        )

    def _active_director_pattern_ids(
        self,
        output: dict[str, Any],
        seed_matches: list[ExperienceMatch],
    ) -> list[str]:
        known = {match.pattern.pattern_id for match in seed_matches}
        selected: list[str] = []
        skeleton = output.get("workflow_skeleton")
        raw_ids = skeleton.get("experience_pattern_ids", []) if isinstance(skeleton, dict) else []
        for item in raw_ids:
            pattern_id = str(item)
            if pattern_id in known and pattern_id not in selected:
                selected.append(pattern_id)
        if selected:
            return selected
        return [match.pattern.pattern_id for match in seed_matches]

    def _filter_known_patterns(self, raw: Any, known_ids: list[str]) -> list[str]:
        values = raw if isinstance(raw, list) else known_ids
        selected: list[str] = []
        known = set(known_ids)
        for item in values:
            pattern_id = str(item)
            if pattern_id in known and pattern_id not in selected:
                selected.append(pattern_id)
        return selected

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
