"""KI-Anbindung fuer Blick und Ohr: OpenAI-kompatibler Endpunkt (eigener Server empfohlen).

Nur Text rein/raus plus optional ein Bild; nichts wird gespeichert. Antworten werden gestreamt
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


def image_part(img) -> dict:
    """PIL-Bild -> OpenAI-`image_url`-Teil (JPEG, Kante hoechstens MAX_SIDE)."""
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
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


# ---------------------------------------------------------------- Sprache -> Text (Ohr)
import wave  # noqa: E402

import numpy as np  # noqa: E402

STT_RATE = 16_000

# Typische Whisper-Halluzinationen auf Stille/Musik: als Teil des Texts -> ganzer Text verworfen
_HALLU_PARTS = (
    "untertitel im auftrag", "untertitel der amara", "untertitel von", "untertitelung",
    "zdf für funk", "zdf, 20", "swr 20", "copyright", "www.", "http", ".com", ".de/",
    "abonniert den kanal", "abonnieren sie", "fürs zuschauen", "für's zuschauen",
    "für das zuschauen", "thanks for watching", "thank you for watching",
)
# ... oder wenn der ganze Text nur das ist
_HALLU_WHOLE = {
    "vielen dank", "danke", "danke schön", "dankeschön", "tschüss", "bis zum nächsten mal",
    "bis bald", "musik", "applaus", "lachen", "stille", "you", "thank you", "bye", "amen",
    "ja", "äh", "ähm", "hm", "hmm", "mhm",
}
_BRACKETS = re.compile(r"[\[\(\*♪][^\]\)\*♪]{0,40}[\]\)\*♪]")


def stt_clean(text: Optional[str]) -> Optional[str]:
    """Transkript saeubern; None, wenn nur Halluzination/Geraeusch/Leeres uebrig bleibt."""
    t = _BRACKETS.sub(" ", text or "")
    t = t.replace("♪", " ")
    t = " ".join(t.split()).strip()
    low = t.lower()
    if sum(c.isalpha() for c in t) < 2:
        return None
    if any(p in low for p in _HALLU_PARTS):
        return None
    if re.sub(r"[^\wäöüß ]+", "", low).strip() in _HALLU_WHOLE:
        return None
    return t


def prompt_echo(text: str, prompt: str) -> bool:
    """Whisper gibt bei Stille/Rauschen gern den Prompt zurueck ("Qwen"): nur Prompt-Woerter = Echo."""
    words = re.findall(r"[\wäöüß]+", (text or "").lower())
    known = set(re.findall(r"[\wäöüß]+", (prompt or "").lower()))
    return bool(words) and bool(known) and all(w in known for w in words)


def wav_bytes(pcm: np.ndarray, rate: int = STT_RATE) -> bytes:
    """int16 mono -> WAV-Datei im Speicher."""
    data = np.asarray(pcm, dtype=np.int16).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return buf.getvalue()


def segments_ok(data: dict) -> bool:
    """Whisper-Segmente pruefen: False bei Stille (alle no_speech_prob > 0.6), unsicherem
    Raten (avg_logprob < -1.0 ueberall) oder Wiederholungsschleifen (compression_ratio > 2.4)."""
    segs = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segs, list) or not segs:
        return True
    segs = [g for g in segs if isinstance(g, dict)]
    if not segs:
        return True
    if any(float(g.get("compression_ratio") or 0.0) > 2.4 for g in segs):
        return False
    if all(float(g.get("no_speech_prob") or 0.0) > 0.6 for g in segs):
        return False
    if all(float(g.get("avg_logprob") if g.get("avg_logprob") is not None else 0.0) < -1.0 for g in segs):
        return False
    return True


def health_url(url: str) -> str:
    """`http://host:8000/v1` -> `http://host:8000/health`."""
    base = (url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/health"


class STT:
    """Sprache -> Text ueber `{url}/audio/transcriptions` (OpenAI-Format, multipart).
    Der Server darf schlafen (Laptop): `reachable()` prueft `/health`, Ergebnis 60 s gemerkt."""

    HEALTH_TTL = 60.0
    HEALTH_TIMEOUT = 3.0

    def __init__(self, url: str, key: str = "", model: str = "whisper-1", language: str = "de",
                 timeout: float = 15.0) -> None:
        self.url = (url or "").rstrip("/")
        self.key = key
        self.model = model
        self.language = language
        self.timeout = timeout
        self._health: Optional[tuple[float, bool]] = None    # (Zeitpunkt, erreichbar)

    @property
    def available(self) -> bool:
        return bool(self.url)

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.key} if self.key else {}

    async def reachable(self) -> bool:
        """Antwortet der Server? (gemerkt fuer HEALTH_TTL Sekunden)"""
        if not self.available:
            return False
        import time
        now = time.monotonic()
        if self._health is not None and now - self._health[0] < self.HEALTH_TTL:
            return self._health[1]
        ok = False
        try:
            timeout = aiohttp.ClientTimeout(total=self.HEALTH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(health_url(self.url), headers=self._headers()) as r:
                    ok = r.status < 500
        except Exception:  # noqa: BLE001
            ok = False
        self._health = (time.monotonic(), ok)
        return ok

    async def transcribe(self, pcm: np.ndarray, prompt: str = "") -> Optional[str]:
        """Transkript (gesaeubert) oder None; wirft nie. `prompt` hilft bei Namen ("DJ Qwen")."""
        if not self.available or pcm is None or len(pcm) == 0:
            return None
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes(pcm), filename="clip.wav", content_type="audio/wav")
        form.add_field("model", self.model)
        if self.language:
            form.add_field("language", self.language)
        if prompt:
            form.add_field("prompt", prompt)
        form.add_field("response_format", "json")
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.url + "/audio/transcriptions", data=form,
                                  headers=self._headers()) as r:
                    if r.status != 200:
                        log.warning("Spracherkennung antwortet %s: %s", r.status, (await r.text())[:200])
                        return None
                    raw = await r.text()
        except Exception as exc:  # noqa: BLE001
            log.warning("Spracherkennung fehlgeschlagen: %s", exc or type(exc).__name__)
            import time
            self._health = (time.monotonic(), False)       # sofort als weg merken
            return None
        text = raw
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if not segments_ok(data):
                    return None
                text = data.get("text") or ""
        except ValueError:
            pass
        text = stt_clean(text)
        if text and prompt_echo(text, prompt):
            return None
        return text
