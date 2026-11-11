"""Crossfade zwischen Titeln und kurzes Ausblenden bei Skip/Stop."""
import asyncio
import tempfile

import numpy as np
import pytest

av = pytest.importorskip("av")

from musikbot.config import Config  # noqa: E402
from musikbot.hub import Hub, Sink  # noqa: E402
from musikbot.media import crossfade_mix  # noqa: E402
from musikbot.player import Player  # noqa: E402
from musikbot.store import Track, TrackStore  # noqa: E402

SR = 48000


def _block(value: int, n: int = 960) -> np.ndarray:
    return np.full((1, n * 2), value, dtype=np.int16)


def test_crossfade_mix_is_equal_power():
    old, new = _block(10000), _block(10000)
    start = crossfade_mix(old, new, 0.0, 0.0)
    end = crossfade_mix(old, new, 1.0, 1.0)
    mid = crossfade_mix(None, new, 0.5, 0.5)
    assert abs(int(start[0, 0]) - 10000) <= 1          # Anfang: nur alter Titel (voll)
    assert abs(int(end[0, -1]) - 10000) <= 1           # Ende: nur neuer Titel (voll)
    assert abs(int(mid[0, 0]) - 7071) <= 2             # Mitte: sin(45°) – Equal Power
    short = crossfade_mix(_block(5000, 100), new, 0.0, 0.0)   # kuerzerer alter Block
    assert short.shape == new.shape and short[0, -1] == 0


def test_sink_fade_out_ramps_down_and_drops_rest():
    sink = Sink()
    for _ in range(40):
        sink.push_audio(_block(20000))
    kept = sink.fade_out(0.3)
    assert kept == pytest.approx(0.3)
    blocks = list(sink.audio)
    assert len(blocks) == 15
    levels = [int(np.abs(b).max()) for b in blocks]
    assert levels[0] >= 19000 and levels[-1] < 1500
    assert levels == sorted(levels, reverse=True)
    assert Sink().fade_out(0.3) == 0.0                   # leerer Puffer: nichts zu tun


def _tone(path: str, freq: float, seconds: float) -> None:
    with av.open(path, mode="w") as box:
        stream = box.add_stream("aac", rate=SR)
        step = 1024
        for start in range(0, int(seconds * SR), step):
            t = (start + np.arange(step)) / SR
            val = (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
            arr = np.repeat(val, 2).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="stereo")
            frame.sample_rate = SR
            frame.pts = start
            for pkt in stream.encode(frame):
                box.mux(pkt)
        for pkt in stream.encode():
            box.mux(pkt)


def test_queue_advance_crossfades_without_gap_and_uses_prefetch():
    asyncio.run(_crossfade_run())


async def _crossfade_run():
    d = tempfile.mkdtemp()
    a, b = f"{d}/a.m4a", f"{d}/b.m4a"
    _tone(a, 300, 2.0)
    _tone(b, 900, 2.0)
    cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir=d,
                 matrix_user_id="@b:x", enable_video=False, crossfade_seconds=0.5)
    store = TrackStore(d + "/db", d + "/tmp")
    for key in ("a", "b"):
        store._tracks[key] = Track(key=key, url="", title=key)
    paths = {"a": a, "b": b}
    fetched: list[tuple[str, bool]] = []

    async def fetch(track, quiet=False):
        fetched.append((track.key, quiet))
        store.set_file(track.key, paths[track.key])

    events: list[str] = []

    async def on_event(name, data):
        events.append(name)

    store.release_file = lambda key: None             # Testdateien nicht loeschen
    player = Player(cfg, Hub(), store, on_event=on_event, fetch=fetch)
    pushed: list[np.ndarray] = []
    player.hub.push_audio = pushed.append
    await player.play(store.get("a"))
    player.enqueue("b")
    for _ in range(100):
        await asyncio.sleep(0.1)
        if "queue-empty" in events:
            break
    await player.shutdown()

    assert ("b", True) in fetched and ("b", False) not in fetched   # vorgeladen, nicht doppelt
    assert events.index("track-end") < events.index("advanced") < events.index("queue-empty")
    seconds = sum(blk.shape[1] // 2 for blk in pushed) / SR
    assert 3.2 < seconds < 3.8          # 2 s + 2 s - 0,5 s Ueberblendung (+ AAC-Rand)
    rms = [float(np.sqrt(np.mean(blk.astype(np.float32) ** 2))) for blk in pushed]
    middle = rms[10:-10]                # ohne AAC-Anlauf/-Auslauf
    assert min(middle) > 2000           # kein Loch beim Uebergang
