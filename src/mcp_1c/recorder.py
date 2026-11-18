"""Evidence recording: screenshots of the 1C window with a readable account of each step, printed to a PDF.

The report is built as an HTML document and printed by Chrome/Edge (headless).  Its structure:

* a cover with the title, the date, the duration and the result;
* a table of the steps;
* one page per step: what was done, the screenshot, what came back;
* an appendix with the technical details (the MCP call and its response as JSON) at the very end, so the
  pages that people read stay free of them.
"""

from __future__ import annotations

import base64
import html
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_TITLE = "Протокол проверки"


@dataclass
class RecordingSession:
    active: bool = False
    output_dir: Path = Path("build/recordings/session")
    display: str = ""
    window: bool = False
    frames_dir: Path = Path("build/recordings/session/frames")
    title: str = DEFAULT_TITLE
    meta: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def start(
        self, output_dir: str, display: str = "", window: bool = False, title: str = "", meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.active = True
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.display = display
        self.window = window
        self.title = title.strip() or DEFAULT_TITLE
        self.meta = {str(k): str(v) for k, v in (meta or {}).items() if str(v).strip()}
        self.started_at = time.time()
        self.frames_dir = self.output_dir / "frames"
        self.events = []
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        # Remove stale frames from previous runs. The same output_dir is reused by default, and old files
        # would otherwise be picked up, producing a mixed-up PDF.
        for stale in self.frames_dir.glob("*.png"):
            try:
                stale.unlink()
            except OSError:
                pass
        # Slides, MP4 and GIF are not produced any more: remove what older runs left in the directory.
        for obsolete in ("recording.mp4", "recording.gif", "recording.html"):
            try:
                (self.output_dir / obsolete).unlink()
            except OSError:
                pass
        slides_dir = self.output_dir / "slides"
        if slides_dir.is_dir():
            for stale in slides_dir.glob("*.svg"):
                try:
                    stale.unlink()
                except OSError:
                    pass
        return {"active": True, "output_dir": str(self.output_dir)}

    def stop(self) -> dict[str, Any]:
        self.active = False
        manifest = self.output_dir / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"title": self.title, "meta": self.meta, "events": self.events}, ensure_ascii=False, indent=2), encoding="utf-8")
        media = self.build_media()
        return {"active": False, "output_dir": str(self.output_dir), "events": len(self.events), **media}

    def capture(self, screenshot_func, action: str, params: dict[str, Any], result: Any, *, screenshot: bool = True) -> dict[str, Any] | None:
        if not self.active:
            return None
        index = len(self.events) + 1
        safe_params = redact(params)
        safe_result = redact(result)
        frame = self.frames_dir / f"{index:03d}_{safe_name(action)}.png"
        event: dict[str, Any] = {
            "index": index,
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "params": safe_params,
            "raw_result": safe_result,
            "result": summarize(safe_result),
            "failed": result_failed(safe_result),
        }
        if screenshot:
            try:
                screenshot_func(str(frame), self.window, self.display)
                if frame.exists():
                    event["frame"] = str(frame)
            except Exception as exc:
                event["screenshot_error"] = str(exc)
        else:
            event["text_only"] = True
        self.events.append(event)
        return {key: value for key, value in event.items() if key != "raw_result"}

    def build_media(self) -> dict[str, Any]:
        if not self.events:
            return {"pdf": ""}
        pdf = self.output_dir / "recording.pdf"
        html_report = self.output_dir / "recording.html"
        html_report.write_text(make_report_html(self.title, self.meta, self.events, self.started_at), encoding="utf-8")
        browser = first_existing(["google-chrome", "chromium", "chromium-browser", "chrome.exe", "msedge.exe"])
        if browser:
            run([
                browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf.resolve()}",
                str(html_report.resolve()),
            ], timeout=240)
        if pdf.exists():
            try:
                html_report.unlink()
            except OSError:
                pass
            return {"pdf": str(pdf.resolve())}
        # No browser to print with: the HTML report is the same document and prints from any browser.
        return {"pdf": "", "html": str(html_report.resolve())}


