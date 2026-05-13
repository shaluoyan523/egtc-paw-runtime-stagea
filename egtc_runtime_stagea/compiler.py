from __future__ import annotations

from pathlib import PurePosixPath

from .experience import ExperienceLibrary
from .models import CompiledGraphPatch, GraphPatch, GraphPatchOperation, NodeCapsule
from .phaseb_models import (
    CompiledWorkflow,
    CompilerFinding,
    PermissionGroundingReport,
    RepoPolicy,
    SandboxProfile,
    WorkflowBlueprint,
)


class PermissionGrounder:
    def __init__(self, repo_policy: RepoPolicy) -> None:
        self.repo_policy = repo_policy

    def derive(self, node: NodeCapsule, phase: str) -> PermissionGroundingReport:
        if phase == "verification":
            allowed_write_paths = []
            allowed_commands = self.repo_policy.test_commands
            justification = "Verification nodes only need read access and repo-grounded test commands."
        elif phase == "implementation":
            allowed_write_paths = self.repo_policy.allowed_write_paths
            allowed_commands = [node.command] if node.command else []
            justification = "Implementation nodes may write only within repo policy write paths."
        else:
            allowed_write_paths = []
            allowed_commands = [node.command] if node.command else []
            justification = "Diagnosis nodes are read-only by default."
        return PermissionGroundingReport(
            node_id=node.node_id,
            sandbox_profile=SandboxProfile(
                network="none",
                allowed_read_paths=self.repo_policy.allowed_read_paths,
                allowed_write_paths=allowed_write_paths,
                allowed_commands=allowed_commands,
                justification=justification,
            ),
            grounded_by=[
                "repo_policy.allowed_read_paths",
                "repo_policy.allowed_write_paths",
                "repo_policy.test_commands",
                "repo_policy.network_allowed_by_default",
            ],
        )


