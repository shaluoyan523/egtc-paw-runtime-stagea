from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime, GraphNodeRecord
from egtc_runtime_stagea.models import CompiledGraphPatch, GraphPatchOperation, NodeCapsule


def node(node_id: str, phase: str = "verification") -> NodeCapsule:
    return NodeCapsule(
        node_id=node_id,
        phase=phase,
        goal=f"Phase E patch apply demo node {node_id}.",
        command=[],
        acceptance_criteria=["Overlooker must cite evidence_ref."],
        required_evidence=["log", "sandbox_events", "resource_report"],
    )


def main() -> int:
    runtime = GraphRuntime(ROOT / "phasee_patch_apply_data")
    spec = GraphRunSpec(
        graph_id="phase-e-patch-apply-demo",
        nodes=[node("implement", "implementation"), node("verify")],
        edges=[("implement", "verify")],
        phase="E",
    )
    records = {
        "implement": GraphNodeRecord(
            node_id="implement",
            status="NODE_REJECTED",
            failure_code="missing_targeted_test",
            attempts=1,
            dependents=["verify"],
            read_only=False,
            write_paths=["."],
        ),
        "verify": GraphNodeRecord(
            node_id="verify",
            status="NODE_PLANNED",
            depends_on=["implement"],
            read_only=True,
        ),
    }
    compiled = CompiledGraphPatch(
        accepted=True,
        patch_id="phase-e-apply",
        graph_id=spec.graph_id,
        operations=[
            GraphPatchOperation(
                op="replace_worker",
                node_id="implement",
                value={"prompt": "Use targeted implementation evidence before retry."},
                rationale="Refine worker instruction after overlooker rejection.",
            ),
            GraphPatchOperation(
                op="insert_node",
                node_id="targeted-tests",
                value={
                    "node": {
                        "node_id": "targeted-tests",
                        "phase": "verification",
                        "goal": "Run targeted evidence checks before final verify.",
                        "acceptance_criteria": ["Overlooker must cite targeted test evidence."],
                        "required_evidence": ["test", "log", "sandbox_events"],
                    }
                },
                rationale="Add missing targeted validation.",
            ),
            GraphPatchOperation(
                op="add_edge",
                source_node_id="implement",
                target_node_id="targeted-tests",
                rationale="Targeted tests consume implementation output.",
            ),
            GraphPatchOperation(
                op="add_edge",
                source_node_id="targeted-tests",
                target_node_id="verify",
                rationale="Final verify consumes targeted test output.",
            ),
        ],
        findings=[],
    )
    applied = runtime._apply_graph_patch(
        "phase-e-patch-apply-demo",
        spec,
        compiled,
        records["implement"],
        records,
    )
    report = {
        "applied": applied,
        "node_ids": [item.node_id for item in spec.nodes],
        "edges": [list(edge) for edge in spec.edges],
        "implement_status": records["implement"].status,
        "implement_prompt": next(item.prompt for item in spec.nodes if item.node_id == "implement"),
        "targeted_depends_on": records["targeted-tests"].depends_on,
        "verify_depends_on": records["verify"].depends_on,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["applied"]
        and "targeted-tests" in report["node_ids"]
        and report["implement_status"] == "NODE_PLANNED"
        and report["implement_prompt"] == "Use targeted implementation evidence before retry."
        and ["implement", "targeted-tests"] in report["edges"]
        and ["targeted-tests", "verify"] in report["edges"]
        and report["targeted_depends_on"] == ["implement"]
        and "targeted-tests" in report["verify_depends_on"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
