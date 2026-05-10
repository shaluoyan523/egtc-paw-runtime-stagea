from __future__ import annotations

from pathlib import Path

from .phaseb_models import RepoPolicy


class RepoPolicyInferencer:
    def infer(self, repo_root: Path) -> RepoPolicy:
        repo_root = repo_root.resolve()
        package_managers: list[str] = []
        test_commands: list[list[str]] = []
        allowed_write_paths = ["."]
        notes: list[str] = []

        if (repo_root / "pyproject.toml").exists():
            package_managers.append("python/pyproject")
            test_commands.append(["python3", "-m", "compileall", "egtc_runtime_stagea", "examples"])
        if (repo_root / "package.json").exists():
            package_managers.append("node/package-json")
            test_commands.append(["npm", "test"])
        if (repo_root / "pytest.ini").exists() or (repo_root / "tests").exists():
            test_commands.append(["python3", "-m", "pytest"])
        if not test_commands:
            notes.append("No explicit test framework detected; compiler should require a static smoke check.")
            test_commands.append(["python3", "-m", "compileall", "."])

        sensitive_paths = [
            ".git",
            ".env",
            ".codex",
            "runtime_data",
            "swe_smoke_data",
            "swe_codex_smoke_data",
            "__pycache__",
        ]
        return RepoPolicy(
            repo_root=str(repo_root),
            package_managers=package_managers,
            test_commands=test_commands,
            allowed_read_paths=["."],
            allowed_write_paths=allowed_write_paths,
            sensitive_paths=sensitive_paths,
            network_allowed_by_default=False,
            notes=notes,
        )