# --- what the steps are called -------------------------------------------------------------------------------

_TITLES = {
    "start_session": "Запуск сессии тестирования",
    "stop_session": "Завершение сессии",
    "open_navigation_link": "Открытие формы по ссылке",
    "open_form": "Открытие формы",
    "click_button": "Нажатие кнопки",
    "set_field_value": "Ввод значения в поле",
    "set_form_fields": "Заполнение полей формы",
    "toggle_field": "Переключение флажка",
    "activate_field": "Переход в поле",
    "choose_field_from_list": "Выбор значения из списка",
    "choose_field_first_row": "Выбор первого значения",
    "field_dropdown": "Открытие списка выбора",
    "save_form": "Запись формы",
    "close_form": "Закрытие формы",
    "form_state": "Состояние формы",
    "wait_form": "Ожидание формы",
    "wait_form_closed": "Ожидание закрытия формы",
    "wait_window": "Ожидание окна",
    "wait_client_idle": "Ожидание клиента",
    "windows_list": "Список окон",
    "active_window": "Активное окно",
    "activate_window": "Переход в окно",
    "ui_tree": "Дерево элементов формы",
    "table_rows": "Чтение строк таблицы",
    "table_find_row": "Поиск строки таблицы",
    "table_add_row": "Добавление строки",
    "table_delete_row": "Удаление строки",
    "table_set_field": "Ввод значения в таблицу",
    "table_current_row": "Текущая строка таблицы",
    "choose_current_row": "Выбор текущей строки",
    "command_interface_search": "Поиск команды интерфейса",
    "command_interface_sections": "Разделы интерфейса",
    "command_interface_commands": "Команды интерфейса",
    "find_object": "Поиск объекта",
    "search_objects": "Поиск объектов",
    "get_object": "Чтение объекта",
    "user_messages": "Сообщения пользователю",
    "screenshot": "Снимок экрана",
    "recording_capture": "Заметка",
    "html_click": "Клик на странице",
    "html_type": "Ввод на странице",
    "html_text": "Чтение текста страницы",
    "html_query": "Поиск элементов страницы",
}

# parameter -> its label, in the order they are shown
_DETAIL_PARAMS = (
    ("navigation_link", "Ссылка"), ("button", "Кнопка"), ("button_name", "Кнопка"), ("field", "Поле"),
    ("field_name", "Поле"), ("name", "Имя"), ("title", "Заголовок"), ("value", "Значение"), ("text", "Текст"),
    ("query", "Запрос"), ("form_name", "Форма"), ("note", "Примечание"), ("base_url", "База"),
)

# result key -> its label, in the order they are shown
_RESULT_FACTS = (
    ("form_name", "Форма"), ("active_title", "Окно"), ("title", "Окно"), ("modal_title", "Окно"),
    ("count", "Найдено"), ("opened", "Открыто"), ("connected", "Подключено"), ("saved", "Записано"),
)


def humanize(action: str) -> str:
    title = _TITLES.get(action)
    if title:
        return title
    if action.startswith("rag_"):
        return f"База знаний: {action[4:].replace('_', ' ')}"
    words = action.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Действие"


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def step_details(params: Any) -> list[tuple[str, str]]:
    """The few parameters worth reading, as (label, value) pairs; the full call goes to the appendix."""
    if not isinstance(params, dict):
        return []
    details: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, label in _DETAIL_PARAMS:
        value = params.get(key)
        if value in (None, "", [], {}) or label in seen:
            continue
        seen.add(label)
        details.append((label, _short(value)))
    fields = params.get("fields")
    if isinstance(fields, dict) and fields:
        shown = [f"{name}: {_short(value, 40)}" for name, value in list(fields.items())[:5]]
        more = len(fields) - len(shown)
        details.append(("Поля", "; ".join(shown) + (f"; ещё {more}" if more > 0 else "")))
    return details[:4]


