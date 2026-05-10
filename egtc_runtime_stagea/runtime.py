from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .artifact_store import ArtifactStore
from .codex_wrapper import CodexExecWrapper
from .event_log import EventLog
from .evidence import EvidenceCollector
from .identity import IdentityService
from .models import NodeCapsule, NodeState, to_plain_dict
from .overlooker import CodexOverlooker
from .validators import DeterministicValidator
from .workspace_diff import diff_snapshots, snapshot_workspace


class StageARuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = IdentityService()
        self.runtime_actor = self.identity.actor("runtime-stagea", "runtime")
        self.runtime_token = self.identity.issue_token(
            self.runtime_actor,
            ["artifact:read", "artifact:write"],
        )
        self.artifacts = ArtifactStore(self.root / "artifacts", self.identity)
        self.event_log = EventLog(self.root / "events.sqlite3")
        self.wrapper = CodexExecWrapper(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.collector = EvidenceCollector(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.validator = DeterministicValidator(self.artifacts)
        self.overlooker = CodexOverlooker(
            self.artifacts, self.runtime_actor, self.runtime_token, self.wrapper
        )

    def run_node(self, node: NodeCapsule) -> dict[str, object]:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        workspace = self._prepare_workspace(run_id, node)
        self._record(run_id, node.node_id, "NodeStateChanged", {"state": NodeState.PENDING})

        before = snapshot_workspace(workspace)
        self._record(run_id, node.node_id, "NodeStateChanged", {"state": NodeState.RUNNING})
        worker_result = self.wrapper.run(node, workspace)
        self._record(
            run_id,
            node.node_id,
            "NodeStateChanged",
            {"state": NodeState.WORKER_SUBMITTED, "worker_id": worker_result.worker_id},
        )

        after = snapshot_workspace(workspace)
        workspace_diff = diff_snapshots(before, after)
        evidence = self.collector.collect(
            node,
            worker_result,
            workspace_diff,
            workspace,
        )
        self._record(run_id, node.node_id, "EvidenceCollected", to_plain_dict(evidence))

        validator_reports = self.validator.run(evidence, node)
        self._record(
            run_id,
            node.node_id,
            "ValidatorsCompleted",
            {"reports": to_plain_dict(validator_reports)},
        )

        overlooker_report = self.overlooker.review(
            node,
            evidence,
            validator_reports,
            worker_result,
            workspace_diff,
            self.root / "runs" / run_id / "overlooker",
        )
        final_state = (
            NodeState.NODE_ACCEPTED
            if overlooker_report.verdict == "pass"
            else NodeState.NODE_REJECTED
        )
        self._record(
            run_id,
            node.node_id,
            "OverlookerCompleted",
            to_plain_dict(overlooker_report),
        )
        self._record(run_id, node.node_id, "NodeStateChanged", {"state": final_state})

        return {
            "run_id": run_id,
            "node_id": node.node_id,
            "workspace": str(workspace),
            "worker_result": to_plain_dict(worker_result),
            "evidence": to_plain_dict(evidence),
            "validator_reports": to_plain_dict(validator_reports),
            "overlooker_report": to_plain_dict(overlooker_report),
            "final_state": final_state.value,
            "events": self.event_log.list_events(run_id),
        }

    def _prepare_workspace(self, run_id: str, node: NodeCapsule) -> Path:
        workspace = self.root / "runs" / run_id / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        if node.workspace:
            source = Path(node.workspace)
            if source.exists():
                shutil.copytree(source, workspace, dirs_exist_ok=True)
        return workspace

    def _record(
        self,
        run_id: str,
        node_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self.event_log.append(run_id, node_id, event_type, payload)
