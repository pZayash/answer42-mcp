from __future__ import annotations

from pathlib import Path
from textwrap import dedent

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


def test_index_help_html_pages(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    processor_dir = root / "DataProcessors" / "PL_ИзменениеВремениБекапирования"
    (processor_dir / "Help").mkdir(parents=True)
    (processor_dir / "PL_ИзменениеВремениБекапирования.mdo").write_text(
        "<MetaDataObject><DataProcessor><name>PL_ИзменениеВремениБекапирования</name></DataProcessor></MetaDataObject>",
        encoding="utf-8",
    )
    (processor_dir / "Help" / "ru.html").write_text(
        dedent(
            """
            <html><head><title>Изменение времени бекапирования</title><style>.x{color:red}</style></head>
            <body><h1>Настройка резервного копирования</h1><p>Позволяет изменить время бекапирования областей данных.</p><script>ignore()</script></body></html>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["help_pages"] == 1

    found = svc.query("время бекапирования", snapshot="SRC-only")
    assert any(row["kind"] == "Справка" and row["object_full_name"] == "Обработка.PL_ИзменениеВремениБекапирования" for row in found["fts"])


def test_index_designer_ext_help_html_pages(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.xml").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    report_dir = root / "Reports" / "ОборотноСальдоваяВедомостьПоСчету"
    (report_dir / "Ext" / "Help").mkdir(parents=True)
    (root / "Reports" / "ОборотноСальдоваяВедомостьПоСчету.xml").write_text(
        "<MetaDataObject><Report><name>ОборотноСальдоваяВедомостьПоСчету</name></Report></MetaDataObject>",
        encoding="utf-8",
    )
    (report_dir / "Ext" / "Help" / "ru.html").write_text(
        dedent(
            """
            <html><body><h1>Отчет &quot;Оборотно-сальдовая ведомость по счету&quot;</h1>
            <p>Отчет предназначен для формирования оборотно-сальдовой ведомости по выбранному счету.</p></body></html>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration", format="designer_xml")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["help_pages"] == 1

    found = svc.query("оборотно сальдовая ведомость по счету справка", snapshot="SRC-only")
    assert any(row["kind"] == "Справка" and row["object_full_name"] == "Отчет.ОборотноСальдоваяВедомостьПоСчету" for row in found["fts"])


def test_split_1c_identifier_variants_are_indexed(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    (root / "Reports").mkdir(parents=True)
    (root / "Reports" / "ОборотноСальдоваяВедомостьПоСчету.xml").write_text(
        "<MetaDataObject><Report><name>ОборотноСальдоваяВедомостьПоСчету</name></Report></MetaDataObject>",
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration", format="designer_xml")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    svc.build_index(snapshot="SRC-only")

    found = svc.query("Оборотно Сальдовая Ведомость По Счету", snapshot="SRC-only")
    assert any(row["object_full_name"] == "Отчет.ОборотноСальдоваяВедомостьПоСчету" for row in found["fts"])
    assert any(row["object_full_name"] == "Отчет.ОборотноСальдоваяВедомостьПоСчету" for row in found["semantic"])


def test_rag_query_returns_semantic_results(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    report_dir = root / "Reports" / "РасчетыСПоставщиками"
    (report_dir / "Help").mkdir(parents=True)
    (report_dir / "РасчетыСПоставщиками.mdo").write_text(
        "<MetaDataObject><Report><name>РасчетыСПоставщиками</name></Report></MetaDataObject>",
        encoding="utf-8",
    )
    (report_dir / "Help" / "ru.html").write_text(
        "<html><body>Задолженность перед поставщиками и подрядчиками.</body></html>",
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["chunks"] >= 2

    found = svc.query("поставщики задолженность", snapshot="SRC-only")
    assert found["semantic"]
    assert any(row["object_full_name"] == "Отчет.РасчетыСПоставщиками" for row in found["semantic"])


def test_index_register_roles_and_dcs_fields(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    reg_dir = root / "InformationRegisters" / "Состояния"
    reg_dir.mkdir(parents=True)
    (reg_dir / "Состояния.mdo").write_text(
        dedent(
            """
            <MetaDataObject>
              <InformationRegister>
                <name>Состояния</name>
                <dimensions><name>Объект</name><type><types>CatalogRef.Партнеры</types></type></dimensions>
                <resources><name>Статус</name><type><types>String</types></type></resources>
                <attributes><name>Комментарий</name><type><types>String</types></type></attributes>
              </InformationRegister>
            </MetaDataObject>
            """
        ).strip(),
        encoding="utf-8",
    )
    report_dir = root / "Reports" / "Контроль"
    (report_dir / "Templates" / "ОсновнаяСхемаКомпоновкиДанных").mkdir(parents=True)
    (report_dir / "Контроль.mdo").write_text(
        "<MetaDataObject><Report><name>Контроль</name></Report></MetaDataObject>",
        encoding="utf-8",
    )
    (report_dir / "Templates" / "ОсновнаяСхемаКомпоновкиДанных" / "Template.dcs").write_text(
        dedent(
            """
            <DataCompositionSchema xmlns:v8="http://v8.1c.ru/8.1/data/core">
              <dataSet>
                <name>НаборДанных1</name>
                <field>
                  <dataPath>Статус</dataPath>
                  <field>Статус</field>
                  <title><v8:item><v8:lang>ru</v8:lang><v8:content>Статус объекта</v8:content></v8:item></title>
                </field>
              </dataSet>
              <calculatedField>
                <dataPath>Длительность</dataPath>
                <expression>РазностьДат(Начало, Конец, "СЕКУНДА")</expression>
              </calculatedField>
            </DataCompositionSchema>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["data_composition_fields"] == 2

    reg = svc.lookup_object("РегистрСведений.Состояния", snapshot="SRC-only")["matches"][0]
    roles = {a["name"]: a["role"] for a in reg["attributes"]}
    assert roles["Объект"] == "dimension"
    assert roles["Статус"] == "resource"
    assert roles["Комментарий"] == "attribute"

    report = svc.lookup_object("Отчет.Контроль", snapshot="SRC-only")["matches"][0]
    fields = {f["data_path"]: f for f in report["data_composition_fields"]}
    assert fields["Статус"]["title"] == "Статус объекта"
    assert fields["Длительность"]["expression"] == 'РазностьДат(Начало, Конец, "СЕКУНДА")'


def test_index_embedded_dynamic_list_fields_from_form(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    cat_dir = root / "Catalogs" / "Пользователи"
    (cat_dir / "Forms" / "ФормаСписка" / "Ext").mkdir(parents=True)
    (cat_dir / "Пользователи.mdo").write_text(
        "<MetaDataObject><Catalog><name>Пользователи</name></Catalog></MetaDataObject>",
        encoding="utf-8",
    )
    (cat_dir / "Forms" / "ФормаСписка" / "Ext" / "Form.xml").write_text(
        dedent(
            """
            <form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" xmlns:schema="http://g5.1c.ru/v8/dt/data-composition-system/schema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
              <attributes xsi:type="form:DynamicListExtInfo">
                <name>Список</name>
                <extInfo>
                  <fields xsi:type="schema:DataCompositionSchemaDataSetField">
                    <dataPath>Ссылка</dataPath>
                    <field>Ссылка</field>
                  </fields>
                  <fields xsi:type="schema:DataCompositionSchemaDataSetField">
                    <dataPath>КонтактнаяИнформация</dataPath>
                    <field>КонтактнаяИнформация</field>
                  </fields>
                </extInfo>
              </attributes>
            </form:Form>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["data_composition_fields"] == 2

    obj = svc.lookup_object("Справочник.Пользователи", snapshot="SRC-only")["matches"][0]
    fields = {f["data_path"]: f for f in obj["data_composition_fields"]}
    assert fields["Ссылка"]["schema_name"] == "ФормаСписка"
    assert fields["КонтактнаяИнформация"]["source_path"].endswith("Form.xml")


def test_lookup_form_element_types(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    cat_dir = root / "Catalogs" / "Пользователи"
    (cat_dir / "Forms" / "ФормаЭлемента" / "Ext").mkdir(parents=True)
    (cat_dir / "Пользователи.mdo").write_text(
        dedent(
            """
            <MetaDataObject><Catalog><name>Пользователи</name>
              <attributes><name>ИНН</name><type><types>String</types></type></attributes>
            </Catalog></MetaDataObject>
            """
        ).strip(),
        encoding="utf-8",
    )
    (cat_dir / "Forms" / "ФормаЭлемента" / "Ext" / "Form.xml").write_text(
        dedent(
            """
            <form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">
              <items xsi:type="form:InputField" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
                <name>ИНН</name><title>ИНН</title><dataPath>ИНН</dataPath>
              </items>
            </form:Form>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    svc.build_index(snapshot="SRC-only")
    result = svc.lookup_form_element_types(snapshot="SRC-only")
    item = next(i for i in result["items"] if i["name"] == "ИНН")
    assert item["attribute_type"] == "String"
    assert item["object_full_name"] == "Справочник.Пользователи"



def test_lookup_form_element_types_for_auto_generated_forms(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    cat_dir = root / "Catalogs" / "Пользователи"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Пользователи.mdo").write_text(
        dedent(
            """
            <MetaDataObject><Catalog><name>Пользователи</name>
              <attributes><name>ИНН</name><synonym><key>ru</key><value>ИНН</value></synonym><type><types>String</types></type></attributes>
            </Catalog></MetaDataObject>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    svc.build_index(snapshot="SRC-only")
    result = svc.lookup_form_element_types(snapshot="SRC-only")
    item = next(i for i in result["items"] if i["name"] == "ИНН" and i["form_name"] == "<auto>")
    assert item["attribute_type"] == "String"
    assert item["element_type"] == "AutoGeneratedField"
    assert item["object_full_name"] == "Справочник.Пользователи"


def test_index_constants_common_forms_and_form_elements(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        "<MetaDataObject><Configuration><name>Test</name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    const_dir = root / "Constants" / "PL_ДатаОкончанияИспользованияРезервногоOpenid"
    const_dir.mkdir(parents=True)
    (const_dir / "PL_ДатаОкончанияИспользованияРезервногоOpenid.mdo").write_text(
        dedent(
            """
            <mdclass:Constant xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
              <name>PL_ДатаОкончанияИспользованияРезервногоOpenid</name>
              <synonym><key>ru</key><value>Дата окончания использования резервного openid</value></synonym>
              <type><types>Date</types></type>
            </mdclass:Constant>
            """
        ).strip(),
        encoding="utf-8",
    )
    form_dir = root / "CommonForms" / "PL_Настройки"
    form_dir.mkdir(parents=True)
    (form_dir / "PL_Настройки.mdo").write_text(
        "<mdclass:CommonForm xmlns:mdclass=\"http://g5.1c.ru/v8/dt/metadata/mdclass\"><name>PL_Настройки</name><synonym><key>ru</key><value>Настройки Platform42</value></synonym></mdclass:CommonForm>",
        encoding="utf-8",
    )
    (form_dir / "Form.form").write_text(
        dedent(
            """
            <form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
              <items xsi:type="form:FormField">
                <name>PL_ДатаОкончанияИспользованияРезервногоOpenid</name>
                <dataPath xsi:type="form:DataPath">
                  <segments>НаборКонстант.PL_ДатаОкончанияИспользованияРезервногоOpenid</segments>
                </dataPath>
                <type>InputField</type>
              </items>
            </form:Form>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["objects"] >= 2
    assert result["forms"] == 1
    assert result["form_elements"] == 1

    constant = svc.lookup_object("Константа.PL_ДатаОкончанияИспользованияРезервногоOpenid", snapshot="SRC-only")
    assert constant["matches"]
    assert constant["matches"][0]["synonym"] == "Дата окончания использования резервного openid"

    found_by_synonym = svc.query("Дата окончания использования резервного openid", snapshot="SRC-only")
    assert any(row["object_full_name"] == "Константа.PL_ДатаОкончанияИспользованияРезервногоOpenid" for row in found_by_synonym["fts"])

    found_form_element = svc.query("НаборКонстант PL_ДатаОкончанияИспользованияРезервногоOpenid", snapshot="SRC-only")
    assert any(row["kind"] == "ЭлементФормы" and row["object_full_name"] == "ОбщаяФорма.PL_Настройки" for row in found_form_element["fts"])



def test_designer_xml_common_form_ext_form_elements(tmp_path: Path):
    root = tmp_path / "src"
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.xml").write_text(
        "<MetaDataObject><Configuration><Name>Test</Name></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    (root / "CommonForms" / "PL_Настройки" / "Ext").mkdir(parents=True)
    (root / "CommonForms" / "PL_Настройки.xml").write_text(
        dedent(
            """
            <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
              <CommonForm>
                <Properties><Name>PL_Настройки</Name><Synonym><v8:item xmlns:v8="http://v8.1c.ru/8.1/data/core"><v8:lang>ru</v8:lang><v8:content>Настройки Platform42</v8:content></v8:item></Synonym></Properties>
              </CommonForm>
            </MetaDataObject>
            """
        ).strip(),
        encoding="utf-8",
    )
    (root / "CommonForms" / "PL_Настройки" / "Ext" / "Form.xml").write_text(
        dedent(
            """
            <Form xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
              <items xsi:type="InputField">
                <name>PL_ДатаОкончанияИспользованияРезервногоOpenid</name>
                <title>Дата окончания использования резервного openid</title>
                <dataPath>НаборКонстант.PL_ДатаОкончанияИспользованияРезервногоOpenid</dataPath>
              </items>
            </Form>
            """
        ).strip(),
        encoding="utf-8",
    )

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC", str(root), kind="configuration", format="designer_xml")
    svc.create_snapshot("SRC-only", sources=["SRC"])
    result = svc.build_index(snapshot="SRC-only")
    assert result["forms"] == 1
    assert result["form_elements"] == 1

    found = svc.query("НаборКонстант PL_ДатаОкончанияИспользованияРезервногоOpenid", snapshot="SRC-only")
    assert any(row["kind"] == "ЭлементФормы" and row["object_full_name"] == "ОбщаяФорма.PL_Настройки" for row in found["fts"])


def test_designer_xml_catalog_children_from_configurator_dump(tmp_path: Path):
    src = Path("src/client_cf").resolve()
    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("CF", str(src), kind="configuration", format="designer_xml")
    svc.create_snapshot("CF-only", sources=["CF"])
    svc.build_index(snapshot="CF-only")
    result = svc.lookup_object("Справочник.ПС_Справочник1", snapshot="CF-only")
    match = result["matches"][0]
    assert {a["name"] for a in match["attributes"]} >= {"Реквизит1", "ВладелецСсылка"}
    assert {tp["name"] for tp in match["table_parts"]} >= {"ДополнительныеСтроки"}


def _write_minimal_edt_source(root: Path, object_name: str, root_tag: str = "Configuration") -> None:
    (root / "Configuration").mkdir(parents=True)
    (root / "Configuration" / "Configuration.mdo").write_text(
        f"<MetaDataObject><{root_tag}><name>{root.name}</name></{root_tag}></MetaDataObject>",
        encoding="utf-8",
    )
    obj_dir = root / "Catalogs" / object_name
    obj_dir.mkdir(parents=True)
    (obj_dir / f"{object_name}.mdo").write_text(
        f"<MetaDataObject><Catalog><name>{object_name}</name><synonym><key>ru</key><value>{object_name}</value></synonym></Catalog></MetaDataObject>",
        encoding="utf-8",
    )


def test_discover_register_and_sync_source_roots(tmp_path: Path):
    root = tmp_path / "sources"
    base = root / "BaseCfg"
    ext = root / "extensions" / "Ext1"
    _write_minimal_edt_source(base, "Партнеры")
    _write_minimal_edt_source(ext, "Дополнение", root_tag="Extension")

    svc = RagService(tmp_path / "rag.sqlite")
    discovered = svc.discover_sources(str(root), recursive=True, max_depth=4)
    assert {Path(item["path"]).name for item in discovered} == {"BaseCfg", "Ext1"}
    assert {item["kind"] for item in discovered} >= {"configuration", "extension"}

    result = svc.add_sources_from_directory(str(root), prefix="T_", watch=True)
    assert result["registered_count"] == 2
    assert svc.list_watch_roots()[0]["path"] == str(root.resolve())
    sources = {src["name"]: src for src in svc.list_sources()}
    assert sources["T_BaseCfg"]["kind"] == "configuration"
    assert sources["T_Ext1"]["kind"] == "extension"

    new_cfg = root / "NewCfg"
    _write_minimal_edt_source(new_cfg, "Новый")
    sync = svc.sync_watch_roots()
    assert sync["roots_count"] == 1
    assert "T_NewCfg" in {src["name"] for src in svc.list_sources()}


def test_query_respects_snapshot_filter_and_reports_index_state(tmp_path: Path):
    src1 = tmp_path / "src1"
    src2 = tmp_path / "src2"
    _write_minimal_edt_source(src1, "Партнеры")
    _write_minimal_edt_source(src2, "Клиенты")

    svc = RagService(tmp_path / "rag.sqlite")
    svc.add_source("SRC1", str(src1), kind="configuration")
    svc.add_source("SRC2", str(src2), kind="configuration")
    svc.create_snapshot("S1", sources=["SRC1"])
    svc.create_snapshot("S2", sources=["SRC2"])
    assert not svc.snapshot_has_index("S1")
    svc.build_index(snapshot="S1")
    assert svc.snapshot_has_index("S1")
    assert svc.query("Партнеры", snapshot="S1")["fts"]
    assert not svc.query("Партнеры", snapshot="S2")["fts"]
