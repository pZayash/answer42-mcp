"""MCP server for 1C:Enterprise test manager automation."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

__all__ = ["__version__"]


def _version_from_pyproject() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version") or "0+unknown")
    except Exception:
        return "0+unknown"


try:
    __version__ = version("answer42")
except PackageNotFoundError:
    __version__ = _version_from_pyproject()
