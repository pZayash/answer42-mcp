from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from .detect import detect_source_format
from .model import (
    Attribute,
    DataCompositionField,
    Form,
    FormElement,
    HelpPage,
    MetadataObject,
    ParsedSource,
    SourceInfo,
    TablePart,
    TablePartColumn,
)

KIND_DIRS = {
    "Catalogs": "Справочник",
    "Documents": "Документ",
    "DataProcessors": "Обработка",
    "Reports": "Отчет",
    "Enums": "Перечисление",
    "CommonModules": "ОбщийМодуль",
    "InformationRegisters": "РегистрСведений",
    "AccumulationRegisters": "РегистрНакопления",
    "AccountingRegisters": "РегистрБухгалтерии",
    "CalculationRegisters": "РегистрРасчета",
}


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    value = elem.text.strip()
    return value or None


def _local_string_text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    direct = _text(elem)
    if direct:
        return direct
    ru_value: str | None = None
    any_value: str | None = None
    for item in elem.iter():
        if _strip_ns(item.tag).lower() != "item":
            continue
        lang: str | None = None
        content: str | None = None
        for child in list(item):
            tag = _strip_ns(child.tag).lower()
            if tag in {"lang", "key"}:
                lang = _text(child)
            elif tag in {"content", "value"}:
                content = _text(child)
        if content:
            any_value = content
            if lang == "ru":
                ru_value = content
    if ru_value or any_value:
        return ru_value or any_value
    for child in elem.iter():
        if _strip_ns(child.tag).lower() in {"content", "value"}:
            any_value = _text(child) or any_value
    return any_value


def _first_text(root: ET.Element, names: Iterable[str]) -> str | None:
    wanted = set(names)
    for elem in root.iter():
        if _strip_ns(elem.tag) in wanted:
            return _text(elem)
    return None


def _direct_text(root: ET.Element, names: Iterable[str]) -> str | None:
    wanted = set(names)
    for elem in list(root):
        if _strip_ns(elem.tag) in wanted:
            return _text(elem)
    return None


def _synonym_text(root: ET.Element) -> str | None:
    for elem in root.iter():
        if _strip_ns(elem.tag).lower() == "synonym":
            direct = _text(elem)
            if direct:
                return direct
            ru_value: str | None = None
            any_value: str | None = None
            children = list(elem)
            for idx, child in enumerate(children):
                tag = _strip_ns(child.tag).lower()
                if tag == "value":
                    any_value = _text(child) or any_value
                    if idx > 0 and _strip_ns(children[idx - 1].tag).lower() == "key" and _text(children[idx - 1]) == "ru":
                        ru_value = _text(child)
            return ru_value or any_value
    return None


def _children_named(root: ET.Element, name: str) -> list[ET.Element]:
    return [elem for elem in root.iter() if _strip_ns(elem.tag) == name]


def _metadata_child_name(elem: ET.Element) -> str | None:
    """Name for metadata child in EDT or Configurator XML dump.

    Configurator XML puts child properties under <Properties><Name>, while EDT
    often uses direct <name>.  Avoid recursing into nested column attributes
    when reading a tabular section name.
    """
    direct = _direct_text(elem, ["name", "Name"])
    if direct:
        return direct
    for child in list(elem):
        if _strip_ns(child.tag).lower() == "properties":
            value = _direct_text(child, ["name", "Name"])
            if value:
                return value
    return None


def _xml_root(path: Path) -> ET.Element | None:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError:
        return None


def _type_text(elem: ET.Element) -> str | None:
    values: list[str] = []
    for child in elem.iter():
        tag = _strip_ns(child.tag)
        if tag in {"type", "Type", "types", "Types", "class", "Class"} and child.text:
            values.append(child.text.strip())
    return ", ".join(dict.fromkeys(v for v in values if v)) or None


def _metadata_field_role(tag: str) -> str | None:
    tag = tag.lower()
    if tag in {"attribute", "attributes", "requisite", "requisites"}:
        return "attribute"
    if tag in {"dimension", "dimensions"}:
        return "dimension"
    if tag in {"resource", "resources"}:
        return "resource"
    if tag in {"standardattribute", "standardattributes"}:
        return "standard_attribute"
    return None


