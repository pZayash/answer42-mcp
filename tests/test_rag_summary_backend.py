from __future__ import annotations

import json
from pathlib import Path

from mcp_1c.rag import RagService
from mcp_1c.rag.summary_backend import ConfigurationSummaryBackend, metadata_object_row_from_chunk


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _fixture_summary(root: Path) -> Path:
    rag = root / "bp" / "configuration" / "rag"
    chunks = rag / "chunks"
    manifest = {
        "schema": "platform42-config-summary-rag-v1",
        "configCode": "bp",
        "layer": "configuration",
        "identifier": "Accounting30",
        "version": "3.0.204.22",
        "chunkCount": 3,
    }
    (rag).mkdir(parents=True)
    (rag / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (rag / "chunks-manifest.json").write_text(
        json.dumps(
            {
                "totalChunks": 3,
                "files": [
                    {"file": "metadata-object-0001.jsonl", "kind": "metadata-object", "lineCount": 1},
                    {"file": "form-0001.jsonl", "kind": "form", "lineCount": 1},
                    {"file": "bsl-module-0001.jsonl", "kind": "bsl-module", "lineCount": 1},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        chunks / "metadata-object-0001.jsonl",
        [
            {
                "id": "bp:configuration:metadata-object:Справочник:Номенклатура",
                "kind": "metadata-object",
                "metadataType": "Справочник",
                "metadataName": "Номенклатура",
                "title": "Справочник Номенклатура",
                "text": "Справочник Номенклатура. Синоним: Номенклатура. Путь исходника: Catalogs/Номенклатура.xml. Реквизиты: Артикул: xs:string, ВидНоменклатуры: CatalogRef.ВидыНоменклатуры. Общие реквизиты: ОбластьДанныхОсновныеДанные.",
                "sourcePath": "Catalogs/Номенклатура.xml",
            }
        ],
    )
    _write_jsonl(
        chunks / "form-0001.jsonl",
        [
            {
                "id": "bp:configuration:form:Справочник:Номенклатура:ФормаЭлемента",
                "kind": "form",
                "metadataType": "Справочник",
                "metadataName": "Номенклатура",
                "formName": "ФормаЭлемента",
                "title": "Форма ФормаЭлемента — Справочник Номенклатура",
                "text": "Форма ФормаЭлемента объекта Справочник Номенклатура. Реквизиты формы: Объект, Артикул: xs:string. Элементы: Артикул -> Объект.Артикул, ГруппаОсновное.",
                "sourcePath": "Catalogs/Номенклатура/Forms/ФормаЭлемента/Ext/Form.xml",
            }
        ],
    )
    _write_jsonl(
        chunks / "bsl-module-0001.jsonl",
        [
            {
                "id": "bp:configuration:bsl-module:Catalogs/Номенклатура/Ext/ManagerModule.bsl",
                "kind": "bsl-module",
                "title": "BSL-модуль Catalogs/Номенклатура/Ext/ManagerModule.bsl",
                "text": "BSL-модуль номенклатура подбор товаров",
                "sourcePath": "Catalogs/Номенклатура/Ext/ManagerModule.bsl",
            }
        ],
    )
    return root


def test_summary_backend_lookup_object(monkeypatch, tmp_path: Path):
    root = _fixture_summary(tmp_path / "summary")
    monkeypatch.setenv("MCP_1C_RAG_SUMMARY_BACKEND", "local-stream")
    monkeypatch.setenv("MCP_1C_RAG_SUMMARY_LOCAL_ROOT", str(root))
    backend = ConfigurationSummaryBackend()
    found = backend.find_configuration("Accounting30", "3.0.204.22")
    snap = backend.create_snapshot(**found)

    result = backend.lookup_object("Справочник.Номенклатура", snap["name"])

    assert result["backend"] == "configuration-summary"
    assert result["matches"]
    match = result["matches"][0]
    assert match["full_name"] == "Справочник.Номенклатура"
    assert match["navigation"][0]["url"] == "e1cib/list/Справочник.Номенклатура"
    assert {attr["name"] for attr in match["attributes"]} >= {"Артикул", "ВидНоменклатуры"}


def test_rag_service_routes_summary_snapshot(monkeypatch, tmp_path: Path):
    root = _fixture_summary(tmp_path / "summary")
    monkeypatch.setenv("MCP_1C_RAG_SUMMARY_BACKEND", "local-stream")
    monkeypatch.setenv("MCP_1C_RAG_SUMMARY_LOCAL_ROOT", str(root))
    svc = RagService(tmp_path / "rag.sqlite")
    candidate = svc.summary_backend.find_configuration("Бухгалтерия предприятия", "3.0.204.22")
    snap = svc.summary_backend.create_snapshot(**candidate)

    assert svc.snapshot_has_index(snap["name"])
    query = svc.query("подбор товаров", snapshot=snap["name"], limit=5)
    lookup = svc.lookup_object("Справочник.Номенклатура", snapshot=snap["name"])
    forms = svc.lookup_form_element_types(snapshot=snap["name"])

    assert query["backend"] == "configuration-summary"
    assert query["fts"]
    assert lookup["matches"][0]["name"] == "Номенклатура"
    assert any(item["name"] == "Артикул" for item in forms["items"])


def test_metadata_object_chunk_parser():
    row = {
        "metadataType": "Справочник",
        "metadataName": "Товары",
        "text": "Справочник Товары. Синоним: Товары. Реквизиты: Артикул: xs:string. Табличные части: Штрихкоды (колонки: Штрихкод: xs:string, Упаковка).",
        "sourcePath": "Catalogs/Товары.xml",
    }

    parsed = metadata_object_row_from_chunk(row, source_name="summary:bp")

    assert parsed["synonym"] == "Товары"
    assert parsed["attributes"][0]["name"] == "Артикул"
    assert parsed["table_parts"][0]["name"] == "Штрихкоды"
    assert parsed["table_parts"][0]["columns"][0]["name"] == "Штрихкод"
