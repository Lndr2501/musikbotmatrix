"""Kleine Helfer: Zeiten, HTML für Matrix-Nachrichten, Text-Parsing."""

from __future__ import annotations

import html
import re
import time

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def parse_time(value: str) -> float | None:
    """`90`, `1:30`, `12:34` oder `1h2m3s` -> Sekunden."""
    value = value.strip().lower().replace(",", ".")
    if not value:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value):
        parts = [int(p) for p in value.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", value)
    if match and any(match.groups()):
        h, m, s = (int(g) if g else 0 for g in match.groups())
        return float(h * 3600 + m * 60 + s)
    return None


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "??:??"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def plain(text: str) -> dict:
    """m.text ohne Formatierung."""
    return {"msgtype": "m.text", "body": text}


def notice(text: str) -> dict:
    return {"msgtype": "m.notice", "body": text}


def rich(body: str, html_body: str) -> dict:
    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.html",
        "formatted_body": html_body,
    }


def now_int() -> int:
    return int(time.time())


def now_ms() -> int:
    return int(time.time() * 1000)
