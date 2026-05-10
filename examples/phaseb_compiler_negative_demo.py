from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.director import DirectorAgentV1
from egtc_runtime_stagea.phaseb_models import structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


def main() -> int:
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    blueprint = DirectorAgentV1().plan("设计并落地一个需要校验的 Phase B change", repo_policy)
    target = blueprint.node_instantiations[0].permission_grounding.sandbox_profile
    target.network = "full"
    target.allowed_write_paths = [".git"]
    compiled = WorkflowCompiler().compile(blueprint)
    print(json.dumps(structured(compiled), indent=2, sort_keys=True))
    return 0 if not compiled.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
