"""Ohr: Mithoeren im Call (Pegel-Detektor, Spracherkennung, Ansprache, Einwurf)."""
import asyncio
import io
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from musikbot.ki import STT, health_url, segments_ok, stt_clean
from musikbot.ohr import REPLY_GAP_S, Ohr, Vad, addressed, name_tokens

SR = 16_000


def _tone(seconds: float, amp: float = 3000.0, f: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.int16)


def _noise(seconds: float, amp: float = 40.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(seconds * SR))).astype(np.int16)


def _feed_chunks(vad: Vad, sig: np.ndarray, chunk: int = 170) -> list[np.ndarray]:
    out = []
    for i in range(0, sig.size, chunk):                 # krumme Stueckgroesse: Rest wird mitgefuehrt
        out += vad.push(sig[i:i + chunk])
    return out


# ------------------------------------------------------------ Pegel-Detektor
def test_vad_cuts_one_sentence_and_ignores_silence_and_short_blips():
    sig = np.concatenate([_noise(1.0), _tone(1.5) + _noise(1.5, seed=1), _noise(1.5, seed=2),
                          _tone(0.3), _noise(1.5, seed=3)])
    segs = _feed_chunks(Vad(), sig)
    assert len(segs) == 1
    dur = segs[0].size / SR
    assert 1.4 <= dur <= 2.2, dur                       # Satz + Vorlauf + kurzer Ausklang
    assert segs[0].dtype == np.int16


def test_vad_silence_only_gives_nothing():
    assert _feed_chunks(Vad(), _noise(5.0, amp=120.0)) == []


def test_vad_force_cuts_long_talk_at_15_seconds():
    vad = Vad()
    segs = _feed_chunks(vad, _tone(20.0), chunk=1600)
    segs += _feed_chunks(vad, _noise(1.5))
    assert len(segs) == 2
    assert abs(segs[0].size / SR - 15.0) < 0.05
    assert 4.5 < segs[1].size / SR < 6.0


# ------------------------------------------------------------ Halluzinationen / Whisper-Segmente
@pytest.mark.parametrize("text", ["Untertitel im Auftrag des ZDF, 2020", "Vielen Dank.", "[Musik]",
                                  " ... ", "Untertitelung. BR 2018", "Copyright WDR 2021",
                                  "Vielen Dank fürs Zuschauen!", "www.example.de", "(Applaus)", "Ähm."])
def test_hallucinations_are_dropped(text):
    assert stt_clean(text) is None


def test_real_speech_is_kept_and_trimmed():
    assert stt_clean("  (Lachen) DJ Qwen, spiel mal was Schnelleres!  ") == "DJ Qwen, spiel mal was Schnelleres!"
    assert stt_clean("Danke, das war ein guter Song") == "Danke, das war ein guter Song"


