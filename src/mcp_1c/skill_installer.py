"""Backward-compatible handler for removed Answer42 bundled skills."""

from __future__ import annotations

import argparse

MESSAGE = (
    "Answer42 no longer ships or installs bundled skills. Nothing was installed; "
    "use MCP tool descriptions and form_hints instead."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deprecated compatibility command; Answer42 no longer installs bundled skills."
    )
    parser.add_argument(
        "--agent",
        action="append",
        help="Ignored legacy option.",
    )
    parser.add_argument(
        "--target-dir",
        help="Ignored legacy option.",
    )
    parser.add_argument(
        "--skill",
        action="append",
        help="Ignored legacy option.",
    )
    parser.add_argument("--no-overwrite", action="store_true", help="Ignored legacy option.")
    parser.add_argument("--dry-run", action="store_true", help="Ignored legacy option.")
    parser.add_argument("--list-agents", action="store_true", help="Ignored legacy option.")
    parser.parse_args(argv)

    print(MESSAGE)
    return 0
