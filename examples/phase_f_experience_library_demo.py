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
from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule
from egtc_runtime_stagea.phaseb_models import structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


def sandbox(read_only: bool) -> dict[str, object]:
    return {
        "backend": "codex_native",
        "sandbox_mode": "read_only" if read_only else "workspace_write",
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [] if read_only else ["."],
        "resource_limits": {
            "wall_time_sec": 30,
            "memory_mb": 512,
            "disk_mb": 256,
            "max_processes": 32,
            "max_command_count": 1,
        },
    }


def graph_node(
    node_id: str,
    phase: str,
    role: str,
    read_only: bool,
    pattern_ids: list[str],
) -> NodeCapsule:
    return NodeCapsule(
        node_id=node_id,
        phase=phase,
        goal=f"Phase F experience-guided node {node_id}",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            role,
            node_id,
            "0.0",
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains diff, test, log, sandbox_events, and resource_report.",
            "Experience pattern usage must be observed after node acceptance.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        experience_pattern_ids=pattern_ids,
        sandbox_profile=sandbox(read_only),
    )


def main() -> int:
    runtime_root = ROOT / "phasef_experience_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    library = ExperienceLibrary(runtime_root / "director_experience")
    seeded = library.seed_defaults()
    objective = (
        "实现一个SWE复杂项目修改并进行多agent测试，需要并行explorer、worker、"
        "overlooker校验、retry预算和artifact evidence。"
    )
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    director = DirectorAgentV1(experience_library=library)
    blueprint = director.plan(objective, repo_policy)
    compiled = WorkflowCompiler().compile(blueprint, experience_library=library)

    runtime = GraphRuntime(runtime_root / "runtime")
    pattern_ids = blueprint.experience_pattern_ids[:3]
    spec = GraphRunSpec(
        graph_id="phase-f-experience-demo",
        nodes=[
            graph_node("explore-context", "exploration", "read", True, pattern_ids),
            graph_node("explore-tests", "exploration", "read", True, pattern_ids),
            graph_node("implement-core", "implementation", "write", False, pattern_ids),
            graph_node("verify", "verification", "read", True, pattern_ids),
        ],
        edges=[
            ("explore-context", "implement-core"),
            ("explore-tests", "implement-core"),
            ("implement-core", "verify"),
        ],
        max_parallelism=2,
        max_attempts=1,
        retry_budget=0,
        overlooker_mode="deterministic",
        phase="F",
    )
    result = runtime.run_graph(spec, run_id="phase-f-experience-demo")
    observations = runtime.experience_library.load_observations()
    proposals = runtime.experience_library.load_update_proposals()
    reviewed_pattern = runtime.experience_library.accept_update_proposal(
        proposals[0],
        reviewer="deterministic-overlooker",
    )
    output = {
        "seeded_count": len(seeded),
        "director_topology": blueprint.workflow_skeleton.topology,
        "director_experience_pattern_ids": blueprint.experience_pattern_ids,
        "director_match_count": len(blueprint.task_diagnosis.experience_matches),
        "compiler_accepted": compiled.accepted,
        "compiler_findings": structured(compiled)["findings"],
        "runtime_accepted": result["accepted"],
        "observation_count": len(observations),
        "proposal_count": len(proposals),
        "reviewed_pattern_version": reviewed_pattern.version if reviewed_pattern else None,
        "reviewed_pattern_confidence": reviewed_pattern.confidence_score if reviewed_pattern else None,
        "node_observation_counts": {
            node_id: len(record["experience_observations"])
            for node_id, record in result["nodes"].items()
        },
        "node_proposal_counts": {
            node_id: len(record["experience_update_proposals"])
            for node_id, record in result["nodes"].items()
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if (
        output["seeded_count"] >= 22
        and output["director_topology"] == "parallel_explore_then_single_writer_then_verify"
        and output["director_match_count"] >= 3
        and output["compiler_accepted"]
        and output["runtime_accepted"]
        and output["observation_count"] >= 4
        and output["proposal_count"] >= 4
        and output["reviewed_pattern_version"] == 2
        and all(count >= 1 for count in output["node_observation_counts"].values())
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