def test_segments_check():
    ok = {"segments": [{"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 1.2}]}
    assert segments_ok(ok) and segments_ok({}) and segments_ok({"segments": []})
    assert not segments_ok({"segments": [{"no_speech_prob": 0.9, "avg_logprob": -0.2}]})
    assert not segments_ok({"segments": [{"no_speech_prob": 0.1, "avg_logprob": -1.4}]})
    assert not segments_ok({"segments": [{"compression_ratio": 2.8}]})
    mixed = {"segments": [{"no_speech_prob": 0.9}, {"no_speech_prob": 0.2}]}
    assert segments_ok(mixed)                           # nur wenn alle Segmente Stille sind


def test_health_url():
    assert health_url("http://whisper.lan:8000/v1") == "http://whisper.lan:8000/health"
    assert health_url("http://h:8000/v1/") == "http://h:8000/health"
    assert health_url("https://x.example/api") == "https://x.example/api/health"


# ------------------------------------------------------------ Ansprache
QWEN = name_tokens("DJ Qwen", "DJ Quenn")


@pytest.mark.parametrize("text", ["DJ Qwen, spiel mal was Schnelleres", "Hey Kwen, was läuft da?",
                                  "Quenn, wie heißt der Song?", "dj quen ist cool",
                                  "Didschei Gwen mach lauter", "D.J. Kwenn bist du da?",
                                  "Ich find den Beat gut. Qwen, was ist das?", "Na DJ Qwen?"])
def test_addressed_positive(text):
    assert addressed(text, QWEN)


@pytest.mark.parametrize("text", ["Gwendolyn kommt später", "ich hab Gwen gestern gesehen",
                                  "der DJ ist gut", "Queen ist eine Band", "", "Kwenzel ist da"])
def test_addressed_negative(text):
    assert not addressed(text, QWEN)


def test_addressed_generic_name():
    names = name_tokens("DJ Bär", "")
    assert addressed("DJ Bär, was geht?", names)
    assert addressed("Hey Bär!", names)
    assert not addressed("der Bär ist süß", names)


# ------------------------------------------------------------ Ohr mit Attrappen
class FakePlayer:
    def __init__(self, ok=True, idle=100.0):
        self.ok = ok
        self.idle = idle
        self.said: list[tuple[str, bool]] = []
        self.current = SimpleNamespace(title="Sandstorm", key="k")

    def _zuruf_ok(self, urgent=False):
        return self.ok

    @property
    def speech_idle(self):
        return self.idle

    @staticmethod
    def _info(track):
        return track.title, "Darude"

    async def shout(self, text, urgent=False):
        self.said.append((text, urgent))
        return True


class FakeKI:
    def __init__(self, answer="  „Hey Max! 🎧 Klar, das ist **Sandstorm**!“ ", delay=0.0):
        self.answer = answer
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def chat(self, messages, max_tokens=120, temperature=0.9):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answer


class FakeSTT:
    def __init__(self, text="Hallo zusammen", up=True):
        self.text = text
        self.up = up
        self.prompts: list[str] = []

    async def reachable(self):
        return self.up

    async def transcribe(self, pcm, prompt=""):
        self.prompts.append(prompt)
        return self.text


def _ohr(ki=None, stt=None, player=None, every=0.0):
    cfg = SimpleNamespace(dj_name="DJ Qwen", dj_name_spoken="DJ Quenn", ohr_every=every)
    return Ohr(cfg, ki or FakeKI(), stt or FakeSTT(), player or FakePlayer(),
               lambda ident: {"@max:x:D1": "Max", "@eva:x:D2": "Eva"}.get(ident, ""))


async def test_reply_when_addressed_is_cleaned_and_urgent():
    player, ki = FakePlayer(), FakeKI()
    ohr = _ohr(ki=ki, player=player)
    ohr._heard("@eva:x:D2", "Ich hab Hunger")
    ohr._heard("@max:x:D1", "DJ Qwen, was läuft da gerade?")
    await ohr._reply_task
    assert player.said == [("Hey Max! Klar, das ist Sandstorm!", True)]
    prompt = ki.calls[0][1]["content"]
    assert "Sandstorm" in prompt and "Eva: Ich hab Hunger" in prompt and "Max" in prompt
    assert "!skip" in ki.calls[0][0]["content"]          # weiss, dass er nichts per Sprache steuert


async def test_reply_rate_limit():
    ki = FakeKI(delay=0.05)
    ohr = _ohr(ki=ki)
    ohr._heard("@max:x:D1", "DJ Qwen, hallo")
    ohr._heard("@max:x:D1", "DJ Qwen, hörst du?")         # laeuft noch -> keine zweite Antwort
    await ohr._reply_task
    ohr._heard("@max:x:D1", "DJ Qwen, jetzt?")            # fertig, aber < 8 s -> auch nicht
    assert len(ki.calls) == 1
    ohr._last_reply -= REPLY_GAP_S + 1
    ohr._heard("@max:x:D1", "DJ Qwen, und jetzt?")
    await ohr._reply_task
    assert len(ki.calls) == 2


async def test_no_reply_when_not_addressed_or_empty_answer():
    player = FakePlayer()
    ohr = _ohr(ki=FakeKI(answer="-"), player=player)
    ohr._heard("@max:x:D1", "Gwendolyn kommt später")
    assert ohr._reply_task is None
    ohr._heard("@max:x:D1", "Hey Qwen!")
    assert await ohr._reply_task is False
    assert player.said == []


async def test_recent_and_forget():
    ohr = _ohr()
    ohr._heard("@max:x:D1", "eins")
    ohr._heard("@eva:x:D2", "zwei")
    ohr._heard("@max:x:D9", "drei")
    t0 = ohr._lines[0]
    ohr._lines[0] = (t0[0] - 500, *t0[1:])                 # zu alt fuer das Fenster
    assert ohr.recent(180) == [("Eva", "zwei"), ("max", "drei")]
    ohr.forget("@MAX:x")                                  # Nutzer-ID, alle Geraete, egal welche Schreibweise
    assert ohr.recent(10_000) == [("Eva", "zwei")]
    ohr.forget("@eva:x:D2")                               # oder genau eine Identity
    assert ohr.recent(10_000) == []


async def test_worker_transcribes_with_name_prompt_and_queue_is_bounded():
    stt = FakeSTT(text="Hallo zusammen")
    ohr = _ohr(stt=stt)
    for i in range(7):                                    # Arbeiter laeuft noch nicht: Stau
        ohr._enqueue("@max:x:D1", np.zeros(SR, dtype=np.int16))
    assert len(ohr._queue) == 4 and ohr.dropped == 3
    ohr.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(ohr.recent()) == 4:
                break
        assert ohr.ready
        assert ohr.recent() == [("Max", "Hallo zusammen")] * 4
        assert stt.prompts[0] == "DJ Qwen, Sandstorm"
    finally:
        ohr.stop()
    assert ohr.recent() == [] and not ohr._queue


async def test_feed_end_to_end_and_pause_while_stt_down():
    stt = FakeSTT(text="Hey Qwen, geiler Song")
    player = FakePlayer()
    ohr = _ohr(stt=stt, player=player)
    ohr.start()
    try:
        await asyncio.sleep(0.01)                         # Health-Check
        sig = np.concatenate([_noise(0.5), _tone(1.2), _noise(1.0)])
        for i in range(0, sig.size, 160):
            ohr.feed("@max:x:D1", sig[i:i + 160])
        for _ in range(100):
            await asyncio.sleep(0.01)
            if player.said:
                break
        assert player.said and player.said[0][1] is True
        # Server weg: nichts mehr sammeln, Stau geleert
        ohr._set_ready(False)
        assert not ohr.ready
        for i in range(0, sig.size, 160):
            ohr.feed("@eva:x:D2", sig[i:i + 160])
        assert not ohr._queue and "@eva:x:D2" not in ohr._vads
    finally:
        ohr.stop()


async def test_not_ready_until_health_check_says_so():
    ohr = _ohr(stt=FakeSTT(up=False))
    ohr.start()
    try:
        await asyncio.sleep(0.01)
        assert not ohr.ready
        ohr.feed("@max:x:D1", _tone(2.0))
        assert not ohr._vads
    finally:
        ohr.stop()


async def test_chime_needs_conversation_and_quiet_moment():
    player, ki = FakePlayer(idle=100.0), FakeKI(answer="Ihr redet ja mehr als ich auflege!")
    ohr = _ohr(ki=ki, player=player, every=240)
    ohr._heard("@max:x:D1", "Wer kommt morgen mit?")
    assert await ohr.chime_once() is False                # nur eine Zeile
    ohr._heard("@eva:x:D2", "Ich vielleicht")
    player.idle = 10.0
    assert await ohr.chime_once() is False                # gerade erst geredet
    player.idle = 100.0
    assert await ohr.chime_once() is True
    assert player.said == [("Ihr redet ja mehr als ich auflege!", False)]
    assert 168 <= ohr.chime_gap() <= 312


async def test_chime_off_when_every_is_zero():
    ohr = _ohr(every=0)
    ohr.start()
    try:
        await asyncio.sleep(0.01)
        chime = [t for t in ohr._tasks if t.get_name() == "ohr-chime"][0]
        assert chime.done()
    finally:
        ohr.stop()


# ------------------------------------------------------------ STT gegen einen echten kleinen Server
async def test_stt_multipart_prompt_and_health():
    seen = {}

    async def transcribe(request):
        form = await request.post()
        seen["auth"] = request.headers.get("Authorization")
        seen["fields"] = {k: form[k] for k in ("model", "language", "prompt", "response_format")}
        f = form["file"]
        with wave.open(io.BytesIO(f.file.read())) as w:
            seen["wav"] = (w.getnchannels(), w.getframerate(), w.getnframes())
        return web.json_response({"text": " Hallo DJ Qwen ", "segments": [
            {"no_speech_prob": 0.05, "avg_logprob": -0.2, "compression_ratio": 1.1}]})

    async def health(request):
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", transcribe)
    app.router.add_get("/health", health)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://127.0.0.1:{server.port}/v1"
        stt = STT(url, "geheim", "whisper-large-v3-turbo", "de")
        assert await stt.reachable()
        text = await stt.transcribe(_tone(0.5), prompt="DJ Qwen")
        assert text == "Hallo DJ Qwen"
        assert seen["auth"] == "Bearer geheim"
        assert seen["fields"] == {"model": "whisper-large-v3-turbo", "language": "de",
                                  "prompt": "DJ Qwen", "response_format": "json"}
        assert seen["wav"] == (1, 16000, 8000)
    finally:
        await server.close()


async def test_stt_failures_return_none_and_mark_unreachable():
    async def bad(request):
        return web.Response(status=500, text="kaputt")

    async def silence(request):
        return web.json_response({"text": " Untertitel im Auftrag des ZDF ", "segments": []})

    async def nospeech(request):
        return web.json_response({"text": "Tschüss zusammen", "segments": [{"no_speech_prob": 0.95}]})

    for handler, expect_up in ((bad, True), (silence, True), (nospeech, True)):
        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            stt = STT(f"http://127.0.0.1:{server.port}/v1")
            assert await stt.transcribe(_tone(0.3)) is None
        finally:
            await server.close()
    # Server gar nicht da: None, und sofort als unerreichbar gemerkt (ohne neuen Health-Check)
    stt = STT("http://127.0.0.1:9/v1", timeout=2.0)
    assert await stt.transcribe(_tone(0.3)) is None
    assert await stt.reachable() is False
    assert STT("").available is False and await STT("").reachable() is False
