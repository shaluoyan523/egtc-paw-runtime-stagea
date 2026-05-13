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
    payload = json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
