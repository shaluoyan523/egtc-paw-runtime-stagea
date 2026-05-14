from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.experience import ExperienceLibrary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT / "phasef_experience_catalog_data" / "experience",
        help="Experience library directory containing JSONL stores.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path. Defaults to stdout.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional directory output. Writes index.json plus one JSON file per pattern.",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include draft, deprecated, and rejected patterns.",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Do not seed default patterns before export.",
    )
    parser.add_argument(
        "--stable",
        action="store_true",
        help="Normalize volatile fields for committed seed-catalog artifacts.",
    )
    args = parser.parse_args()

    library = ExperienceLibrary(args.root)
    if not args.no_seed:
        library.seed_defaults()
    patterns = library.export_agent_catalog(include_inactive=args.include_inactive)
    if args.stable:
        for pattern in patterns:
            pattern["last_updated_at"] = 0.0
    catalog = {
        "schema": "egtc.experience.catalog.v1",
        "pattern_count": len(library.load_patterns(include_inactive=args.include_inactive)),
        "patterns": patterns,
    }
    if args.output_dir:
        write_directory_catalog(args.output_dir, catalog)
        return 0
    payload = json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def write_directory_catalog(output_dir: Path, catalog: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern_dir = output_dir / "patterns"
    pattern_dir.mkdir(parents=True, exist_ok=True)
    patterns = catalog["patterns"]
    if not isinstance(patterns, list):
        raise ValueError("catalog.patterns must be a list")

    entries = []
    pattern_type_counts: dict[str, int] = {}
    for raw_pattern in patterns:
        if not isinstance(raw_pattern, dict):
            continue
        pattern_id = str(raw_pattern["pattern_id"])
        pattern_type = str(raw_pattern["pattern_type"])
        rel_path = Path("patterns") / f"{pattern_id}.json"
        pattern_type_counts[pattern_type] = pattern_type_counts.get(pattern_type, 0) + 1
        (output_dir / rel_path).write_text(
            json.dumps(raw_pattern, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        entries.append(
            {
                "pattern_id": pattern_id,
                "pattern_type": pattern_type,
                "description": raw_pattern.get("description", ""),
                "tags": raw_pattern.get("tags", []),
                "evidence_level": raw_pattern.get("evidence_level", ""),
                "confidence_score": raw_pattern.get("confidence_score", 0),
                "path": str(rel_path),
            }
        )

    index = {
        "schema": str(catalog["schema"]) + ".directory",
        "catalog_schema": catalog["schema"],
        "pattern_count": len(entries),
        "pattern_type_counts": dict(sorted(pattern_type_counts.items())),
        "patterns": sorted(entries, key=lambda item: str(item["pattern_id"])),
    }
    (output_dir / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
