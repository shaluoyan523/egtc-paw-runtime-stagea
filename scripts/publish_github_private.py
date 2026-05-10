from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_EXCLUDES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "runtime_data",
    "swe_smoke_data",
    "swe_codex_smoke_data",
}


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url} failed: {exc.code} {body}") from exc


def iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if any(part in DEFAULT_EXCLUDES for part in rel_parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def create_repo(token: str, repo_name: str, description: str, org: str | None) -> dict[str, object]:
    endpoint = (
        f"https://api.github.com/orgs/{org}/repos"
        if org
        else "https://api.github.com/user/repos"
    )
    return request_json(
        "POST",
        endpoint,
        token,
        {
            "name": repo_name,
            "description": description,
            "private": True,
            "auto_init": False,
        },
    )


def create_blob(token: str, owner: str, repo: str, content: bytes) -> str:
    response = request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/blobs",
        token,
        {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        },
    )
    return str(response["sha"])


def create_tree(
    token: str,
    owner: str,
    repo: str,
    entries: list[dict[str, str]],
) -> str:
    response = request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/trees",
        token,
        {"tree": entries},
    )
    return str(response["sha"])


def create_commit(
    token: str,
    owner: str,
    repo: str,
    message: str,
    tree_sha: str,
) -> str:
    response = request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/commits",
        token,
        {"message": message, "tree": tree_sha},
    )
    return str(response["sha"])


def create_branch_ref(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    commit_sha: str,
) -> None:
    request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        token,
        {"ref": f"refs/heads/{branch}", "sha": commit_sha},
    )


def set_default_branch(token: str, owner: str, repo: str, branch: str) -> None:
    request_json(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}",
        token,
        {"default_branch": branch},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="egtc-paw-runtime-stagea")
    parser.add_argument("--org", default=None)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="Initial Stage A prototype")
    parser.add_argument(
        "--description",
        default="EGTC-PAW Runtime v4 Phase A prototype with Codex worker and Codex Overlooker.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_PAT")
        or os.environ.get("GH_PAT")
    )
    root = Path(__file__).resolve().parents[1]
    files = iter_files(root)
    if args.dry_run:
        print(json.dumps({"root": str(root), "files": [str(p.relative_to(root)) for p in files]}, indent=2))
        return 0
    if not token:
        print("Set GITHUB_TOKEN or GH_TOKEN with repo scope before publishing.", file=sys.stderr)
        return 2

    repo_info = create_repo(token, args.repo, args.description, args.org)
    owner = str(repo_info["owner"]["login"])  # type: ignore[index]
    repo_name = str(repo_info["name"])
    tree_entries: list[dict[str, str]] = []
    for path in files:
        rel_path = path.relative_to(root).as_posix()
        blob_sha = create_blob(token, owner, repo_name, path.read_bytes())
        tree_entries.append(
            {"path": rel_path, "mode": "100644", "type": "blob", "sha": blob_sha}
        )
        print(f"staged {rel_path}")
    tree_sha = create_tree(token, owner, repo_name, tree_entries)
    commit_sha = create_commit(token, owner, repo_name, args.message, tree_sha)
    create_branch_ref(token, owner, repo_name, args.branch, commit_sha)
    set_default_branch(token, owner, repo_name, args.branch)
    print(repo_info.get("html_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
