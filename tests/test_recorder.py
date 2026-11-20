from pathlib import Path

from mcp_1c import recorder
from mcp_1c.recorder import RecordingSession


def test_recording_paths_are_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    session = RecordingSession()
    start = session.start("relative-recordings")

    assert Path(start["output_dir"]).is_absolute()
    assert session.output_dir == (tmp_path / "relative-recordings").resolve()

    session.capture(
        lambda path, window, display: Path(path).write_bytes(b"not-a-real-png-but-not-rendered"),
        "text-only step",
        {},
        {},
        screenshot=False,
    )

    def fake_run(args, timeout=180):
        for arg in args:
            if arg.startswith("--print-to-pdf="):
                Path(arg.removeprefix("--print-to-pdf=")).write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(recorder, "first_existing", lambda names: "fake-browser")
    monkeypatch.setattr(recorder, "run", fake_run)

    stop = session.stop()

    assert Path(stop["output_dir"]).is_absolute()
    assert Path(stop["pdf"]).is_absolute()
    assert Path(stop["pdf"]).exists()


def _recorded_session(tmp_path, monkeypatch, browser=""):
    monkeypatch.setattr(recorder, "first_existing", lambda names: browser)
    session = RecordingSession()
    session.start(str(tmp_path / "rec"), title="Проверка номенклатуры", meta={"База": "http://127.0.0.1/base/"})
    session.capture(
        lambda path, window, display: Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"),
        "open_navigation_link",
        {"navigation_link": "e1cib/list/Справочник.Номенклатура", "password": "secret-pass"},
        {"opened": True, "client_diagnostics": {"user_messages": [], "error_info": ""}},
    )
    session.capture(lambda *_args: None, "click_button", {"button": "Записать"}, {"error": "Кнопка не найдена"}, screenshot=False)
    return session


def test_report_keeps_technical_details_in_the_appendix(tmp_path, monkeypatch):
    session = _recorded_session(tmp_path, monkeypatch)
    page = recorder.make_report_html(session.title, session.meta, session.events, session.started_at)

    readable, appendix = page.split("Приложение. Технические детали")
    assert "Проверка номенклатуры" in readable and "Открытие формы по ссылке" in readable
    assert "Кнопка не найдена" in readable  # the failure is told in words
    assert "client_diagnostics" not in readable and "navigation_link" not in readable
    assert "client_diagnostics" in appendix and "navigation_link" in appendix
    assert "secret-pass" not in page and "***" in appendix


def test_steps_are_marked_failed_and_named_in_words(tmp_path, monkeypatch):
    session = _recorded_session(tmp_path, monkeypatch)

    assert [event["failed"] for event in session.events] == [False, True]
    assert recorder.humanize("click_button") == "Нажатие кнопки"
    assert recorder.humanize("some_new_tool") == "Some new tool"
    assert recorder.humanize("rag_search") == "База знаний: search"
    assert recorder.result_facts({"hits": 5}) == [("hits", "5")]


def test_without_a_browser_the_html_report_is_kept(tmp_path, monkeypatch):
    session = _recorded_session(tmp_path, monkeypatch, browser="")

    stop = session.stop()

    assert stop["pdf"] == "" and Path(stop["html"]).exists()

def _events(count):
    return [
        {"index": i, "time": "2026-11-20T10:00:00", "action": "table_goto_row", "params": {"name": "Список"}, "raw_result": {"found": True}}
        for i in range(1, count + 1)
    ]


def test_long_step_table_continues_on_the_next_sheet():
    html_report = recorder.make_report_html("Прогон", {}, _events(59))
    sheets = html_report.count("Ход проверки")
    assert sheets >= 4
    assert f"лист {sheets} из {sheets}" in html_report
    for index in range(1, 60):
        assert f'<td class="num">{index}</td>' in html_report


def test_short_step_table_is_one_sheet():
    html_report = recorder.make_report_html("Прогон", {}, _events(5))
    assert html_report.count("Ход проверки") == 1
    assert "лист 1 из" not in html_report


def test_table_steps_have_russian_titles():
    for action in ("table_goto_row", "table_move_row", "dynamic_list_find", "choose_value_type"):
        assert recorder.humanize(action)[0] in "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ"
