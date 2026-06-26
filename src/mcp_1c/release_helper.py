"""Release helper for Answer42 source checkouts."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9_.+-]*)?$")


def _run(cmd: list[str], *, dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True)


def _git_output(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _replace_version(pyproject: Path, version: str) -> None:
    text = pyproject.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"\s*$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"Could not find exactly one project.version in {pyproject}")
    pyproject.write_text(new_text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump pyproject.toml version, commit, tag and push to trigger PyPI release CI"
    )
    parser.add_argument("version", help="Version without leading v, e.g. 0.2.1")
    parser.add_argument("--remote", default="origin", help="Git remote to push. Default: origin")
    parser.add_argument("--branch", default="HEAD", help="Branch/ref to push. Default: HEAD")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them")
    parser.add_argument("--no-push", action="store_true", help="Create commit/tag locally but do not push")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow unrelated uncommitted changes")
    args = parser.parse_args(argv)

    version = args.version.removeprefix("v")
    if not SEMVER_RE.match(version):
        raise SystemExit(f"Invalid version {args.version!r}; expected e.g. 0.2.1")
    tag = f"v{version}"

    repo_root = Path(_git_output(["rev-parse", "--show-toplevel"]))
    pyproject = repo_root / "pyproject.toml"
    status = _git_output(["status", "--porcelain"])
    if status and not args.allow_dirty:
        raise SystemExit(
            "Working tree is not clean. Commit/stash changes first, or pass --allow-dirty.\n" + status
        )
    existing_tags = _git_output(["tag", "--list", tag])
    if existing_tags:
        raise SystemExit(f"Tag already exists: {tag}")

    print(f"Bumping {pyproject} to {version}")
    if not args.dry_run:
        _replace_version(pyproject, version)
    else:
        print(f"+ update {pyproject}: version = {version!r}")

    _run(["git", "add", str(pyproject.relative_to(repo_root))], dry_run=args.dry_run)
    _run(["git", "commit", "-m", f"Release {tag}"], dry_run=args.dry_run)
    _run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], dry_run=args.dry_run)
    if not args.no_push:
        _run(["git", "push", args.remote, args.branch], dry_run=args.dry_run)
        _run(["git", "push", args.remote, tag], dry_run=args.dry_run)
        print("CI will publish PyPI package and create GitLab Release from the pushed tag.")
    return 0
