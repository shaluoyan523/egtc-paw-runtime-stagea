from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--objective",
        default="开始设计Phase B，正式设计中控Agent v1：TaskDiagnosis、WorkflowSkeleton、NodeInstantiation、PermissionGrounding、structured output、compiler 校验。中控Agent起名叫做Director Agent",
    )
    parser.add_argument("--repo-root", default=str(ROOT))
    args = parser.parse_args()

    repo_policy = RepoPolicyInferencer().infer(Path(args.repo_root))
    blueprint = DirectorAgentV1().plan(args.objective, repo_policy)
    compiled = WorkflowCompiler().compile(blueprint)
    result = {
        "director_structured_output": structured(blueprint),
        "compiler_result": structured(compiled),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if compiled.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
