"""Baer-DJ (Kamerabild ohne Video) und Ansage."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

import numpy as np
import pytest

av = pytest.importorskip("av")
pytest.importorskip("PIL")

from musikbot.ansage import spoken_title, text_for, to_stereo48k  # noqa: E402
from musikbot.baer import BaerDJ, Beat  # noqa: E402
from musikbot.config import Config  # noqa: E402
from musikbot.hub import Hub  # noqa: E402
from musikbot.player import DUCK, Player  # noqa: E402
from musikbot.store import TrackStore  # noqa: E402

SR = 48000


def _kick_track(bpm: float, seconds: float):
    """20-ms-Bloecke: kurzer 60-Hz-Schlag auf jedem Beat, dazwischen leises Rauschen."""
    rng = np.random.default_rng(0)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    beat = 60.0 / bpm
    env = np.exp(-((t % beat) / 0.05))
    sig = 0.8 * env * np.sin(2 * np.pi * 60 * t) + 0.01 * rng.standard_normal(n)
    pcm = (np.clip(sig, -1, 1) * 30000).astype(np.int16)
    for i in range(0, n - 960, 960):
        yield i / SR, np.repeat(pcm[i:i + 960], 2).reshape(1, -1)


def test_beat_finds_tempo_and_kicks():
    beat = Beat()
    for at, block in _kick_track(120, 12):
        beat.feed(block, 100.0 + at)
    st = beat.state(112.0)
    assert st["bpm"] == pytest.approx(120, abs=4)
    assert len(beat.kicks) >= 10
    assert 0.0 <= st["phase"] < 1.0
    assert beat.state(110.02)["loud"] > 0.2              # direkt nach einem Kick laut
    assert beat.state(200.0)["bass"] == 0.0          # lange nichts gehoert: Stille


def test_render_frame_size_and_announcement():
    baer = BaerDJ(426, 240)
    for at, block in _kick_track(128, 4):
        baer.beat.feed(block, at)
    cover = av.VideoFrame.from_ndarray(np.full((36, 64, 3), 200, np.uint8), format="rgb24")
    baer.set_track("Ein sehr langer Titel, der niemals in die Sprechblase passt, ehrlich", "Jemand", cover)
    voice = np.full((1, SR * 2), 3000, np.int16)
    baer.announce(1.0, voice)
    for now in (0.5, 1.5, 2.5, 3.9):
        frame = baer.render(now)
        assert (frame.width, frame.height) == (426, 240)
    assert baer._voice(1.5) > 0 and baer._voice(5.0) == 0


def test_spoken_title_cleans_youtube_noise():
    assert spoken_title("Daft Punk - One More Time (Official Video)", "Daft Punk - Topic") == \
        ("One More Time", "Daft Punk")
    assert spoken_title("Song [Lyrics] | 4K", "Kanal") == ("Song", "Kanal")
    assert spoken_title("", "") == ("der nächste Titel", "")
    assert text_for("Voll", "suehnung") == "Jetzt kommt: Voll von suehnung!"


def test_to_stereo48k_pads_and_resamples():
    mono = np.full(22050, 1000, np.int16)                 # 1 s bei 22,05 kHz
    out = to_stereo48k(mono, 22050)
    assert out.dtype == np.int16 and out.shape[0] == 1
    assert out.shape[1] / 2 / SR == pytest.approx(1.0 + 0.3 + 0.5, abs=0.05)


def _player(**kw) -> Player:
    d = tempfile.mkdtemp()
    cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir=d,
                 matrix_user_id="@b:x", ansage_voice="", **kw)
    return Player(cfg, Hub(), TrackStore(d + "/db", d + "/tmp"))


def test_mix_voice_ducks_music_and_ends():
    p = _player()
    p._voice = np.full((1, 960 * 2 * 50), 1000, np.int16)      # 1 s Stimme
    p._voice_i = 0
    music = np.full((1, 960 * 2), 10000, np.int16)
    out = [p._mix_voice(music.copy()) for _ in range(50)]
    mid = out[25]
    assert int(mid[0, 0]) == pytest.approx(10000 * DUCK + 1000, abs=20)
    assert p._voice is None                                     # danach wieder normal
    assert int(out[0][0, 0]) == pytest.approx(10000 + 1000, abs=50)   # weicher Einstieg


def test_baer_replaces_cover_and_streams_frames():
    asyncio.run(_baer_run())


async def _baer_run():
    p = _player(video_height=144, video_fps=20)
    assert p.baer is not None and p.baer.w == 256
    sink = p.hub.new_sink()
    p.current = SimpleNamespace(key="k1", title="Titel", uploader="")
    p._source = SimpleNamespace(has_video=False)
    p.set_cover("k1", None)
    assert sink.take_video() is None                            # kein Titelbild, der Baer kommt
    task = asyncio.create_task(p._baer_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    frame = sink.take_video()
    assert frame is not None and (frame.width, frame.height) == (256, 144)
    p._source = SimpleNamespace(has_video=True)                 # echtes Video: Baer schweigt
    assert not p._baer_active()


def test_without_video_or_disabled_no_baer():
    assert _player(baer_enabled=False).baer is None
    p = _player(enable_video=False)
    p.hub.new_sink()
    p._source = SimpleNamespace(has_video=False)
    assert not p._baer_active()


@pytest.mark.skipif(not os.path.exists(os.environ.get("ANSAGE_TEST_VOICE", "/nope")),
                    reason="Piper-Stimme fuer den Test fehlt (ANSAGE_TEST_VOICE)")
def test_piper_synth_real_voice():
    from musikbot.ansage import Ansager
    a = Ansager(os.environ["ANSAGE_TEST_VOICE"])
    out = a.synth(text_for("Voll", "suehnung"))
    assert out is not None and out.shape[1] / 2 / SR > 1.0
    assert np.abs(out).max() > 1000


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg fehlt")
def test_embedded_cover_is_still_not_video(tmp_path):
    import subprocess
    from musikbot.media import MediaSource
    pic, mp3 = tmp_path / "c.png", tmp_path / "s.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=300x300", "-frames:v", "1",
                    str(pic)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", "-i", str(pic),
                    "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                    "-c:a", "libmp3lame", str(mp3)], check=True)
    src = MediaSource(str(mp3), video_height=144)
    src.open()
    try:
        assert not src.has_video
        assert src.still is not None and (src.still.width, src.still.height) == (256, 144)
        assert sum(b.shape[1] for b in src.audio_blocks()) > 0
    finally:
        src.close()
