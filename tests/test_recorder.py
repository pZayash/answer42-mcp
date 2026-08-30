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