def result_facts(result: Any) -> list[tuple[str, str]]:
    """What came back, in a few readable facts."""
    if not isinstance(result, dict):
        return [("Результат", _short(result, 140))] if result not in (None, "") else []
    facts: list[tuple[str, str]] = []
    seen: set[str] = set()
    error = result.get("error") or result.get("attach_error")
    if error:
        facts.append(("Ошибка", _short(error, 160)))
    for key, label in _RESULT_FACTS:
        value = result.get(key)
        if value in (None, "") or label in seen:
            continue
        seen.add(label)
        facts.append((label, "да" if value is True else "нет" if value is False else _short(value)))
    diagnostics = result.get("client_diagnostics")
    messages = result.get("user_messages") or (diagnostics.get("user_messages") if isinstance(diagnostics, dict) else None)
    if messages:
        facts.append(("Сообщения", _short(messages, 140)))
    if not facts:  # nothing familiar: show the plain values the call returned
        for key, value in result.items():
            if isinstance(value, (str, int, float, bool)) and value != "":
                facts.append((str(key), "да" if value is True else "нет" if value is False else _short(value)))
    return facts[:4]


def result_failed(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("error") or result.get("attach_error")) or result.get("ok") is False
    return False


# --- the report ----------------------------------------------------------------------------------------------

