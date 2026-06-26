#!/usr/bin/env python3
"""Build a small static GitLab Pages site from project Markdown docs."""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
DOCS = ROOT / "docs"

PAGES = [
    ("index.html", ROOT / "README.md", "README"),
    ("installation.html", DOCS / "installation.md", "Установка"),
    ("agent-installation.html", DOCS / "agent-installation.md", "Инструкция для AI-агентов"),
    ("architecture.html", DOCS / "architecture.md", "Архитектура"),
]

NAV = [
    ("index.html", "README"),
    ("installation.html", "Установка"),
    ("agent-installation.html", "AI-агентам"),
    ("architecture.html", "Архитектура"),
]

CSS = """
:root { color-scheme: light dark; --bg:#0b1020; --panel:#111936; --text:#eef3ff; --muted:#aebbe2; --link:#67e8f9; --line:#2b365f; --code:#080d1c; }
@media (prefers-color-scheme: light) { :root { --bg:#f7f9ff; --panel:#ffffff; --text:#111827; --muted:#526078; --link:#0369a1; --line:#dbe3f5; --code:#f1f5f9; } }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.62 Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background:radial-gradient(circle at 20% 0%, rgba(103,232,249,.18), transparent 28rem), var(--bg); }
.wrapper { max-width: 1120px; margin: 0 auto; padding: 28px 20px 56px; }
.header { display:flex; align-items:center; gap:18px; padding:22px 0 18px; }
.logo { width:72px; height:72px; object-fit:contain; filter: drop-shadow(0 12px 24px rgba(0,0,0,.28)); }
.kicker { color:var(--muted); margin:0; }
h1,h2,h3,h4 { line-height:1.22; margin:1.6em 0 .55em; }
h1 { font-size: clamp(2rem, 5vw, 3.8rem); margin-top:.2em; letter-spacing:-.04em; }
h2 { font-size:1.8rem; border-top:1px solid var(--line); padding-top:1.1em; }
a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
.nav { display:flex; flex-wrap:wrap; gap:10px; margin: 12px 0 28px; }
.nav a { padding:8px 12px; border:1px solid var(--line); border-radius:999px; background:rgba(255,255,255,.04); }
main { background: color-mix(in srgb, var(--panel) 92%, transparent); border:1px solid var(--line); border-radius:28px; padding: clamp(20px, 4vw, 44px); box-shadow:0 24px 80px rgba(0,0,0,.22); }
pre { overflow:auto; padding:16px 18px; background:var(--code); border:1px solid var(--line); border-radius:16px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:.92em; background:var(--code); border-radius:6px; padding:.12em .32em; }
pre code { background:transparent; padding:0; }
blockquote { margin:1.2em 0; padding:.5em 1.2em; border-left:4px solid var(--link); color:var(--muted); }
table { width:100%; border-collapse:collapse; margin:1.2em 0; display:block; overflow:auto; }
th,td { border:1px solid var(--line); padding:9px 11px; vertical-align:top; }
th { background:rgba(255,255,255,.07); }
img { max-width:100%; height:auto; }
.brand { margin-left:auto; display:flex; align-items:center; gap:10px; padding:8px 12px; border:1px solid var(--line); border-radius:18px; background:rgba(255,255,255,.06); }
.brand span { color:var(--muted); font-size:.82rem; }
.brand img { width:92px; height:auto; display:block; }
.footer { color:var(--muted); margin-top:22px; font-size:.92rem; }
""".strip()


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^0-9a-zа-яё_-]+", "-", text, flags=re.I)
    return text.strip("-") or "section"


def page_link(url: str) -> str:
    mapping = {
        "README.md": "index.html",
        "docs/installation.md": "installation.html",
        "docs/agent-installation.md": "agent-installation.html",
        "docs/architecture.md": "architecture.html",
        "docs/assets/answer42-logo.png": "assets/answer42-logo.png",
        "docs/assets/platform42-logo.svg": "assets/platform42-logo.svg",
    }
    return mapping.get(url, url)


def convert_inline(text: str) -> str:
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\u0000{len(placeholders) - 1}\u0000"

    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{m.group(1)}</code>"), text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", lambda m: stash(f'<img src="{html.escape(page_link(m.group(2)), quote=True)}" alt="{html.escape(m.group(1), quote=True)}">'), text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: stash(f'<a href="{html.escape(page_link(m.group(2)), quote=True)}">{m.group(1)}</a>'), text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    # Some placeholders can be nested, for example Markdown links with code-formatted text:
    # [`file.md`](file.md). Resolve repeatedly until no marker remains.
    for _ in range(3):
        changed = False
        for i, value in enumerate(placeholders):
            marker = f"\u0000{i}\u0000"
            if marker in text:
                text = text.replace(marker, value)
                changed = True
        if not changed:
            break
    return text


def markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    para: list[str] = []
    in_ul = False
    in_ol = False
    in_quote = False
    table: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{convert_inline(' '.join(para))}</p>")
            para = []

    def flush_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def flush_quote() -> None:
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        rows = []
        for row in table:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            rows.append(cells)
        if len(rows) >= 2 and all(re.fullmatch(r":?-{3,}:?", c) for c in rows[1]):
            header = rows[0]
            body = rows[2:]
            html_rows = ["<table><thead><tr>" + "".join(f"<th>{convert_inline(c)}</th>" for c in header) + "</tr></thead><tbody>"]
            for r in body:
                html_rows.append("<tr>" + "".join(f"<td>{convert_inline(c)}</td>" for c in r) + "</tr>")
            html_rows.append("</tbody></table>")
            out.append("".join(html_rows))
        else:
            for row in table:
                out.append(f"<p>{convert_inline(row)}</p>")
        table = []

    for line in lines:
        if line.startswith("```"):
            flush_para()
            flush_lists()
            flush_quote()
            flush_table()
            if not in_code:
                in_code = True
                code_lang = line[3:].strip()
                cls = f' class="language-{html.escape(code_lang, quote=True)}"' if code_lang else ""
                out.append(f"<pre><code{cls}>")
            else:
                out.append("</code></pre>")
                in_code = False
            continue
        if in_code:
            out.append(html.escape(line) + "\n")
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            flush_para()
            flush_lists()
            flush_quote()
            table.append(line)
            continue
        else:
            flush_table()
        if not line.strip():
            flush_para()
            flush_lists()
            flush_quote()
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            flush_para()
            flush_lists()
            flush_quote()
            level = len(m.group(1))
            title = m.group(2).strip()
            out.append(f'<h{level} id="{slugify(title)}">{convert_inline(title)}</h{level}>' )
            continue
        if line.startswith(">"):
            flush_para()
            flush_lists()
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append(f"<p>{convert_inline(line.lstrip('> ').strip())}</p>")
            continue
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            flush_para()
            flush_quote()
            if not in_ul:
                flush_lists()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{convert_inline(m.group(1))}</li>")
            continue
        m = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if m:
            flush_para()
            flush_quote()
            if not in_ol:
                flush_lists()
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{convert_inline(m.group(1))}</li>")
            continue
        flush_lists()
        flush_quote()
        para.append(line.strip())

    flush_para()
    flush_lists()
    flush_quote()
    flush_table()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def page_html(title: str, body: str) -> str:
    nav = "".join(f'<a href="{href}">{label}</a>' for href, label in NAV)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — Answer42</title>
  <link rel="icon" href="assets/answer42-logo.png">
  <style>{CSS}</style>
</head>
<body>
  <div class="wrapper">
    <header class="header">
      <img class="logo" src="assets/answer42-logo.png" alt="Answer42 logo">
      <div>
        <p class="kicker">The Answer to Life, Universe, and 1C — UI Driver</p>
        <h1>Answer42</h1>
      </div>
      <a class="brand" href="https://42clouds.com/" aria-label="Platform42">
        <span>by</span>
        <img src="assets/platform42-logo.svg" alt="Platform42">
      </a>
    </header>
    <nav class="nav">{nav}</nav>
    <main>{body}</main>
    <div class="footer">Generated from repository Markdown by <code>scripts/build_pages.py</code>.</div>
  </div>
</body>
</html>
"""


def main() -> None:
    if PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    (PUBLIC / "assets").mkdir(parents=True)
    shutil.copy2(DOCS / "assets" / "answer42-logo.png", PUBLIC / "assets" / "answer42-logo.png")
    shutil.copy2(DOCS / "assets" / "platform42-logo.svg", PUBLIC / "assets" / "platform42-logo.svg")
    for out_name, src, title in PAGES:
        if not src.exists():
            continue
        markdown = src.read_text(encoding="utf-8")
        markdown = re.sub(
            r'<p\s+align="center">\s*<img\s+src="([^"]+)"\s+alt="([^"]*)"\s+width="([^"]+)"\s*>\s*</p>',
            r'![\2](\1)',
            markdown,
            flags=re.S,
        )
        body = markdown_to_html(markdown)
        (PUBLIC / out_name).write_text(page_html(title, body), encoding="utf-8")
    print(f"Built {len(list(PUBLIC.glob('*.html')))} pages into {PUBLIC}")


if __name__ == "__main__":
    main()