class WorkflowCompiler:
    STAGE_D_GRAPH_PATCH_OPS = {"retry_node"}
    PHASE_E_GRAPH_PATCH_OPS = {
        "retry_node",
        "replace_worker",
        "split_node",
        "insert_node",
        "add_edge",
        "remove_edge",
        "update_join_policy",
    }
    DEFERRED_GRAPH_PATCH_OPS = {"update_schedule"}
    FORBIDDEN_PATCH_VALUE_KEYS = {
        "allowed_write_paths",
        "capability_tokens",
        "network",
        "permissions",
        "sandbox_profile",
        "secret_refs",
    }

    def compile(
        self,
        blueprint: WorkflowBlueprint,
        experience_library: ExperienceLibrary | None = None,
    ) -> CompiledWorkflow:
        findings: list[CompilerFinding] = []
        node_ids = [inst.node.node_id for inst in blueprint.node_instantiations]
        if len(node_ids) != len(set(node_ids)):
            findings.append(
                CompilerFinding("error", "duplicate_node_id", "Node ids must be unique.")
            )

        skeleton_ids = {node.node_id for node in blueprint.workflow_skeleton.nodes}
        instantiated_ids = {inst.skeleton_node_id for inst in blueprint.node_instantiations}
        missing = sorted(skeleton_ids - instantiated_ids)
        for skeleton_node_id in missing:
            findings.append(
                CompilerFinding(
                    "error",
                    "missing_instantiation",
                    "Every skeleton node must have one instantiation.",
                    skeleton_node_id,
                )
            )

        for inst in blueprint.node_instantiations:
            findings.extend(self._check_node(blueprint.repo_policy, inst.node, inst.permission_grounding))

        findings.extend(self._check_experience_usage(blueprint, experience_library))

        accepted = not any(finding.severity == "error" for finding in findings)
        return CompiledWorkflow(
            accepted=accepted,
            blueprint_id=blueprint.blueprint_id,
            executable_nodes=[inst.node for inst in blueprint.node_instantiations] if accepted else [],
            findings=findings,
        )

    def _check_node(
        self,
        repo_policy: RepoPolicy,
        node: NodeCapsule,
        grounding: PermissionGroundingReport,
    ) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        profile = grounding.sandbox_profile
        if profile.network != "none" and not repo_policy.network_allowed_by_default:
            findings.append(
                CompilerFinding(
                    "error",
                    "network_not_grounded",
                    "Network access was requested without repo policy grounding.",
                    node.node_id,
                )
            )
        for path in profile.allowed_write_paths:
            if self._is_sensitive(path, repo_policy.sensitive_paths):
                findings.append(
                    CompilerFinding(
                        "error",
                        "sensitive_write_path",
                        f"Write path overlaps sensitive path: {path}",
                        node.node_id,
                    )
                )
        if node.command and profile.allowed_commands and node.command not in profile.allowed_commands:
            findings.append(
                CompilerFinding(
                    "error",
                    "command_not_allowed",
                    f"Node command is not in allowed_commands: {node.command}",
                    node.node_id,
                )
            )
        if not node.acceptance_criteria:
            findings.append(
                CompilerFinding(
                    "error",
                    "missing_acceptance_criteria",
                    "Node must define Overlooker acceptance criteria.",
                    node.node_id,
                )
            )
        if not grounding.grounded_by:
            findings.append(
                CompilerFinding(
                    "error",
                    "missing_permission_grounding",
                    "PermissionGroundingReport must cite repo policy sources.",
                    node.node_id,
                )
            )
        return findings

    def _check_experience_usage(
        self,
        blueprint: WorkflowBlueprint,
        experience_library: ExperienceLibrary | None,
    ) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        blueprint_pattern_ids = set(blueprint.experience_pattern_ids)
        skeleton_pattern_ids = set(blueprint.workflow_skeleton.experience_pattern_ids)
        if not skeleton_pattern_ids.issubset(blueprint_pattern_ids):
            findings.append(
                CompilerFinding(
                    "error",
                    "experience_skeleton_not_declared",
                    "WorkflowSkeleton references experience patterns not declared by the blueprint.",
                )
            )
        node_pattern_ids: set[str] = set()
        for inst in blueprint.node_instantiations:
            node_pattern_ids.update(inst.node.experience_pattern_ids)
            missing = sorted(set(inst.node.experience_pattern_ids) - blueprint_pattern_ids)
            if missing:
                findings.append(
                    CompilerFinding(
                        "error",
                        "experience_node_not_declared",
                        f"Node references experience patterns not declared by the blueprint: {missing}",
                        inst.node.node_id,
                    )
                )
        if not blueprint_pattern_ids and not node_pattern_ids:
            return findings
        if experience_library is None:
            findings.append(
                CompilerFinding(
                    "warning",
                    "experience_library_not_provided",
                    "Experience pattern references were not checked against an active library.",
                )
            )
            return findings
        active = {pattern.pattern_id: pattern for pattern in experience_library.load_patterns()}
        for pattern_id in sorted(blueprint_pattern_ids | node_pattern_ids):
            pattern = active.get(pattern_id)
            if pattern is None:
                findings.append(
                    CompilerFinding(
                        "error",
                        "unknown_experience_pattern",
                        f"Experience pattern is not active in the library: {pattern_id}",
                    )
                )
                continue
            forbidden = self._forbidden_experience_keys(pattern.recommended_structure)
            if forbidden:
                findings.append(
                    CompilerFinding(
                        "error",
                        "experience_attempts_permission_change",
                        (
                            "Experience patterns may shape workflow structure but cannot "
                            f"request permissions or sandbox changes: {forbidden}"
                        ),
                    )
                )
        return findings

    def _forbidden_experience_keys(self, value: object) -> list[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in self.FORBIDDEN_PATCH_VALUE_KEYS:
                    found.add(key)
                found.update(self._forbidden_experience_keys(nested))
        elif isinstance(value, list):
            for item in value:
                found.update(self._forbidden_experience_keys(item))
        return sorted(found)

    def validate_patch(
        self,
        patch: GraphPatch,
        known_node_ids: set[str],
        graph_id: str | None = None,
        phase: str = "D",
    ) -> CompiledGraphPatch:
        findings: list[dict[str, object]] = []
        if not patch.patch_id:
            findings.append(
                self._patch_finding(
                    "error",
                    "missing_patch_id",
                    "GraphPatch must have a patch_id.",
                )
            )
        if graph_id is not None and patch.graph_id != graph_id:
            findings.append(
                self._patch_finding(
                    "error",
                    "graph_id_mismatch",
                    f"GraphPatch targets {patch.graph_id!r}, expected {graph_id!r}.",
                    patch.triggering_node_id,
                )
            )
        if not patch.director_id:
            findings.append(
                self._patch_finding(
                    "error",
                    "missing_director_id",
                    "GraphPatch must cite a Director actor.",
                )
            )
        if patch.triggering_node_id not in known_node_ids:
            findings.append(
                self._patch_finding(
                    "error",
                    "unknown_triggering_node",
                    f"Triggering node is not in graph: {patch.triggering_node_id}",
                    patch.triggering_node_id,
                )
            )
        if not patch.operations:
            findings.append(
                self._patch_finding(
                    "error",
                    "empty_patch",
                    "GraphPatch must contain at least one operation.",
                )
            )
        for operation in patch.operations:
            findings.extend(self._check_patch_operation(operation, patch, known_node_ids, phase))
        return CompiledGraphPatch(
            accepted=not any(finding["severity"] == "error" for finding in findings),
            patch_id=patch.patch_id,
            graph_id=patch.graph_id,
            operations=patch.operations,
            findings=findings,
        )

    def _check_patch_operation(
        self,
        operation: GraphPatchOperation,
        patch: GraphPatch,
        known_node_ids: set[str],
        phase: str,
    ) -> list[dict[str, object]]:
        findings: list[dict[str, object]] = []
        allowed_ops = self.STAGE_D_GRAPH_PATCH_OPS if phase == "D" else self.PHASE_E_GRAPH_PATCH_OPS
        if operation.op in self.DEFERRED_GRAPH_PATCH_OPS:
            findings.append(
                self._patch_finding(
                    "error",
                    "graph_patch_op_deferred",
                    f"GraphPatch op {operation.op!r} is deferred to a later stage.",
                    operation.node_id,
                )
            )
            return findings
        if operation.op not in allowed_ops:
            findings.append(
                self._patch_finding(
                    "error",
                    "unknown_graph_patch_op",
                    f"Unsupported GraphPatch op: {operation.op}",
                    operation.node_id,
                )
            )
            return findings

        target_node_id = operation.node_id or operation.target_node_id
        if operation.op == "retry_node":
            if target_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_retry_node",
                        f"retry_node target is not in graph: {target_node_id}",
                        target_node_id,
                    )
                )
            if phase == "D" and target_node_id != patch.triggering_node_id:
                findings.append(
                    self._patch_finding(
                        "error",
                        "retry_must_target_triggering_node",
                        "Stage D retry_node patches may only retry the node that triggered the patch.",
                        target_node_id,
                    )
                )
            if not operation.rationale:
                findings.append(
                    self._patch_finding(
                        "warning",
                        "missing_operation_rationale",
                        "GraphPatch operation should include a rationale.",
                        target_node_id,
                    )
                )
        elif operation.op == "insert_node":
            new_node = operation.value.get("node")
            if not isinstance(new_node, dict):
                findings.append(
                    self._patch_finding(
                        "error",
                        "insert_node_missing_node_payload",
                        "insert_node requires value.node payload.",
                        target_node_id,
                    )
                )
            else:
                new_node_id = str(new_node.get("node_id") or "")
                if not new_node_id:
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_missing_node_id",
                            "insert_node value.node must include node_id.",
                            target_node_id,
                        )
                    )
                if new_node_id in known_node_ids:
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_duplicate_node_id",
                            f"insert_node target already exists: {new_node_id}",
                            new_node_id,
                        )
                    )
                if not new_node.get("acceptance_criteria"):
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_missing_acceptance_criteria",
                            "Inserted node must define Overlooker acceptance criteria.",
                            new_node_id or target_node_id,
                        )
                    )
                if not new_node.get("required_evidence"):
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_missing_required_evidence",
                            "Inserted node must define required_evidence.",
                            new_node_id or target_node_id,
                        )
                    )
        elif operation.op in {"add_edge", "remove_edge"}:
            if operation.source_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_edge_source",
                        f"Edge source is not in graph: {operation.source_node_id}",
                        operation.source_node_id,
                    )
                )
            if operation.target_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_edge_target",
                        f"Edge target is not in graph: {operation.target_node_id}",
                        operation.target_node_id,
                    )
                )
        elif operation.op in {"replace_worker", "split_node", "update_join_policy"}:
            if target_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_graph_patch_node",
                        f"GraphPatch node is not in graph: {target_node_id}",
                        target_node_id,
                    )
                )
            if operation.op == "replace_worker" and "executor_kind" not in operation.value and "prompt" not in operation.value:
                findings.append(
                    self._patch_finding(
                        "error",
                        "replace_worker_missing_change",
                        "replace_worker requires a prompt or executor_kind change.",
                        target_node_id,
                    )
                )
            if operation.op == "split_node" and "nodes" not in operation.value:
                findings.append(
                    self._patch_finding(
                        "error",
                        "split_node_missing_nodes",
                        "split_node requires value.nodes.",
                        target_node_id,
                    )
                )
        forbidden = sorted(self.FORBIDDEN_PATCH_VALUE_KEYS.intersection(operation.value))
        if forbidden:
            code = (
                "patch_requires_permission_review"
                if phase != "D"
                else "patch_attempts_permission_change"
            )
            findings.append(
                self._patch_finding(
                    "error",
                    code,
                    f"GraphPatch cannot change permissions or sandbox policy without permission review: {forbidden}",
                    target_node_id,
                )
            )
        return findings

    def _patch_finding(
        self,
        severity: str,
        code: str,
        message: str,
        node_id: str | None = None,
    ) -> dict[str, object]:
        finding: dict[str, object] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if node_id is not None:
            finding["node_id"] = node_id
        return finding

    def _is_sensitive(self, path: str, sensitive_paths: list[str]) -> bool:
        normalized = PurePosixPath(path).as_posix().strip("/")
        if normalized in {"", "."}:
            return False
        return any(
            normalized == sensitive.strip("/")
            or normalized.startswith(f"{sensitive.strip('/')}/")
            for sensitive in sensitive_paths
        )
