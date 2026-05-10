from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request


def token() -> str:
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def request_json(method: str, url: str, payload: dict[str, object] | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token()}")
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


def git_lines(*args: str) -> list[str]:
    return subprocess.check_output(["git", *args], text=True).splitlines()


def git_text(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def create_blob(owner: str, repo: str, content: bytes) -> str:
    response = request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/blobs",
        {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
    )
    return str(response["sha"])


def ref_exists(owner: str, repo: str, branch: str) -> bool:
    try:
        request_json("GET", f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}")
        return True
    except RuntimeError as exc:
        if "failed: 404" in str(exc):
            return False
        raise


def ref_sha(owner: str, repo: str, branch: str) -> str | None:
    try:
        response = request_json("GET", f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}")
    except RuntimeError as exc:
        if "failed: 404" in str(exc):
            return None
        raise
    return str(response["object"]["sha"])


def main() -> int:
    owner_repo = sys.argv[1] if len(sys.argv) > 1 else "shaluoyan523/egtc-paw-runtime-stagea"
    branch = sys.argv[2] if len(sys.argv) > 2 else git_text("branch", "--show-current")
    owner, repo = owner_repo.split("/", 1)

    tree_entries = []
    for line in git_lines("ls-tree", "-r", "--full-tree", "HEAD"):
        meta, path = line.split("\t", 1)
        mode, obj_type, _sha = meta.split()
        if obj_type != "blob":
            continue
        content = subprocess.check_output(["git", "show", f"HEAD:{path}"])
        blob_sha = create_blob(owner, repo, content)
        tree_entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})
        print(f"staged {path}")

    tree_sha = request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/trees",
        {"tree": tree_entries},
    )["sha"]
    message = git_text("log", "-1", "--pretty=%s")
    parent_sha = ref_sha(owner, repo, branch)
    commit_payload = {"message": message, "tree": tree_sha}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    commit_sha = request_json(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/commits",
        commit_payload,
    )["sha"]
    if parent_sha:
        request_json(
            "PATCH",
            f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
            {"sha": commit_sha, "force": False},
        )
    else:
        request_json(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
    print(f"https://github.com/{owner}/{repo}/tree/{branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
