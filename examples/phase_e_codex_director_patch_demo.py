from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphNodeRecord, GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule, to_plain_dict


def main() -> int:
    runtime_root = ROOT / "phasee_codex_director_patch_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-e-codex-director-patch-demo",
        nodes=[
            NodeCapsule(
                node_id="implement",
                phase="implementation",
                goal="Rejected node requiring Phase E Director graph patch.",
                command=[],
                acceptance_criteria=["Overlooker must cite evidence_ref."],
                required_evidence=["diff", "test", "log"],
            ),
            NodeCapsule(
                node_id="verify",
                phase="verification",
                goal="Downstream verification.",
                command=[],
                acceptance_criteria=["Overlooker must cite evidence_ref."],
                required_evidence=["test", "log"],
            ),
        ],
        edges=[("implement", "verify")],
        phase="E",
        director_mode="codex",
        replan_budget=1,
    )
    records = {
        "implement": GraphNodeRecord(
            node_id="implement",
            status="NODE_REJECTED",
            attempts=1,
            dependents=["verify"],
            failure_code="missing_validation_route",
            overlooker_recommended_action="request_director_replan",
            overlooker_report_ref="artifact://phase-e-codex-overlooker-report",
        ),
        "verify": GraphNodeRecord(
            node_id="verify",
            status="NODE_PLANNED",
            depends_on=["implement"],
        ),
    }
    patch = runtime._propose_retry_patch(
        "phase-e-codex-director-patch-demo",
        spec,
        records["implement"],
        records,
    )
    compiled = runtime.compiler.validate_patch(
        patch,
        set(records),
        spec.graph_id,
        phase="E",
    )
    applied = runtime._apply_graph_patch(
        "phase-e-codex-director-patch-demo",
        spec,
        compiled,
        records["implement"],
        records,
    ) if compiled.accepted else False
    report = {
        "compiled_accepted": compiled.accepted,
        "applied": applied,
        "director_id": patch.director_id,
        "ops": [operation.op for operation in patch.operations],
        "findings": compiled.findings,
        "node_ids": [node.node_id for node in spec.nodes],
        "edges": [list(edge) for edge in spec.edges],
        "records": {node_id: to_plain_dict(record) for node_id, record in records.items()},
        "director_events": [
            event
            for event in runtime.event_log.list_events("phase-e-codex-director-patch-demo")
            if event["event_type"] == "DirectorGraphPatchSessionCompleted"
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    allowed_ops = {
        "retry_node",
        "replace_worker",
        "split_node",
        "insert_node",
        "add_edge",
        "remove_edge",
        "update_join_policy",
    }
    return 0 if (
        report["compiled_accepted"]
        and report["applied"]
        and report["director_id"] == "director-phased"
        and report["director_events"]
        and report["director_events"][0]["payload"]["director_session_id"].startswith("director-")
        and report["director_events"][0]["payload"]["exit_code"] == 0
        and report["ops"]
        and set(report["ops"]).issubset(allowed_ops)
        and records["implement"].status == "NODE_PLANNED"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
