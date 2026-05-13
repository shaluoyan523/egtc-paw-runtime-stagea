from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.models import GraphPatch, GraphPatchOperation


def patch(operation: GraphPatchOperation) -> GraphPatch:
    return GraphPatch(
        patch_id=f"phase-e-{operation.op}",
        director_id="director-phased",
        graph_id="phase-e-demo",
        triggering_node_id="implement",
        triggering_event="director_replan",
        overlooker_report_ref="artifact://phase-e-demo",
        operations=[operation],
        rationale="Phase E compiler demo.",
    )


def main() -> int:
    compiler = WorkflowCompiler()
    known_nodes = {"diagnose", "implement", "verify"}
    accepted_insert = compiler.validate_patch(
        patch(
            GraphPatchOperation(
                op="insert_node",
                node_id="targeted-tests",
                value={
                    "node": {
                        "node_id": "targeted-tests",
                        "phase": "verification",
                        "acceptance_criteria": ["Overlooker must cite parsed test evidence."],
                        "required_evidence": ["test", "log", "sandbox_events"],
                    }
                },
                rationale="Insert targeted tests before final verification.",
            )
        ),
        known_nodes,
        "phase-e-demo",
        phase="E",
    )
    bad_permission = compiler.validate_patch(
        patch(
            GraphPatchOperation(
                op="replace_worker",
                node_id="implement",
                value={"sandbox_profile": {"network": "on"}},
                rationale="Try to widen sandbox policy.",
            )
        ),
        known_nodes,
        "phase-e-demo",
        phase="E",
    )
    bad_edge = compiler.validate_patch(
        patch(
            GraphPatchOperation(
                op="add_edge",
                source_node_id="missing",
                target_node_id="verify",
                rationale="Try to connect an unknown source.",
            )
        ),
        known_nodes,
        "phase-e-demo",
        phase="E",
    )
    still_deferred = compiler.validate_patch(
        patch(GraphPatchOperation(op="update_schedule", node_id="verify")),
        known_nodes,
        "phase-e-demo",
        phase="E",
    )
    report = {
        "accepted_insert": accepted_insert.accepted,
        "bad_permission": [finding["code"] for finding in bad_permission.findings],
        "bad_edge": [finding["code"] for finding in bad_edge.findings],
        "still_deferred": [finding["code"] for finding in still_deferred.findings],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted_insert"]
        and "patch_requires_permission_review" in report["bad_permission"]
        and "unknown_edge_source" in report["bad_edge"]
        and "graph_patch_op_deferred" in report["still_deferred"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
