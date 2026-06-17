#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_1c.rag import RagService


def main() -> None:
    parser = argparse.ArgumentParser(description="Build/query local 1C RAG SQLite index")
    parser.add_argument("--db", default="build/rag/onec-rag.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect")
    p.add_argument("path")

    p = sub.add_parser("add-source")
    p.add_argument("name")
    p.add_argument("path")
    p.add_argument("--kind", default="standalone")
    p.add_argument("--format", default="auto")

    p = sub.add_parser("snapshot")
    p.add_argument("name")
    p.add_argument("--base")
    p.add_argument("--extension", action="append", default=[])
    p.add_argument("--source", action="append")

    p = sub.add_parser("build")
    p.add_argument("--snapshot")
    p.add_argument("--source")

    p = sub.add_parser("lookup")
    p.add_argument("object")
    p.add_argument("--snapshot")

    p = sub.add_parser("query")
    p.add_argument("query")
    p.add_argument("--snapshot")
    p.add_argument("--limit", type=int, default=10)

    sub.add_parser("sources")
    sub.add_parser("snapshots")

    args = parser.parse_args()
    svc = RagService(args.db)
    if args.cmd == "detect":
        out = svc.detect_source(args.path)
    elif args.cmd == "add-source":
        out = svc.add_source(args.name, args.path, args.kind, args.format)
    elif args.cmd == "snapshot":
        out = svc.create_snapshot(args.name, base=args.base, extensions=args.extension, sources=args.source)
    elif args.cmd == "build":
        out = svc.build_index(snapshot=args.snapshot, source=args.source)
    elif args.cmd == "lookup":
        out = svc.lookup_object(args.object, args.snapshot)
    elif args.cmd == "query":
        out = svc.query(args.query, args.snapshot, args.limit)
    elif args.cmd == "sources":
        out = svc.list_sources()
    elif args.cmd == "snapshots":
        out = svc.list_snapshots()
    else:
        raise AssertionError(args.cmd)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
