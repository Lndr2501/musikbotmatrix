"""Eigene Spracherkennung neben dem Bot: Whisper (faster-whisper) auf der CPU, OpenAI-kompatibel.

Start: ``python -m musikbot.stt_server`` (im selben Image, eigener Container, eigenes Speicherlimit).
Der Bot spricht ihn wie jeden anderen Whisper-Server an (``STT_URL=http://127.0.0.1:8097/v1``):

- ``POST /v1/audio/transcriptions`` (multipart: file, model, language, prompt) -> JSON
  ``{"text", "language", "segments": [{start, end, text, no_speech_prob, avg_logprob, compression_ratio}]}``
- ``GET /health`` -> 200, sobald das Modell geladen ist (vorher 503)

Nichts wird gespeichert; Anfragen laufen nacheinander (eine CPU-Last, keine Warteschlangen-Explosion).

Umgebung: STT_PORT (8097), STT_HOST (127.0.0.1), STT_LOCAL_MODEL (small), STT_THREADS (4),
STT_COMPUTE (int8), STT_MODELS_DIR (/data/stt-models), STT_SERVER_KEY (leer = ohne Schluessel).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import wave
from typing import Callable, Optional

import numpy as np
from aiohttp import web

log = logging.getLogger("musikbot.stt_server")

RATE = 16_000
MAX_BYTES = 8 * 1024 * 1024          # ~4 min WAV bei 16 kHz – mehr schickt das Ohr nie

# transcribe(pcm_float32_16k, language, prompt) -> (text, language, segments[dict])
Transcriber = Callable[[np.ndarray, str, str], tuple[str, str, list[dict]]]


def read_audio(data: bytes) -> np.ndarray:
    """Audiodatei -> float32 mono 16 kHz. WAV (16 bit) direkt, alles andere ueber PyAV."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() == 2:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                ch, rate = w.getnchannels(), w.getframerate()
                if ch > 1:
                    pcm = pcm.reshape(-1, ch).mean(axis=1)
                audio = pcm.astype(np.float32) / 32768.0
                if rate != RATE and len(audio):
                    n = int(round(len(audio) * RATE / rate))
                    audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
                return audio.astype(np.float32)
    except (wave.Error, EOFError):
        pass
    from faster_whisper.audio import decode_audio          # PyAV: mp3/ogg/webm/...
    return decode_audio(io.BytesIO(data), sampling_rate=RATE)


def whisper_transcriber(model: str, threads: int, compute: str, models_dir: str) -> Transcriber:
    from faster_whisper import WhisperModel
    t = time.monotonic()
    wm = WhisperModel(model, device="cpu", compute_type=compute, cpu_threads=threads,
                      download_root=models_dir or None)
    log.info("Whisper %s geladen (%.0f s, %d Threads, %s)", model, time.monotonic() - t, threads, compute)

    def run(audio: np.ndarray, language: str, prompt: str):
        segs, info = wm.transcribe(audio, language=language or None, initial_prompt=prompt or None,
                                   beam_size=1, vad_filter=False, condition_on_previous_text=False)
        out = [{"start": s.start, "end": s.end, "text": s.text, "no_speech_prob": s.no_speech_prob,
                "avg_logprob": s.avg_logprob, "compression_ratio": s.compression_ratio} for s in segs]
        return " ".join(s["text"].strip() for s in out).strip(), info.language, out
    return run


class SttServer:
    def __init__(self, key: str = "") -> None:
        self.key = key
        self.transcribe: Optional[Transcriber] = None
        self._lock = asyncio.Lock()
        self.served = 0

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BYTES)
        app.router.add_get("/health", self.health)
        app.router.add_post("/v1/audio/transcriptions", self.transcriptions)
        app.router.add_post("/audio/transcriptions", self.transcriptions)
        return app

    async def health(self, _req: web.Request) -> web.Response:
        if self.transcribe is None:
            return web.json_response({"status": "loading"}, status=503)
        return web.json_response({"status": "ok", "served": self.served})

    async def transcriptions(self, req: web.Request) -> web.Response:
        if self.key and req.headers.get("Authorization", "") != "Bearer " + self.key:
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.transcribe is None:
            return web.json_response({"error": "model loading"}, status=503)
        fields: dict[str, str] = {}
        data = b""
        try:
            reader = await req.multipart()
            async for part in reader:
                if part.name == "file":
                    data = bytes(await part.read(decode=False))
                elif part.name:
                    fields[part.name] = (await part.text()).strip()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "multipart expected"}, status=400)
        if not data:
            return web.json_response({"error": "file missing"}, status=400)
        try:
            audio = await asyncio.to_thread(read_audio, data)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "unreadable audio"}, status=400)
        async with self._lock:                       # eine Erkennung nach der anderen
            t = time.monotonic()
            text, lang, segs = await asyncio.to_thread(
                self.transcribe, audio, fields.get("language", ""), fields.get("prompt", ""))
        self.served += 1
        log.debug("%.1f s Audio in %.2f s erkannt", len(audio) / RATE, time.monotonic() - t)
        return web.json_response({"text": text, "language": lang, "duration": len(audio) / RATE,
                                  "segments": segs})


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    host = os.environ.get("STT_HOST", "127.0.0.1")
    port = int(os.environ.get("STT_PORT", "8097"))
    srv = SttServer(os.environ.get("STT_SERVER_KEY", "").strip())
    runner = web.AppRunner(srv.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Spracherkennung auf http://%s:%d/v1 (laedt Modell …)", host, port)
    srv.transcribe = await asyncio.to_thread(
        whisper_transcriber, os.environ.get("STT_LOCAL_MODEL", "small"),
        int(os.environ.get("STT_THREADS", "4")), os.environ.get("STT_COMPUTE", "int8"),
        os.environ.get("STT_MODELS_DIR", "/data/stt-models"))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