def parse_source(source: SourceInfo) -> ParsedSource:
    fmt = source.format if source.format != "unknown" else detect_source_format(source.path)
    source = SourceInfo(
        name=source.name,
        path=source.path,
        kind=source.kind,
        format=fmt,
        repo_url=source.repo_url,
        branch=source.branch,
        commit=source.commit,
    )
    if fmt == "edt":
        return parse_edt_source(source)
    if fmt == "designer_xml":
        return parse_designer_xml_source(source)
    raise ValueError(f"Unsupported or unknown 1C source format for {source.path}: {fmt}")


def parse_edt_source(source: SourceInfo) -> ParsedSource:
    parsed = ParsedSource(source=source)
    for folder, ru_kind in KIND_DIRS.items():
        for mdo in sorted((source.path / folder).glob("*/*.mdo")):
            obj_name = mdo.parent.name
            root = _xml_root(mdo)
            if root is not None:
                obj_name = _first_text(root, ["name", "Name"]) or obj_name
                synonym = _synonym_text(root)
                hierarchical = (_first_text(root, ["hierarchical", "Hierarchical"]) or "").lower() == "true"
                owners = [(_text(x) or "") for x in _children_named(root, "owner") + _children_named(root, "Owner")]
            else:
                synonym = None
                hierarchical = False
                owners = []
            full = f"{ru_kind}.{obj_name}"
            parsed.objects.append(MetadataObject(full, ru_kind, obj_name, synonym, str(mdo), source.format, hierarchical, [o for o in owners if o]))
            if root is not None:
                _extract_metadata_children(parsed, root, full, mdo)
            _extract_forms(parsed, mdo.parent, full)
            _extract_data_composition_schemas(parsed, mdo.parent, full)
            _extract_help_pages(parsed, mdo.parent, full)
    return parsed


def parse_designer_xml_source(source: SourceInfo) -> ParsedSource:
    parsed = ParsedSource(source=source)
    for folder, ru_kind in KIND_DIRS.items():
        base = source.path / folder
        if not base.exists():
            continue
        candidates = list(base.glob("*.xml")) + list(base.glob("*/*.xml"))
        for xml in sorted(candidates):
            if xml.name in {"Form.xml"} or "/Forms/" in str(xml):
                continue
            obj_name = xml.stem if xml.parent == base else xml.parent.name
            root = _xml_root(xml)
            if root is not None:
                obj_name = _first_text(root, ["name", "Name"]) or obj_name
                synonym = _synonym_text(root)
                hierarchical = (_first_text(root, ["hierarchical", "Hierarchical"]) or "").lower() == "true"
                owners = [(_text(x) or "") for x in _children_named(root, "owner") + _children_named(root, "Owner")]
            else:
                synonym = None
                hierarchical = False
                owners = []
            full = f"{ru_kind}.{obj_name}"
            parsed.objects.append(MetadataObject(full, ru_kind, obj_name, synonym, str(xml), source.format, hierarchical, [o for o in owners if o]))
            if root is not None:
                _extract_metadata_children(parsed, root, full, xml)
            object_dir = xml.parent if xml.parent != base else base / xml.stem
            _extract_forms(parsed, object_dir, full)
            _extract_data_composition_schemas(parsed, object_dir, full)
            _extract_help_pages(parsed, object_dir, full)
    return parsed


