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


def _replace_version(pyproject: Path, version: str) -> bool:
    text = pyproject.read_text(encoding="utf-8")
    pattern = r'(?m)^version\s*=\s*"([^"]+)"\s*$'
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f"Could not find project.version in {pyproject}")
    if match.group(1) == version:
        return False
    new_text, count = re.subn(pattern, f'version = "{version}"', text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not replace project.version in {pyproject}")
    pyproject.write_text(new_text, encoding="utf-8")
    return True


def _commits_since_last_tag() -> str:
    previous_tag = _git_output(["describe", "--tags", "--abbrev=0", "--match", "v*"])
    if previous_tag:
        return _git_output(["log", "--pretty=format:- %s (%h)", f"{previous_tag}..HEAD"])
    return _git_output(["log", "--pretty=format:- %s (%h)", "HEAD"])


def _default_release_notes(tag: str) -> str:
    try:
        commits = _commits_since_last_tag()
    except subprocess.CalledProcessError:
        commits = _git_output(["log", "--pretty=format:- %s (%h)", "-n", "20"])
    if not commits.strip():
        commits = "- Maintenance release"
    return (
        f"# Answer42 {tag}\n\n"
        "## Changes\n"
        f"{commits}\n\n"
        "## Verification\n"
        "- GitLab CI runs unit tests and package checks on branch and tag pipelines.\n"
        "- PyPI publishing and GitLab Release creation are performed by the tag pipeline.\n"
    )


def _ensure_release_notes(repo_root: Path, tag: str, *, dry_run: bool, overwrite: bool) -> Path:
    notes_path = repo_root / "release-notes" / f"{tag}.md"
    if notes_path.exists() and notes_path.stat().st_size > 0 and not overwrite:
        print(f"Release notes already exist: {notes_path.relative_to(repo_root)}")
        return notes_path
    text = _default_release_notes(tag)
    print(f"+ write {notes_path.relative_to(repo_root)}")
    if not dry_run:
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(text, encoding="utf-8")
    return notes_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump version, create release notes, commit, tag and push to trigger Answer42 release CI"
    )
    parser.add_argument("version", help="Version without leading v, e.g. 0.2.1")
    parser.add_argument("--remote", default="origin", help="Git remote to push. Default: origin")
    parser.add_argument("--branch", default="HEAD", help="Branch/ref to push. Default: HEAD")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them")
    parser.add_argument("--no-push", action="store_true", help="Create commit/tag locally but do not push")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow unrelated uncommitted changes")
    parser.add_argument("--overwrite-notes", action="store_true", help="Overwrite existing release notes")
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

    print(f"Preparing {tag}")
    changed = True
    if not args.dry_run:
        changed = _replace_version(pyproject, version)
    else:
        print(f"+ update {pyproject.relative_to(repo_root)}: version = {version!r}")
    notes_path = _ensure_release_notes(
        repo_root, tag, dry_run=args.dry_run, overwrite=args.overwrite_notes
    )

    files_to_add = [str(notes_path.relative_to(repo_root))]
    if changed:
        files_to_add.insert(0, str(pyproject.relative_to(repo_root)))
    _run(["git", "add", *files_to_add], dry_run=args.dry_run)
    _run(["git", "commit", "-m", f"Release {tag}"], dry_run=args.dry_run)
    _run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], dry_run=args.dry_run)
    if not args.no_push:
        _run(["git", "push", args.remote, args.branch], dry_run=args.dry_run)
        _run(["git", "push", args.remote, tag], dry_run=args.dry_run)
        print("CI will publish PyPI package and create GitLab Release from the pushed tag.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
