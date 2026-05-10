from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request_json(method: str, url: str, token: str, payload: dict[str, object] | None = None):
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
        raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if value:
        return value
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def remote_file(token_value: str, owner: str, repo: str, rel_path: str, branch: str):
    encoded = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded}?ref={branch}"
    try:
        return request_json("GET", url, token_value)
    except RuntimeError as exc:
        if "failed: 404" in str(exc):
            return None
        raise


def put_file(
    token_value: str,
    owner: str,
    repo: str,
    rel_path: str,
    content: bytes,
    branch: str,
    message: str,
) -> None:
    encoded = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
    existing = remote_file(token_value, owner, repo, rel_path, branch)
    payload: dict[str, object] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if existing and isinstance(existing, dict) and existing.get("sha"):
        payload["sha"] = existing["sha"]
    request_json(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded}",
        token_value,
        payload,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="shaluoyan523/egtc-paw-runtime-stagea")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="Add Phase B Director Agent v1 design")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    owner, repo = args.repo.split("/", 1)
    root = Path.cwd()
    token_value = token()
    for raw_path in args.paths:
        path = Path(raw_path)
        rel_path = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        put_file(
            token_value,
            owner,
            repo,
            rel_path,
            (root / rel_path).read_bytes(),
            args.branch,
            args.message,
        )
        print(f"synced {rel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
