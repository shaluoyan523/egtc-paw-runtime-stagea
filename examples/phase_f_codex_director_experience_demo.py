from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.director import DirectorAgentV1
from egtc_runtime_stagea.experience import ExperienceLibrary
from egtc_runtime_stagea.phaseb_models import structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


def main() -> int:
    runtime_root = ROOT / "phasef_codex_director_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    workspace = runtime_root / "director"
    library = ExperienceLibrary(runtime_root / "experience")
    library.seed_defaults()
    objective = (
        "实现一个SWE复杂项目修改并进行多agent测试，需要Director根据经验库选择agent数量、"
        "并行explorer、worker、overlooker校验、retry预算和artifact evidence。"
    )
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    director = DirectorAgentV1(experience_library=library)
    blueprint = director.plan_with_codex_director(
        objective,
        repo_policy,
        workspace,
        timeout_sec=300,
    )
    compiled = WorkflowCompiler().compile(blueprint, experience_library=library)
    output = {
        "compiled": structured(compiled),
        "director_mode": blueprint.director_mode,
        "director_session_id": blueprint.director_session_id,
        "topology": blueprint.workflow_skeleton.topology,
        "agent_allocation": blueprint.workflow_skeleton.agent_allocation,
        "experience_pattern_ids": blueprint.experience_pattern_ids,
        "node_count": len(blueprint.workflow_skeleton.nodes),
        "node_roles": {
            node.node_id: node.role for node in blueprint.workflow_skeleton.nodes
        },
        "instantiation_executor_kinds": {
            inst.skeleton_node_id: inst.node.executor_kind
            for inst in blueprint.node_instantiations
        },
        "workspace": str(workspace),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    role_values = set(output["node_roles"].values())
    total_agents = int(output["agent_allocation"].get("total_agents", 0))
    return 0 if (
        compiled.accepted
        and output["director_mode"] == "codex"
        and str(output["director_session_id"] or "").startswith("director-")
        and output["experience_pattern_ids"]
        and output["node_count"] >= 3
        and total_agents == output["node_count"]
        and "explorer" in role_values
        and ("worker" in role_values or "coder" in role_values)
        and any(role in role_values for role in {"verifier", "worker"})
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
