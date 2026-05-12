from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.models import GraphPatch, GraphPatchOperation


def patch(
    graph_id: str,
    operation: GraphPatchOperation,
    triggering_node_id: str = "verify",
) -> GraphPatch:
    return GraphPatch(
        patch_id=f"patch-{operation.op}",
        director_id="director-demo",
        graph_id=graph_id,
        triggering_node_id=triggering_node_id,
        triggering_event="overlooker_rejected_node",
        overlooker_report_ref="artifact://demo",
        operations=[operation],
        rationale="Compiler demo patch.",
    )


def main() -> int:
    compiler = WorkflowCompiler()
    known_nodes = {"build", "verify"}
    accepted = compiler.validate_patch(
        patch(
            "demo-graph",
            GraphPatchOperation(
                op="retry_node",
                node_id="verify",
                value={"failure_code": "worker_exit_2"},
                rationale="Retry the rejected verifier.",
            ),
        ),
        known_nodes,
        "demo-graph",
    )
    deferred = compiler.validate_patch(
        patch("demo-graph", GraphPatchOperation(op="insert_node", node_id="verify")),
        known_nodes,
        "demo-graph",
    )
    permission_change = compiler.validate_patch(
        patch(
            "demo-graph",
            GraphPatchOperation(
                op="retry_node",
                node_id="verify",
                value={"allowed_write_paths": ["."]},
                rationale="Try to widen permissions.",
            ),
        ),
        known_nodes,
        "demo-graph",
    )
    wrong_graph = compiler.validate_patch(
        patch(
            "other-graph",
            GraphPatchOperation(
                op="retry_node",
                node_id="verify",
                rationale="Try to patch a different graph.",
            ),
        ),
        known_nodes,
        "demo-graph",
    )
    report = {
        "accepted_retry_node": accepted.accepted,
        "deferred_insert_node": [finding["code"] for finding in deferred.findings],
        "permission_change": [finding["code"] for finding in permission_change.findings],
        "wrong_graph": [finding["code"] for finding in wrong_graph.findings],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted_retry_node"]
        and "graph_patch_op_deferred" in report["deferred_insert_node"]
        and "patch_attempts_permission_change" in report["permission_change"]
        and "graph_id_mismatch" in report["wrong_graph"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
