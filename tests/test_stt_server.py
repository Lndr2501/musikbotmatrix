"""Eigene Spracherkennung: Server-Schnittstelle mit Attrappe statt Whisper, Bot-Client dagegen."""
from __future__ import annotations

import asyncio
import io
import wave

import numpy as np
from aiohttp import web

from musikbot.ki import STT, wav_bytes
from musikbot.stt_server import SttServer, read_audio


def fake(seen):
    def run(audio, language, prompt):
        seen.append((len(audio), language, prompt))
        return "Hallo DJ Qwen", language or "de", [
            {"start": 0.0, "end": 1.0, "text": " Hallo DJ Qwen", "no_speech_prob": 0.01,
             "avg_logprob": -0.2, "compression_ratio": 1.1}]
    return run


async def _serve(srv):
    runner = web.AppRunner(srv.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


async def test_bot_client_talks_to_own_server_and_health_waits_for_model():
    srv = SttServer()
    runner, url = await _serve(srv)
    try:
        stt = STT(url, model="small")
        assert not await stt.reachable()              # Modell laedt noch: 503
        seen = []
        srv.transcribe = fake(seen)
        stt._health = None
        assert await stt.reachable()
        pcm = (np.sin(np.arange(16000) * 0.2) * 5000).astype(np.int16)
        assert await stt.transcribe(pcm, prompt="DJ Qwen") == "Hallo DJ Qwen"
        assert seen == [(16000, "de", "DJ Qwen")]
    finally:
        await runner.cleanup()


async def test_key_is_checked_when_set():
    srv = SttServer(key="geheim")
    srv.transcribe = fake([])
    runner, url = await _serve(srv)
    try:
        pcm = np.zeros(1600, np.int16) + 100
        assert await STT(url, key="falsch").transcribe(pcm) is None
        assert await STT(url, key="geheim").transcribe(pcm) == "Hallo DJ Qwen"
    finally:
        await runner.cleanup()


async def test_requests_run_one_after_another():
    srv = SttServer()
    active, peak = [0], [0]

    def slow(audio, language, prompt):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.05)
        active[0] -= 1
        return "eins", "de", []
    srv.transcribe = slow
    runner, url = await _serve(srv)
    try:
        stt = STT(url)
        pcm = np.zeros(1600, np.int16) + 100
        res = await asyncio.gather(*(stt.transcribe(pcm) for _ in range(4)))
        assert res == ["eins"] * 4 and peak[0] == 1
    finally:
        await runner.cleanup()


def test_read_audio_handles_rates_and_stereo():
    mono = read_audio(wav_bytes(np.full(16000, 16384, np.int16)))
    assert mono.dtype == np.float32 and len(mono) == 16000 and abs(mono.mean() - 0.5) < 1e-3
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(np.full(48000 * 2, 8192, np.int16).tobytes())
    st = read_audio(buf.getvalue())
    assert len(st) == 16000 and abs(st.mean() - 0.25) < 1e-3
