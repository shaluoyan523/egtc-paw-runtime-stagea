from __future__ import annotations

import hashlib
import os
from pathlib import Path


def snapshot_workspace(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"}]
        for filename in files:
            path = Path(current_root) / filename
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    before_paths = set(before)
    after_paths = set(after)
    created = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path for path in before_paths & after_paths if before[path] != after[path]
    )
    return {"created": created, "modified": modified, "deleted": deleted}
