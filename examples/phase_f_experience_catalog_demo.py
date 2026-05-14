from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.experience import ExperienceLibrary


def main() -> int:
    runtime_root = ROOT / "phasef_experience_catalog_demo_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    library = ExperienceLibrary(runtime_root / "experience")
    seeded = library.seed_defaults()
    catalog = library.export_agent_catalog()
    by_type: dict[str, int] = {}
    for pattern in catalog:
        pattern_type = str(pattern["pattern_type"])
        by_type[pattern_type] = by_type.get(pattern_type, 0) + 1
    directory_root = runtime_root / "directory_catalog"
    from scripts.export_experience_catalog import write_directory_catalog

    write_directory_catalog(
        directory_root,
        {
            "schema": "egtc.experience.catalog.v1",
            "pattern_count": len(catalog),
            "patterns": catalog,
        },
    )
    index = json.loads((directory_root / "index.json").read_text(encoding="utf-8"))
    index_paths = [
        directory_root / str(entry["path"])
        for entry in index["patterns"]
    ]

    required_ids = {
        "seed-aggregation-layered-proposer-aggregator",
        "seed-topology-graph-of-agents-message-passing",
        "seed-dynamic-topology-semantic-routing",
        "seed-routing-learned-mas-gates",
        "seed-memory-hierarchical-task-experience-planner",
        "seed-scaling-large-dynamic-hierarchy",
        "seed-generation-evolutionary-agent-search",
        "seed-generation-self-configuring-rectifying-mas",
        "seed-governance-cross-team-orchestration",
        "seed-governance-company-role-lifecycle",
        "seed-memory-scale-time-before-team-size",
        "seed-security-topology-privacy-leakage-control",
        "seed-review-verification-aware-planning",
        "seed-failure-intervention-debugging-loop",
        "seed-tool-planning-context-efficient-search",
        "seed-routing-disagreement-based-recruitment",
        "seed-governance-protocol-aware-agent-communication",
    }
    ids = {str(pattern["pattern_id"]) for pattern in catalog}
    missing = sorted(required_ids - ids)

    output = {
        "seeded_count": len(seeded),
        "catalog_count": len(catalog),
        "pattern_types": by_type,
        "directory_index_count": index["pattern_count"],
        "directory_missing_files": [
            str(path.relative_to(directory_root))
            for path in index_paths
            if not path.exists()
        ],
        "missing_required_ids": missing,
        "sample_agent_pattern": catalog[0] if catalog else None,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if (
        len(seeded) >= 22
        and not missing
        and output["directory_index_count"] == len(catalog)
        and not output["directory_missing_files"]
        and len(by_type) >= 10
        and all(
            {
                "pattern_id",
                "pattern_type",
                "description",
                "applicability_signals",
                "anti_signals",
                "recommended_structure",
                "required_evidence",
                "risk_notes",
                "source_refs",
                "evidence_level",
                "confidence_score",
                "tags",
            }.issubset(pattern)
            for pattern in catalog
        )
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
