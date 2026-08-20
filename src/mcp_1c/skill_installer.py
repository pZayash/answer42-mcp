"""Install packaged Answer42 skills into popular agent skill directories."""

from __future__ import annotations

import argparse
import importlib.resources
import shutil
from pathlib import Path

PACKAGED_SKILLS = ("answer42", "answer42-rag", "answer42-universal-report")

AGENT_SKILL_DIRS = {
    # OpenClaw workspace skills.
    "openclaw": Path("~/.openclaw/workspace/skills"),
    # Claude Code / Claude Desktop style user skills directory.
    "claude": Path("~/.claude/skills"),
    # Best-effort conventional locations for agents that do not expose a single
    # portable skill standard yet. Users can always pass --target-dir explicitly.
    "codex": Path("~/.codex/skills"),
    "opencode": Path("~/.config/opencode/skills"),
    "pi": Path("~/.pi/skills"),
    "hermes": Path("~/.hermes/skills"),
}


def _copy_tree(src: Path, dst: Path, *, force: bool, dry_run: bool) -> str:
    existed = dst.exists()
    if existed:
        if not force:
            return "skipped-exists"
        if not dry_run:
            shutil.rmtree(dst)
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    return "updated" if existed else "installed"


def install_skills(
    *,
    agents: list[str],
    target_dir: Path | None,
    skills: list[str],
    force: bool,
    dry_run: bool,
) -> list[dict[str, str]]:
    selected_skills = skills or list(PACKAGED_SKILLS)
    unknown_skills = sorted(set(selected_skills) - set(PACKAGED_SKILLS))
    if unknown_skills:
        raise ValueError(f"Unknown packaged skill(s): {', '.join(unknown_skills)}")

    targets: list[tuple[str, Path]] = []
    if target_dir is not None:
        targets.append(("custom", target_dir.expanduser()))
    for agent in agents:
        if agent == "all":
            targets.extend((name, path.expanduser()) for name, path in AGENT_SKILL_DIRS.items())
            continue
        path = AGENT_SKILL_DIRS.get(agent)
        if path is None:
            raise ValueError(
                f"Unknown agent {agent!r}. Known: {', '.join(sorted(AGENT_SKILL_DIRS))}, all"
            )
        targets.append((agent, path.expanduser()))
    if not targets:
        targets.append(("openclaw", AGENT_SKILL_DIRS["openclaw"].expanduser()))

    results: list[dict[str, str]] = []
    root = importlib.resources.files("mcp_1c.assets").joinpath("skills")
    with importlib.resources.as_file(root) as root_path:
        for skill in selected_skills:
            src = root_path / skill
            if not src.exists():
                raise FileNotFoundError(f"Packaged skill not found: {skill}")
            for agent, base_dir in targets:
                dst = base_dir / skill
                status = _copy_tree(src, dst, force=force, dry_run=dry_run)
                results.append(
                    {
                        "agent": agent,
                        "skill": skill,
                        "path": str(dst),
                        "status": "would-" + status if dry_run else status,
                    }
                )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install packaged Answer42 skills")
    parser.add_argument(
        "--agent",
        action="append",
        choices=[*sorted(AGENT_SKILL_DIRS), "all"],
        help="Agent preset to install into. Can be passed multiple times. Default: openclaw.",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        help="Custom skills directory. Use this for agents with non-standard or project-local paths.",
    )
    parser.add_argument(
        "--skill",
        action="append",
        choices=list(PACKAGED_SKILLS),
        help="Packaged skill to install. Can be passed multiple times. Default: all packaged skills.",
    )
    parser.add_argument("--no-overwrite", action="store_true", help="Skip skills that already exist.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without writing files.")
    parser.add_argument("--list-agents", action="store_true", help="Print known agent presets and exit.")
    args = parser.parse_args(argv)

    if args.list_agents:
        for name, path in sorted(AGENT_SKILL_DIRS.items()):
            print(f"{name}: {path.expanduser()}")
        return 0

    results = install_skills(
        agents=args.agent or [],
        target_dir=args.target_dir,
        skills=args.skill or [],
        force=not args.no_overwrite,
        dry_run=args.dry_run,
    )
    for item in results:
        print(f"{item['status']}: {item['agent']}:{item['skill']} -> {item['path']}")
    return 0