def _extract_metadata_children(parsed: ParsedSource, root: ET.Element, full: str, source_path: Path) -> None:
    # Heuristic parser covering both EDT and designer XML structures.
    # Keep object attributes and table-part columns separate: XML dumps often
    # nest column attributes under a tabularSection/tablePart node.
    table_part_descendants: set[int] = set()
    for elem in root.iter():
        tag = _strip_ns(elem.tag).lower()
        if tag in {"tabularsection", "tabularsections", "tablepart", "tableparts"}:
            for child in elem.iter():
                table_part_descendants.add(id(child))

    for elem in root.iter():
        tag = _strip_ns(elem.tag).lower()
        role = _metadata_field_role(tag)
        if role is not None and id(elem) not in table_part_descendants:
            name = _metadata_child_name(elem)
            if name:
                parsed.attributes.append(Attribute(full, name, _type_text(elem), _synonym_text(elem), None, str(source_path), role))
        elif tag in {"tabularsection", "tabularsections", "tablepart", "tableparts"}:
            name = _metadata_child_name(elem)
            if name:
                parsed.table_parts.append(TablePart(full, name, _synonym_text(elem), str(source_path)))
                for col in elem.iter():
                    ctag = _strip_ns(col.tag).lower()
                    if _metadata_field_role(ctag) is not None:
                        cname = _metadata_child_name(col)
                        if cname and cname != name:
                            parsed.table_part_columns.append(TablePartColumn(full, name, cname, _type_text(col), _synonym_text(col), None, str(source_path)))


def _extract_forms(parsed: ParsedSource, object_dir: Path, full: str) -> None:
    forms_dir = object_dir / "Forms"
    if not forms_dir.exists():
        return
    for form_dir in sorted([p for p in forms_dir.iterdir() if p.is_dir()]):
        form_name = form_dir.name
        form_xml = form_dir / "Ext" / "Form.xml"
        if not form_xml.exists():
            form_xml = form_dir / "Form.xml"
        parsed.forms.append(Form(full, form_name, None, str(form_xml) if form_xml.exists() else str(form_dir)))
        if form_xml.exists():
            root = _xml_root(form_xml)
            if root is not None:
                _extract_form_elements(parsed, root, full, form_name, form_xml)
                _extract_embedded_data_composition_fields(parsed, root, full, form_name, form_xml)


def _extract_form_elements(parsed: ParsedSource, root: ET.Element, full: str, form_name: str, source_path: Path) -> None:
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        low = tag.lower()
        if low not in {"element", "item", "items", "button", "command", "field", "table", "group"}:
            continue
        name = elem.attrib.get("name") or elem.attrib.get("Name") or _first_text(elem, ["name", "Name"])
        if not name:
            continue
        parsed.form_elements.append(
            FormElement(
                full,
                form_name,
                name,
                tag,
                elem.attrib.get("title") or elem.attrib.get("Title") or _first_text(elem, ["title", "Title", "caption", "Caption"]),
                elem.attrib.get("dataPath") or elem.attrib.get("path") or _first_text(elem, ["dataPath", "path", "DataPath"]),
                elem.attrib.get("commandName") or _first_text(elem, ["commandName", "CommandName"]),
                None,
                str(source_path),
            )
        )


def _extract_embedded_data_composition_fields(parsed: ParsedSource, root: ET.Element, full: str, form_name: str, source_path: Path) -> None:
    dataset_name: str | None = None
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        low = tag.lower()
        if low in {"dataset", "datasetquery"}:
            dataset_name = _metadata_child_name(elem) or dataset_name
        if low not in {"fields", "field", "calculatedfield", "totalfield"}:
            continue
        data_path = _direct_text(elem, ["dataPath", "DataPath"]) or _direct_text(elem, ["name", "Name"])
        field = _direct_text(elem, ["field", "Field"])
        title_elem = next((child for child in list(elem) if _strip_ns(child.tag).lower() in {"title", "caption"}), None)
        title = _local_string_text(title_elem)
        expression = _direct_text(elem, ["expression", "Expression"])
        if not data_path and not field and not title:
            continue
        parsed.data_composition_fields.append(
            DataCompositionField(
                full,
                form_name,
                data_path or field or title or "",
                field,
                title,
                expression,
                dataset_name,
                tag,
                str(source_path),
                {"embedded_in_form": True},
            )
        )