_CSS = """
@page { size: A4 landscape; margin: 0; }
@page tech { size: A4 landscape; margin: 14mm 16mm; }
:root { --ink: #1f2937; --muted: #6b7280; --line: #e5e7eb; --accent: #0f766e; --accent-soft: #ccfbf1;
        --bad: #b91c1c; --bad-soft: #fee2e2; --paper: #ffffff; --wash: #f3f6f8; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; color: var(--ink); background: var(--paper);
  font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, "DejaVu Sans", sans-serif; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.page { width: 297mm; height: 210mm; position: relative; overflow: hidden; page-break-after: always; break-after: page; }
.band { position: absolute; left: 0; top: 0; bottom: 0; width: 9mm; background: var(--accent); }
.cover { background: linear-gradient(135deg, #f0fdfa 0%, #ffffff 60%); }
.cover .band { width: 14mm; }
.cover .inner { position: absolute; left: 34mm; right: 30mm; top: 48mm; }
.kicker { color: var(--accent); font-weight: 600; letter-spacing: .14em; text-transform: uppercase; font-size: 11pt; }
.cover h1 { font-size: 40pt; line-height: 1.1; margin: 8mm 0 4mm; font-weight: 700; letter-spacing: -.01em; }
.cover .lead { color: var(--muted); font-size: 14pt; margin: 0 0 16mm; }
.cards { display: flex; gap: 6mm; flex-wrap: wrap; }
.card { background: var(--paper); border: 1px solid var(--line); border-radius: 4mm; padding: 5mm 7mm; min-width: 46mm;
        box-shadow: 0 1px 3px rgba(15, 23, 42, .06); }
.card .n { font-size: 24pt; font-weight: 700; }
.card .l { color: var(--muted); font-size: 10pt; margin-top: 1mm; }
.card.ok .n { color: var(--accent); } .card.bad .n { color: var(--bad); }
.facts { margin-top: 14mm; color: var(--muted); font-size: 11pt; line-height: 1.7; }
.facts b { color: var(--ink); font-weight: 600; }
.head { padding: 12mm 16mm 0 24mm; }
.head h2 { font-size: 22pt; margin: 0; font-weight: 700; }
.head .sub { color: var(--muted); font-size: 11pt; margin-top: 1.5mm; }
table.steps { width: calc(100% - 40mm); margin: 8mm 16mm 0 24mm; border-collapse: collapse; font-size: 10.5pt; }
table.steps th { text-align: left; color: var(--muted); font-weight: 600; font-size: 9pt; text-transform: uppercase;
                 letter-spacing: .06em; padding: 2mm 3mm; border-bottom: 2px solid var(--line); }
table.steps td { padding: 2.3mm 3mm; border-bottom: 1px solid var(--line); vertical-align: top; }
table.steps tr { page-break-inside: avoid; }
td.num { color: var(--muted); width: 12mm; } td.time { color: var(--muted); width: 22mm; white-space: nowrap; }
td.what { font-weight: 600; } td.what small { display: block; font-weight: 400; color: var(--muted); font-size: 9pt; margin-top: .5mm; }
td.st { width: 24mm; }
.pill { display: inline-block; border-radius: 99px; padding: .6mm 3mm; font-size: 9pt; font-weight: 600; background: var(--accent-soft); color: var(--accent); }
.pill.bad { background: var(--bad-soft); color: var(--bad); } .pill.none { background: var(--wash); color: var(--muted); }
.step .top { position: absolute; left: 24mm; right: 16mm; top: 10mm; display: flex; align-items: center; gap: 5mm; }
.step .badge { flex: none; width: 12mm; height: 12mm; border-radius: 50%; background: var(--accent); color: #fff; font-weight: 700;
               font-size: 14pt; display: flex; align-items: center; justify-content: center; }
.step.bad .badge { background: var(--bad); }
.step .ttl { flex: 1; min-width: 0; } .step .ttl h2 { margin: 0; font-size: 18pt; font-weight: 700; line-height: 1.15; }
.step .ttl .sub { color: var(--muted); font-size: 10.5pt; margin-top: 1mm; }
.shot { position: absolute; left: 24mm; right: 16mm; top: 29mm; height: 136mm; display: flex; align-items: center; justify-content: center;
        background: var(--wash); border: 1px solid var(--line); border-radius: 3mm; overflow: hidden; }
.shot img { max-width: 100%; max-height: 100%; object-fit: contain; display: block; }
.shot .none { color: var(--muted); font-size: 12pt; text-align: center; padding: 10mm; }
.shot .text { width: 100%; padding: 10mm 14mm; align-self: flex-start; }
.shot .text .h { color: var(--muted); font-size: 9pt; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 4mm; }
.shot .text .big { font-size: 16pt; line-height: 1.5; } .shot .text .big span { color: var(--muted); }
.shot .text .big.bad { color: var(--bad); }
.cap { position: absolute; left: 24mm; right: 16mm; top: 170mm; display: flex; gap: 8mm; font-size: 10.5pt; line-height: 1.5; }
.cap .col { flex: 1; min-width: 0; } .cap .h { color: var(--muted); font-size: 8.5pt; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 1mm; }
.cap .row span { color: var(--muted); } .cap .bad { color: var(--bad); font-weight: 600; }
.foot { position: absolute; left: 24mm; right: 16mm; bottom: 6mm; display: flex; justify-content: space-between; color: #9ca3af; font-size: 8.5pt; }
.appendix { page: tech; }
.appendix h2 { font-size: 20pt; margin: 0 0 1mm; } .appendix .sub { color: var(--muted); font-size: 10pt; margin-bottom: 6mm; }
.tech { border-top: 1px solid var(--line); padding: 3mm 0 4mm; page-break-inside: avoid; break-inside: avoid; }
.tech h3 { margin: 0 0 2mm; font-size: 10pt; font-weight: 600; } .tech h3 span { color: var(--muted); font-weight: 400; margin-left: 3mm; }
.tech .grid { display: flex; gap: 5mm; } .tech .grid > div { flex: 1; min-width: 0; }
.tech .lbl { color: var(--muted); font-size: 7.5pt; text-transform: uppercase; letter-spacing: .08em; margin-bottom: .8mm; }
.tech pre { margin: 0; background: var(--wash); border-radius: 2mm; padding: 2mm 3mm; font-size: 6.8pt; line-height: 1.35; color: #374151;
            font-family: Consolas, "DejaVu Sans Mono", "Courier New", monospace; white-space: pre-wrap; word-break: break-word; }
"""


def _fmt_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%H:%M:%S")
    except ValueError:
        return value


