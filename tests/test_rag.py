from __future__ import annotations

from pathlib import Path

from mcp_1c.rag import RagService
from mcp_1c.rag.detect import detect_source_format


def test_detect_formats():
    assert detect_source_format("tests/fixtures/rag_edt") == "edt"
    assert detect_source_format("tests/fixtures/rag_designer/src/cf") == "designer_xml"


def test_index_edt_and_query(tmp_path: Path):
    svc = RagService(tmp_path / "rag.sqlite")
    src = svc.add_source("EDT", "tests/fixtures/rag_edt", kind="base")
    assert src["detected_format"] == "edt"
    snap = svc.create_snapshot("EDT-only", base="EDT")
    assert snap["layers"][0]["format"] == "edt"
    result = svc.build_index(snapshot="EDT-only")
    assert result["objects"] >= 1
    obj = svc.lookup_object("Справочник.Партнеры", snapshot="EDT-only")
    assert obj["matches"]
    match = obj["matches"][0]
    assert match["navigation"][0]["url"] == "e1cib/list/Справочник.Партнеры"
    assert any(a["name"] == "ИНН" for a in match["attributes"])
    assert not any(a["name"] == "Представление" for a in match["attributes"])
    assert match["table_parts"][0]["columns"][0]["name"] == "Представление"
    found = svc.query("Партнеры", snapshot="EDT-only")
    assert found["terms"] or found["fts"]


def test_index_designer_xml(tmp_path: Path):
    svc = RagService(tmp_path / "rag.sqlite")
    src = svc.add_source("MCP", "tests/fixtures/rag_designer/src/cf", kind="configuration")
    assert src["detected_format"] == "designer_xml"
    svc.create_snapshot("MCP-only", sources=["MCP"])
    result = svc.build_index(snapshot="MCP-only")
    assert result["objects"] >= 1
    obj = svc.lookup_object("Обработка.MCPTestManager", snapshot="MCP-only")
    assert obj["matches"]
    assert obj["matches"][0]["navigation"][0]["url"] == "e1cib/app/Обработка.MCPTestManager"
