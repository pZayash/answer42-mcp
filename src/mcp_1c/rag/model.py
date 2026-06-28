from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SourceFormat = Literal["edt", "designer_xml", "unknown"]
SourceKind = Literal["base", "extension", "configuration", "standalone"]


@dataclass(slots=True)
class SourceInfo:
    name: str
    path: Path
    kind: SourceKind = "standalone"
    format: SourceFormat = "unknown"
    repo_url: str | None = None
    branch: str | None = None
    commit: str | None = None


@dataclass(slots=True)
class MetadataObject:
    full_name: str
    kind: str
    name: str
    synonym: str | None = None
    source_path: str | None = None
    source_format: SourceFormat = "unknown"
    is_hierarchical: bool | None = None
    owner_types: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Attribute:
    object_full_name: str
    name: str
    type_name: str | None = None
    synonym: str | None = None
    required: bool | None = None
    source_path: str | None = None
    role: str = "attribute"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TablePart:
    object_full_name: str
    name: str
    synonym: str | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TablePartColumn:
    object_full_name: str
    table_part: str
    name: str
    type_name: str | None = None
    synonym: str | None = None
    required: bool | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Form:
    object_full_name: str
    name: str
    kind: str | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FormElement:
    object_full_name: str
    form_name: str
    name: str
    element_type: str | None = None
    title: str | None = None
    data_path: str | None = None
    command_name: str | None = None
    parent_name: str | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DataCompositionField:
    object_full_name: str
    schema_name: str
    data_path: str
    source_field: str | None = None
    title: str | None = None
    expression: str | None = None
    dataset_name: str | None = None
    field_type: str | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HelpPage:
    object_full_name: str
    language: str
    title: str | None = None
    text: str | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedSource:
    source: SourceInfo
    objects: list[MetadataObject] = field(default_factory=list)
    attributes: list[Attribute] = field(default_factory=list)
    table_parts: list[TablePart] = field(default_factory=list)
    table_part_columns: list[TablePartColumn] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    form_elements: list[FormElement] = field(default_factory=list)
    data_composition_fields: list[DataCompositionField] = field(default_factory=list)
    help_pages: list[HelpPage] = field(default_factory=list)
