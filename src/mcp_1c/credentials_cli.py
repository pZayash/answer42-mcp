"""Command line management of the Answer42 credential store.

Usage::

    answer42 credentials add --url URL --username USER [--title T] [--alias A ...]
    answer42 credentials list
    answer42 credentials remove --url URL

The password is never taken from the command line (it would end up in shell
history and process lists): it is read from an interactive prompt, or from the
first line of stdin with ``--password-stdin``.  A running Answer42 server picks
up the changed file on its next credential lookup.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .auth import ENV_ACCOUNT_ID, _hash_token
from .credentials import DEFAULT_CREDENTIALS_PATH, CredentialsStore


def _account_id(args: argparse.Namespace) -> str:
    if args.account:
        return args.account
    if args.token:
        # HTTP mode without an explicit account id namespaces credentials by token hash.
        return _hash_token(args.token)
    return os.getenv(ENV_ACCOUNT_ID) or "default"


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
    if not password:
        raise SystemExit("Password is empty")
    return password


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="answer42 credentials",
        description="Manage the Answer42 credential store",
    )
    parser.add_argument(
        "--file",
        default=None,
        help=f"credentials file (default: ONEC_MCP_CREDENTIALS_FILE or {DEFAULT_CREDENTIALS_PATH})",
    )
    account = parser.add_mutually_exclusive_group()
    account.add_argument("--account", default="", help=f"account id (default: {ENV_ACCOUNT_ID} or 'default')")
    account.add_argument("--token", default="", help="HTTP Bearer token; the account id is derived the same way as the server does")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="add or update an entry")
    add.add_argument("--url", required=True, help="infobase URL, file path or wildcard pattern")
    add.add_argument("--username", required=True)
    add.add_argument("--title", default="")
    add.add_argument("--alias", action="append", default=[], help="alternative name; can be repeated")
    add.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")

    commands.add_parser("list", help="list entries without secrets")

    remove = commands.add_parser("remove", help="remove an entry by exact URL")
    remove.add_argument("--url", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = CredentialsStore(Path(args.file) if args.file else None)
    account_id = _account_id(args)

    if args.command == "add":
        password = _read_password(args)
        updated = store.upsert(
            args.url,
            args.username,
            password,
            title=args.title,
            aliases=args.alias,
            account_id=account_id,
        )
        print(f"{'Updated' if updated else 'Added'} credentials for {args.url.rstrip('/')} (account {account_id})")
        return 0
    if args.command == "list":
        print(json.dumps({"account": account_id, "entries": store.list_entries(account_id)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "remove":
        if store.remove(args.url, account_id=account_id):
            print(f"Removed credentials for {args.url.rstrip('/')} (account {account_id})")
            return 0
        print(f"No credentials for {args.url.rstrip('/')} (account {account_id})", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
