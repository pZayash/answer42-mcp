from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any

from .model import SourceFormat, SourceKind


def detect_source_format(path: str | Path) -> SourceFormat:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return "unknown"

    # EDT-style project: Configuration/Configuration.mdo and object folders with *.mdo.
    if (root / "Configuration" / "Configuration.mdo").exists():
        return "edt"
    if any(root.glob("Catalogs/*/*.mdo")) or any(root.glob("Documents/*/*.mdo")) or any(root.glob("DataProcessors/*/*.mdo")):
        return "edt"

    # Designer XML dump: Configuration.xml, object XML at root folders, Ext/Form.xml.
    if (root / "Configuration.xml").exists() or any(root.glob("*/**/Ext/Form.xml")) or any(root.glob("Catalogs/*.xml")):
        return "designer_xml"

    return "unknown"


def infer_source_kind(path: str | Path, default: SourceKind = "configuration") -> SourceKind:
    """Best-effort classification of a 1C source tree as base/configuration/extension.

    The parser supports both EDT and Designer XML dumps.  Their on-disk layouts do
    not always encode whether the tree is a base configuration or an extension, so
    this helper keeps the heuristic intentionally conservative: explicit XML root
    tags/names win, then path-name hints, otherwise ``default``.
    """
    root = Path(path)
    lower_parts = [part.casefold() for part in root.parts]
    joined = "/".join(lower_parts)
    for candidate in (root / "Configuration" / "Configuration.mdo", root / "Configuration.xml"):
        if not candidate.exists():
            continue
        try:
            head = candidate.read_text(encoding="utf-8", errors="ignore")[:4096]
        except Exception:
            continue
        if re.search(r"<\s*Extension\b", head, flags=re.IGNORECASE):
            return "extension"
        if re.search(r"<\s*Configuration\b", head, flags=re.IGNORECASE):
            return "configuration"
    if any(marker in joined for marker in ("extension", "extensions", "расшир", "cfe")):
        return "extension"
    if any(marker in joined for marker in ("base", "configuration", "конфигурац", "cf")):
        return "configuration"
    return default


def discover_source_roots(root: str | Path, *, recursive: bool = True, max_depth: int = 4) -> list[dict[str, Any]]:
    """Find 1C configuration/extension source roots under ``root``.

    Returns detected EDT/Designer XML source directories.  ``max_depth`` is
    relative to ``root`` and bounds filesystem walking to avoid accidental full
    disk scans.
    """
    base = Path(root).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        return []
    results: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def add_candidate(path: Path) -> bool:
        fmt = detect_source_format(path)
        if fmt == "unknown" or path in seen:
            return False
        seen.add(path)
        results.append({"path": str(path), "name": path.name, "format": fmt, "kind": infer_source_kind(path)})
        return True

    add_candidate(base)
    if base in seen:
        return sorted(results, key=lambda item: item["path"])
    if not recursive:
        return results

    for current, dirs, _files in os.walk(base):
        cur = Path(current)
        try:
            depth = len(cur.relative_to(base).parts)
        except ValueError:
            continue
        if depth >= max_depth:
            dirs[:] = []
            continue
        # Skip noisy/heavy infrastructure directories.
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__", "build", "dist"}]
        for dirname in list(dirs):
            child = cur / dirname
            if add_candidate(child):
                dirs.remove(dirname)

    return sorted(results, key=lambda item: item["path"])
