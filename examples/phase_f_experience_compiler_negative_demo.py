from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.director import DirectorAgentV1
from egtc_runtime_stagea.experience import ExperienceLibrary, ExperiencePattern
from egtc_runtime_stagea.phaseb_models import structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


def main() -> int:
    runtime_root = ROOT / "phasef_experience_data" / "negative"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    library = ExperienceLibrary(runtime_root)
    seed = library.seed_defaults()[0]
    library.add_pattern(
        replace(
            seed,
            pattern_id="bad-network-experience",
            description="Invalid pattern that attempts to smuggle network permission.",
            applicability_signals=["实现"],
            recommended_structure={"topology": "unsafe", "network": "full"},
            confidence_score=9.0,
        )
    )
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    blueprint = DirectorAgentV1(experience_library=library).plan(
        "实现复杂功能并测试",
        repo_policy,
    )
    blueprint.experience_pattern_ids.append("bad-network-experience")
    compiled = WorkflowCompiler().compile(blueprint, experience_library=library)
    result = structured(compiled)
    print(json.dumps(result, indent=2, sort_keys=True))
    codes = {finding["code"] for finding in result["findings"]}
    return 0 if (
        not compiled.accepted
        and "experience_attempts_permission_change" in codes
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
