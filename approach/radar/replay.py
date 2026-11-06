"""Turn a session log into a self-contained HTML radar replay (no server needed)."""

from __future__ import annotations

import html
from pathlib import Path

from approach.session import SessionLog

TEMPLATE = Path(__file__).with_name("template.html")


def render(log: SessionLog) -> str:
    payload = log.to_json().replace("</", "<\\/")
    title = html.escape(log.meta.get("title") or "Radar replay")
    return TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", title).replace("__SESSION__", payload)


def write(log: SessionLog, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(log), encoding="utf-8")
    return path