def _duration(started_at: float, events: list[dict[str, Any]]) -> str:
    try:
        end = datetime.fromisoformat(str(events[-1]["time"])).timestamp()
    except (KeyError, ValueError, IndexError):
        return "—"
    seconds = max(0, int(end - started_at)) if started_at else 0
    minutes, secs = divmod(seconds, 60)
    return f"{minutes} мин {secs:02d} с" if minutes else f"{secs} с"


def _status(event: dict[str, Any]) -> tuple[str, str]:
    if event.get("failed"):
        return "bad", "Ошибка"
    if event.get("screenshot_error"):
        return "none", "Без снимка"
    return "ok", "Выполнено"


def _json_block(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + "\n… (обрезано)"
    return html.escape(text)


def _image_tag(frame: str) -> str:
    try:
        data = base64.b64encode(Path(frame).read_bytes()).decode("ascii")
    except OSError:
        return '<div class="none">Снимок экрана недоступен</div>'
    return f'<img alt="" src="data:image/png;base64,{data}">'


def _cover(title: str, meta: dict[str, str], events: list[dict[str, Any]], started_at: float) -> str:
    failed = sum(1 for e in events if e.get("failed"))
    # локальное время отчёта (как и раньше), но с явной зоной: ruff DTZ006 запрещает наивное fromtimestamp()
    local_tz = datetime.now().astimezone().tzinfo
    started = datetime.fromtimestamp(started_at, tz=local_tz) if started_at else datetime.now(tz=local_tz)
    cards = [
        f'<div class="card"><div class="n">{len(events)}</div><div class="l">шагов</div></div>',
        f'<div class="card"><div class="n">{html.escape(_duration(started_at, events))}</div><div class="l">длительность</div></div>',
    ]
    if failed:
        cards.append(f'<div class="card bad"><div class="n">{failed}</div><div class="l">с ошибкой</div></div>')
    else:
        cards.append('<div class="card ok"><div class="n">✓</div><div class="l">без ошибок</div></div>')
    facts = "".join(f"<div><b>{html.escape(k)}:</b> {html.escape(v)}</div>" for k, v in meta.items())
    return (
        '<section class="page cover"><div class="band"></div><div class="inner">'
        '<div class="kicker">Answer42 · доказательная запись</div>'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="lead">{started.strftime("%d.%m.%Y, %H:%M")}</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<div class="facts">{facts}</div></div></section>'
    )


def _table_pages(events: list[dict[str, Any]]) -> str:
    rows = []
    for event in events:
        css, label = _status(event)
        details = step_details(event.get("params"))
        sub = " · ".join(f"{k}: {v}" for k, v in details)
        rows.append(
            f'<tr><td class="num">{event["index"]}</td><td class="time">{_fmt_time(str(event.get("time", "")))}</td>'
            f'<td class="what">{html.escape(humanize(str(event.get("action", ""))))}'
            f"{f'<small>{html.escape(sub)}</small>' if sub else ''}</td>"
            f'<td class="st"><span class="pill {"bad" if css == "bad" else "none" if css == "none" else ""}">{label}</span></td></tr>'
        )
    return (
        '<section class="page"><div class="band"></div><div class="head"><h2>Ход проверки</h2>'
        '<div class="sub">Что выполнялось по шагам. Подробности каждого шага следуют на отдельных страницах.</div></div>'
        '<table class="steps"><thead><tr><th>№</th><th>Время</th><th>Шаг</th><th>Итог</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></section>'
    )


def _text_card(facts: list[tuple[str, str]], result: Any) -> str:
    """The body of a step without a screenshot (knowledge-base calls and the like): what came back, in words."""
    lines = [
        f'<div class="big {"bad" if key == "Ошибка" else ""}"><span>{html.escape(key)}:</span> {html.escape(value)}</div>'
        for key, value in facts
    ]
    if not lines and result not in (None, "", {}, []):
        lines.append(f'<div class="big">{html.escape(_short(result, 400))}</div>')
    return f'<div class="text"><div class="h">Снимок экрана не требовался</div>{"".join(lines)}</div>'


def _step_page(event: dict[str, Any], total: int) -> str:
    css, label = _status(event)
    action = str(event.get("action", ""))
    details = step_details(event.get("params"))
    facts = result_facts(event.get("raw_result"))
    if event.get("frame"):
        shot = _image_tag(event["frame"])
    elif event.get("text_only"):
        shot = _text_card(facts, event.get("raw_result"))
    else:
        shot = '<div class="none">Снимок экрана недоступен</div>'
    shot_class = "shot text-only" if event.get("text_only") and not event.get("frame") else "shot"
    done = "".join(f'<div class="row"><span>{html.escape(k)}:</span> {html.escape(v)}</div>' for k, v in details) or (
        '<div class="row"><span>Параметры не требуются</span></div>')
    got = "".join(
        f'<div class="row {"bad" if k == "Ошибка" else ""}"><span>{html.escape(k)}:</span> {html.escape(v)}</div>' for k, v in facts
    ) or f'<div class="row"><span>{html.escape(label)}</span></div>'
    return (
        f'<section class="page step {"bad" if css == "bad" else ""}"><div class="band"></div>'
        f'<div class="top"><div class="badge">{event["index"]}</div><div class="ttl"><h2>{html.escape(humanize(action))}</h2>'
        f'<div class="sub">{_fmt_time(str(event.get("time", "")))} · {html.escape(label)}</div></div></div>'
        f'<div class="{shot_class}">{shot}</div>'
        f'<div class="cap"><div class="col"><div class="h">Что сделано</div>{done}</div>'
        f'<div class="col"><div class="h">Что получилось</div>{got}</div></div>'
        f'<div class="foot"><span>Шаг {event["index"]} из {total}</span><span>Answer42</span></div></section>'
    )


def _appendix(events: list[dict[str, Any]]) -> str:
    blocks = []
    for event in events:
        blocks.append(
            f'<div class="tech"><h3>{event["index"]}. {html.escape(humanize(str(event.get("action", ""))))}'
            f'<span>{html.escape(str(event.get("action", "")))} · {_fmt_time(str(event.get("time", "")))}</span></h3>'
            f'<div class="grid"><div><div class="lbl">Запрос</div><pre>{_json_block(event.get("params"), 1500)}</pre></div>'
            f'<div><div class="lbl">Ответ</div><pre>{_json_block(event.get("raw_result"), 2600)}</pre></div></div></div>'
        )
    return (
        '<section class="appendix"><h2>Приложение. Технические детали</h2>'
        '<div class="sub">Вызовы MCP-инструментов и ответы в исходном виде, для отладки. Пароли скрыты.</div>'
        f'{"".join(blocks)}</section>'
    )


def make_report_html(title: str, meta: dict[str, str], events: list[dict[str, Any]], started_at: float = 0.0) -> str:
    total = len(events)
    pages = [_cover(title, meta, events, started_at), _table_pages(events)]
    pages.extend(_step_page(event, total) for event in events)
    pages.append(_appendix(events))
    return (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{_CSS}</style></head><body>{''.join(pages)}</body></html>"
    )


# --- helpers -------------------------------------------------------------------------------------------------


def run(args: list[str], timeout: int = 180) -> None:
    try:
        subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        pass


def first_existing(names: list[str]) -> str:
    from shutil import which
    for name in names:
        path = which(name)
        if path:
            return path
    if os.name == "nt":
        env_paths = [
            os.getenv("ProgramFiles"),
            os.getenv("ProgramFiles(x86)"),
            os.getenv("LocalAppData"),
        ]
        candidates = []
        for root in filter(None, env_paths):
            base = Path(str(root))
            candidates.extend([
                base / "Google/Chrome/Application/chrome.exe",
                base / "Microsoft/Edge/Application/msedge.exe",
                base / "Programs/Microsoft VS Code/chrome.exe",
            ])
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return ""


SENSITIVE_KEYS = {"password", "pwd", "pass", "secret", "token", "authorization", "auth"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS or "password" in str(key).lower():
                result[key] = "***"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def safe_name(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80]
    return result or "action"


def summarize(value: Any, limit: int = 900) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"
