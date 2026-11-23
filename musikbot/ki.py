"""KI-Anbindung fuer Blick: OpenAI-kompatibler Endpunkt (eigener Server empfohlen).

Nur Text rein/raus plus Bilder; nichts wird gespeichert. Antworten werden gestreamt
(manche Proxys verlangen `stream: true`), Denk-Modus wird abgeschaltet (Qwen3-Stil
`chat_template_kwargs.enable_thinking`), damit ein Satz nicht hinter Minuten Nachdenken haengt.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Optional

import aiohttp

log = logging.getLogger("musikbot.ki")

MAX_SIDE = 1024          # Bildkante fuers Modell: Bildschirmtext bleibt lesbar, Anfrage klein
JPEG_QUALITY = 82


def image_part(img, max_side: int = MAX_SIDE, quality: int = JPEG_QUALITY) -> dict:
    """PIL-Bild -> OpenAI-`image_url`-Teil (JPEG, Kante hoechstens `max_side`)."""
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return {"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D]")


def clean_line(text: str, max_words: int = 30) -> str:
    """Modellantwort -> ein sprechbarer Satz: ohne Emojis, Anfuehrungszeichen, Markdown; gekuerzt."""
    t = _EMOJI.sub("", text or "")
    t = re.sub(r"[*_`#>]+", "", t)
    t = " ".join(t.split()).strip().strip('"„“”\'').strip()
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip(",;:–-") + "…"
    return t


class KI:
    def __init__(self, url: str, key: str, model: str, timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.url and self.model)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        return h

    async def chat(self, messages: list[dict], max_tokens: int = 120,
                   temperature: float = 0.9) -> Optional[str]:
        """Antworttext oder None (Fehler/Zeitueberschreitung werden geloggt, nicht geworfen)."""
        if not self.available:
            return None
        body = {"model": self.model, "messages": messages, "stream": True,
                "max_tokens": max_tokens, "temperature": temperature,
                "chat_template_kwargs": {"enable_thinking": False}}
        parts: list[str] = []
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.url + "/chat/completions", json=body,
                                  headers=self._headers()) as r:
                    if r.status != 200:
                        log.warning("KI antwortet %s: %s", r.status, (await r.text())[:200])
                        return None
                    async for raw in r.content:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        for ch in chunk.get("choices") or []:
                            piece = (ch.get("delta") or {}).get("content") or \
                                (ch.get("message") or {}).get("content")
                            if piece:
                                parts.append(piece)
        except Exception as exc:  # noqa: BLE001
            log.warning("KI-Anfrage fehlgeschlagen: %s", exc)
            return None
        text = "".join(parts).strip()
        return text or None