def _extract_help_pages(parsed: ParsedSource, object_dir: Path, full: str) -> None:
    # EDT exports usually keep help as <Object>/Help/*.html, while Designer
    # hierarchical dumps keep it under <Object>/Ext/Help/*.html with
    # <Object>/Ext/Help.xml next to the language-specific pages.
    for help_dir in (object_dir / "Help", object_dir / "Ext" / "Help"):
        if not help_dir.exists():
            continue
        for page in sorted(help_dir.glob("*.html")):
            raw = page.read_text(encoding="utf-8-sig", errors="ignore")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
            h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", raw, flags=re.IGNORECASE | re.DOTALL)
            title = _html_to_text((title_match or h1_match).group(1)) if (title_match or h1_match) else None
            text = _html_to_text(raw)
            if text:
                parsed.help_pages.append(HelpPage(full, page.stem, title, text, str(page), {"file": page.name}))


def _html_to_text(raw: str) -> str:
    without_scripts = re.sub(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    with_breaks = re.sub(r"<\s*/\s*(p|div|br|li|tr|td|th|h[1-6])\s*>", "\n", without_scripts, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", " ", with_breaks)
    decoded = html.unescape(without_tags)
    lines = [re.sub(r"\s+", " ", line).strip() for line in decoded.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_data_composition_schemas(parsed: ParsedSource, object_dir: Path, full: str) -> None:
    templates_dir = object_dir / "Templates"
    if not templates_dir.exists():
        return
    for dcs in sorted(templates_dir.glob("*/Template.dcs")):
        root = _xml_root(dcs)
        if root is None:
            continue
        schema_name = dcs.parent.name
        dataset_name: str | None = None
        for elem in root.iter():
            tag = _strip_ns(elem.tag)
            low = tag.lower()
            if low == "dataset":
                dataset_name = _metadata_child_name(elem) or dataset_name
            if low not in {"field", "calculatedfield", "totalfield"}:
                continue
            data_path = _direct_text(elem, ["dataPath", "DataPath"]) or _direct_text(elem, ["name", "Name"])
            field = _direct_text(elem, ["field", "Field"])
            title_elem = next((child for child in list(elem) if _strip_ns(child.tag).lower() in {"title", "caption"}), None)
            title = _local_string_text(title_elem)
            expression = _direct_text(elem, ["expression", "Expression"])
            if not data_path and not field and not title:
                continue
            parsed.data_composition_fields.append(
                DataCompositionField(
                    full,
                    schema_name,
                    data_path or field or title or "",
                    field,
                    title,
                    expression,
                    dataset_name,
                    tag,
                    str(dcs),
                )
            )


def navigation_for_object(full_name: str, kind: str) -> tuple[str, str] | None:
    if kind in {"Справочник", "Документ", "РегистрСведений", "РегистрНакопления"}:
        return ("list", f"e1cib/list/{full_name}")
    if kind in {"Обработка", "Отчет"}:
        return ("app", f"e1cib/app/{full_name}")
    return None


def split_1c_identifier(text: str | None) -> str:
    """Return a human-readable variant of 1C metadata identifiers.

    1C metadata names are commonly written as CamelCase identifiers, e.g.
    ``ОборотноСальдоваяВедомостьПоСчету``.  FTS tokenizers do not split those
    words automatically, so business queries like ``оборотно сальдовая`` miss
    chunks that contain only the technical identifier.  Keep this helper small
    and deterministic: it does not add domain synonyms, it only exposes words
    already present in the metadata name.
    """
    if not text:
        return ""
    value = re.sub(r"[._/\\:-]+", " ", text)
    value = re.sub(r"(?<=[а-яёa-z0-9])(?=[А-ЯЁA-Z])", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def search_text_variants(*values: str | None) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for part in (value, split_1c_identifier(value)):
            normalized = normalize_query(part)
            if normalized and normalized not in seen:
                seen.add(normalized)
                parts.append(part)
    return " ".join(parts)


def normalize_query(text: str) -> str:
    text = text.replace("ё", "е")
    text = re.sub(r"[\u2010-\u2015–—−-]+", " ", text)
    return re.sub(r"\s+", " ", text.strip().lower())
