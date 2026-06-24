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
