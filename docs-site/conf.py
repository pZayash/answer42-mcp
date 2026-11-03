"""Sphinx configuration of the Answer42 documentation site (GitLab Pages).

Guides are read from ``README.md`` and ``docs/*.md`` without copying (see
``source-read`` below); release notes come from ``release-notes/``; the API
section (``api/``) is generated from the MCP tool registry of ``mcp_1c.server``
and the tool groups of ``mcp_1c.tool_groups``.

Build: ``python -m sphinx -W --keep-going -b html docs-site public``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

import tomllib

from mcp_1c import tool_groups

project = "Answer42"
author = "Platform42"
copyright = "2026, Kosolapov Stanislav, 42Clouds and contributors"
# The checkout's version, not the one of an older installed package.
release = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
version = ".".join(release.split(".")[:2])
language = "ru"

REPO_URL = "https://gitlab.com/platform42/answer42-mcp"
REPO_BRANCH = "beta"

extensions = ["myst_parser", "sphinxcontrib.mermaid"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
locale_dirs = ["locale"]  # Russian strings of the Furo theme that Sphinx does not translate

myst_enable_extensions = ["colon_fence", "deflist", "attrs_inline"]
myst_heading_anchors = 3
myst_fence_as_directive = ["mermaid"]
mermaid_d3_zoom = False  # the zoom box has a fixed height and shrinks wide diagrams
suppress_warnings = ["myst.header", "myst.xref_missing"]

html_theme = "furo"
html_title = f"Answer42 {release}"
html_static_path = ["_static", "../docs/assets"]
html_css_files = ["answer42.css"]
html_logo = "../docs/assets/answer42-logo.png"
html_favicon = "../docs/assets/answer42-logo.png"
html_theme_options = {
    "source_repository": REPO_URL,
    "source_branch": REPO_BRANCH,
    "source_directory": "docs-site/",
    "sidebar_hide_name": True,
    "light_css_variables": {"color-brand-primary": "#0e7490", "color-brand-content": "#0e7490"},
    "dark_css_variables": {"color-brand-primary": "#67e8f9", "color-brand-content": "#67e8f9"},
}

# --- Guides: guides/<name>.md is replaced with the repository document ---

GUIDES = {
    "guides/overview": ROOT / "README.md",
    "guides/installation": ROOT / "docs" / "installation.md",
    "guides/agent-installation": ROOT / "docs" / "agent-installation.md",
    "guides/architecture": ROOT / "docs" / "architecture.md",
}
_DOC_LINKS = {
    "README.md": "overview.md",
    "../README.md": "overview.md",
    "docs/installation.md": "installation.md",
    "docs/agent-installation.md": "agent-installation.md",
    "docs/architecture.md": "architecture.md",
}
_LINK = re.compile(r"\]\(([^)#\s]+)([^)\s]*)\)")
_HTML_IMG = re.compile(r'<p align="center">\s*<img src="assets/([^"]+)"[^>]*>\s*</p>\s*', re.DOTALL)
_BLOB = f"{REPO_URL}/-/blob/{REPO_BRANCH}/"


def _fix_link(match: re.Match, base: str) -> str:
    target, anchor = match.group(1), match.group(2)
    if "://" in target or target.startswith(("#", "mailto:")):
        return match.group(0)
    if target in _DOC_LINKS:
        return f"]({_DOC_LINKS[target]}{anchor})"
    if target.startswith("assets/"):
        return f"](../../docs/{target}{anchor})"
    # Anything else in the repository (scripts, sources, release notes) opens on GitLab.
    path = (Path(base) / target).as_posix() if base else target
    return f"]({_BLOB}{path.lstrip('./')}{anchor})"


def _guide_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    base = "" if path.parent == ROOT else path.parent.relative_to(ROOT).as_posix()
    text = _HTML_IMG.sub("", text)
    return _LINK.sub(lambda m: _fix_link(m, base), text)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.stem))


# Headings and boilerplate lines of the older, English release notes.
_RELEASE_NOTE_PHRASES = [
    ("### Changes", "### Изменения"),
    ("### Verification", "### Проверка"),
    ("GitLab CI runs unit tests and package checks on branch and tag pipelines.",
     "GitLab CI запускает модульные тесты и проверки пакета в пайплайнах веток и тегов."),
    ("PyPI publishing and GitLab Release creation are performed by the tag pipeline.",
     "Публикацию в PyPI и создание GitLab Release выполняет пайплайн тега."),
]


def _release_notes() -> str:
    parts = ["# Что нового\n", "Release notes всех версий Answer42, новые сверху.\n"]
    for path in sorted((ROOT / "release-notes").glob("v*.md"), key=_version_key, reverse=True):
        text = path.read_text(encoding="utf-8").lstrip("﻿")
        # One page for all versions: each release is a section, its own sections one level down.
        text = re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", text, flags=re.MULTILINE)
        text = re.sub(r"^## Answer42 ", "## ", text, count=1, flags=re.MULTILINE)
        for english, russian in _RELEASE_NOTE_PHRASES:
            text = text.replace(english, russian)
        parts.append(text.strip() + "\n")
    return "\n".join(parts)


def _source_read(app, docname, source):
    if docname in GUIDES:
        source[0] = _guide_text(GUIDES[docname])
    elif docname == "guides/releases":
        source[0] = _release_notes()


# --- API: one page per tool group, generated from the MCP tool registry ---

GROUP_TITLES = {
    "core": "Основные инструменты",
    "forms": "Формы",
    "tables": "Таблицы",
    "lists": "Динамические списки",
    "commands": "Командный интерфейс",
    "ui_snapshots": "Снимки ui_tree",
    "metadata": "Метаданные",
    "rag": "RAG по исходникам",
    "html": "Живые HTML-страницы",
    "recording": "Доказательная запись",
    "credentials": "Учётные данные",
}
GROUP_DESCRIPTIONS = {
    "core": "сессии, окна, навигация, кнопки, поля, основная работа с таблицами и отчётами; клиент видит её всегда",
    "forms": "углублённая работа с формами: фокус и обход элементов, ожидания, группы и страницы, декорации, HTML-поля через API тест-клиента, диалог выбора файла, пакетный ввод полей",
    "tables": "углублённая работа с таблицами: перемещение курсора, ячейки, выделение, копирование и пометка удаления, режим редактирования, пакеты строк",
    "lists": "настройки динамических списков: отборы, сортировка, поиск, вывод списка в табличный документ",
    "commands": "разделы, команды и поиск командного интерфейса, командные панели и контекстные меню",
    "ui_snapshots": "работа с сохранёнными большими снимками `ui_tree`: поиск, узлы, потомки, сравнение, действия",
    "metadata": "метаданные конфигурации из живого интерфейса: каталог метаданных, версии подсистем и расширений",
    "rag": "RAG по исходникам конфигурации: поиск, источники и снимки; видна только пока RAG активен",
    "html": "живые веб-страницы в HTML-полях через встроенный WebKit клиента: текст, поиск, клик, ввод, ожидание, JavaScript, ошибки консоли",
    "recording": "доказательная запись: скриншоты и слайды каждого шага, PDF-отчёт",
    "credentials": "сохранённые учётные данные информационных баз",
}
GROUP_ORDER = list(GROUP_TITLES)
API_DIR = HERE / "api"
# Tool docstrings stay in English for MCP clients; the site shows these translations.
API_RU = json.loads((HERE / "api_ru.json").read_text(encoding="utf-8"))


def _schema_type(schema: dict) -> str:
    if "anyOf" in schema:
        kinds = [_schema_type(s) for s in schema["anyOf"]]
        return " | ".join(k for k in kinds if k)
    if "enum" in schema:
        return " | ".join(f"`{value}`" for value in schema["enum"])
    kind = schema.get("type", "")
    if kind == "array":
        return f"array of {_schema_type(schema.get('items') or {}) or 'any'}"
    return str(kind or "any")


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _default(schema: dict) -> str:
    if "default" not in schema:
        return "обязательный"
    value = schema["default"]
    if value is None:
        return "`null`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if value == "":
        return '`""`'
    return f"`{value}`"


def _param_text(schema: dict) -> str:
    text = schema.get("description", "")
    return API_RU["params"].get(text, text)


def _summary(tool) -> str:
    """The first sentence of the tool description, for the table at the top of a group page."""
    text = (API_RU["tools"].get(tool.name) or tool.description or "").strip().split("\n\n")[0]
    return re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]


def _tool_markdown(tool) -> str:
    lines = [f"## `{tool.name}`", ""]
    description = API_RU["tools"].get(tool.name) or (tool.description or "").strip()
    if description:
        lines += [description, ""]
    properties = (tool.input_schema or {}).get("properties") or {}
    if properties:
        lines += ["| Параметр | Тип | По умолчанию | Описание |", "|---|---|---|---|"]
        for name, schema in properties.items():
            # session_id has no schema default: stdio falls back to "default", HTTP requires it.
            default = "`default` в stdio" if name == "session_id" else _default(schema)
            lines.append(
                f"| `{name}` | {_cell(_schema_type(schema))} | {default} | {_cell(_param_text(schema))} |"
            )
        lines.append("")
    return "\n".join(lines)


def _generate_api(app) -> None:
    from mcp_1c import server

    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    API_DIR.mkdir(exist_ok=True)
    for old in API_DIR.glob("group-*.md"):
        old.unlink()
    groups = [g for g in GROUP_ORDER if g in tool_groups.GROUPS] + sorted(set(tool_groups.GROUPS) - set(GROUP_ORDER))
    ungrouped = sorted(set(tools) - {t for names in tool_groups.GROUPS.values() for t in names})
    for index, group in enumerate(groups, start=1):
        names = sorted(n for n in tool_groups.GROUPS[group] if n in tools)
        title = GROUP_TITLES.get(group, group)
        page = [
            f"# {title}",
            "",
            f"Группа `{group}` — {GROUP_DESCRIPTIONS.get(group) or tool_groups.DESCRIPTIONS.get(group, '')}. Инструментов: {len(names)}.",
            "",
            "| Инструмент | Назначение |",
            "|---|---|",
            *(f"| [`{n}`](#{n}) | {_cell(_summary(tools[n]))} |" for n in names),
            "",
            *(_tool_markdown(tools[n]) for n in names),
        ]
        (API_DIR / f"group-{index:02d}-{group}.md").write_text("\n".join(page), encoding="utf-8")
    if ungrouped:
        page = ["# Прочие инструменты", "", *(_tool_markdown(tools[n]) for n in ungrouped)]
        (API_DIR / "group-99-other.md").write_text("\n".join(page), encoding="utf-8")


def setup(app):
    app.connect("builder-inited", _generate_api)
    app.connect("source-read", _source_read)
