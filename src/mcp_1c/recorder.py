from __future__ import annotations

import base64
import html
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class RecordingSession:
    active: bool = False
    output_dir: Path = Path("build/recordings/session")
    display: str = ""
    window: bool = False
    frames_dir: Path = Path("build/recordings/session/frames")
    slides_dir: Path = Path("build/recordings/session/slides")
    events: list[dict[str, Any]] = field(default_factory=list)

    def start(self, output_dir: str, display: str = "", window: bool = False) -> dict[str, Any]:
        self.active = True
        self.output_dir = Path(output_dir)
        self.display = display
        self.window = window
        self.frames_dir = self.output_dir / "frames"
        self.slides_dir = self.output_dir / "slides"
        self.events = []
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.slides_dir.mkdir(parents=True, exist_ok=True)
        return {"active": True, "output_dir": str(self.output_dir)}

    def stop(self) -> dict[str, Any]:
        self.active = False
        manifest = self.output_dir / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"events": self.events}, ensure_ascii=False, indent=2), encoding="utf-8")
        media = self.build_media()
        return {"active": False, "output_dir": str(self.output_dir), "events": len(self.events), **media}

    def capture(self, screenshot_func, action: str, params: dict[str, Any], result: Any) -> dict[str, Any] | None:
        if not self.active:
            return None
        index = len(self.events) + 1
        frame = self.frames_dir / f"{index:03d}_{safe_name(action)}.png"
        try:
            screenshot_func(str(frame), self.window, self.display)
        except Exception as exc:
            event = {"index": index, "action": action, "params": params, "result": summarize(result), "screenshot_error": str(exc)}
            self.events.append(event)
            return event
        slide = self.slides_dir / f"{index:03d}_{safe_name(action)}.svg"
        slide.write_text(make_slide_svg(frame, action, params, result, index), encoding="utf-8")
        event = {
            "index": index,
            "action": action,
            "params": params,
            "result": summarize(result),
            "frame": str(frame),
            "slide": str(slide),
        }
        self.events.append(event)
        return event

    def build_media(self) -> dict[str, Any]:
        slides = sorted(self.slides_dir.glob("*.svg"))
        if not slides:
            return {"gif": "", "mp4": "", "pdf": ""}
        gif = self.output_dir / "recording.gif"
        mp4 = self.output_dir / "recording.mp4"
        pdf = self.output_dir / "recording.pdf"
        pattern = str(self.slides_dir / "*.svg")
        run(["ffmpeg", "-y", "-framerate", "0.8", "-pattern_type", "glob", "-i", pattern, "-vf", "fps=12,scale=1280:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", str(gif)])
        run(["ffmpeg", "-y", "-framerate", "0.8", "-pattern_type", "glob", "-i", pattern, "-vf", "fps=24,scale=1920:1080:flags=lanczos,format=yuv420p", "-movflags", "+faststart", str(mp4)])
        html_deck = self.output_dir / "recording.html"
        html_deck.write_text(make_html_deck(slides), encoding="utf-8")
        browser = first_existing(["google-chrome", "chromium", "chromium-browser"])
        if browser:
            run([
                browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                f"--print-to-pdf={pdf.resolve()}",
                str(html_deck.resolve()),
            ], timeout=240)
        if not pdf.exists():
            converter = first_existing(["magick", "convert"])
            if converter:
                run([converter, *map(str, slides), str(pdf)], timeout=240)
        return {
            "gif": str(gif) if gif.exists() else "",
            "mp4": str(mp4) if mp4.exists() else "",
            "pdf": str(pdf) if pdf.exists() else "",
            "html": str(html_deck) if html_deck.exists() else "",
        }


def make_html_deck(slides: list[Path]) -> str:
    pages = []
    for slide in slides:
        data = slide.read_text(encoding="utf-8")
        pages.append(f'<section class="page">{data}</section>')
    return '''<!doctype html>
<html><head><meta charset="utf-8"><style>
@page { size: 16in 9in; margin: 0; }
html, body { margin: 0; padding: 0; background: #111827; }
.page { width: 16in; height: 9in; page-break-after: always; overflow: hidden; }
.page svg { width: 16in; height: 9in; display: block; }
</style></head><body>''' + "\n".join(pages) + "</body></html>"


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
    return ""


def safe_name(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80]
    return result or "action"


def summarize(value: Any, limit: int = 900) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def make_slide_svg(frame: Path, action: str, params: dict[str, Any], result: Any, index: int) -> str:
    data = base64.b64encode(frame.read_bytes()).decode("ascii")
    param_text = summarize(params, 700)
    result_text = summarize(result, 900)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  <rect width="1920" height="1080" fill="#111827"/>
  <rect x="24" y="24" width="1344" height="1032" rx="18" fill="#0b1220"/>
  <image x="48" y="48" width="1296" height="972" href="data:image/png;base64,{data}" preserveAspectRatio="xMidYMid meet"/>
  <rect x="1392" y="24" width="504" height="1032" rx="18" fill="#f8fafc"/>
  <text x="1424" y="78" font-family="Arial" font-size="28" font-weight="700" fill="#111827">#{index} {html.escape(action)}</text>
  <text x="1424" y="116" font-family="Arial" font-size="16" fill="#64748b">{html.escape(now)}</text>
  <text x="1424" y="170" font-family="Arial" font-size="22" font-weight="700" fill="#0f172a">Параметры</text>
  {multiline_text(param_text, 1424, 204, 440, 17, '#334155')}
  <text x="1424" y="550" font-family="Arial" font-size="22" font-weight="700" fill="#0f172a">Результат</text>
  {multiline_text(result_text, 1424, 584, 440, 17, '#334155')}
</svg>'''


def multiline_text(text: str, x: int, y: int, width: int, font_size: int, color: str) -> str:
    chars = max(20, width // (font_size // 2))
    lines: list[str] = []
    for raw in text.splitlines() or [""]:
        s = raw
        while len(s) > chars:
            lines.append(s[:chars])
            s = s[chars:]
        lines.append(s)
    out = []
    for i, line in enumerate(lines[:22]):
        out.append(f'<text x="{x}" y="{y + i * (font_size + 5)}" font-family="Arial" font-size="{font_size}" fill="{color}">{html.escape(line)}</text>')
    return "\n  ".join(out)
