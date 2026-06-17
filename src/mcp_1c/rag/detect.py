from __future__ import annotations

from pathlib import Path

from .model import SourceFormat


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
