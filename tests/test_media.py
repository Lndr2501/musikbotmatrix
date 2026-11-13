"""MediaSource: dekodiert einen mit PyAV erzeugten Clip (Audio + Video)."""
import numpy as np
import pytest

av = pytest.importorskip("av")

from musikbot.media import MediaSource, silence_block, apply_gain  # noqa: E402

SR = 48000


def _make_clip(path: str, seconds: float = 2.0) -> None:
    container = av.open(path, mode="w")
    try:
        vstream = container.add_stream("libx264", rate=25)
    except Exception:  # kein x264 -> offenbar fehlender Codec
        try:
            vstream = container.add_stream("mpeg4", rate=25)
        except Exception:
            vstream = None
    astream = container.add_stream("aac", rate=SR)
    if vstream:
        vstream.width = 320
        vstream.height = 240
        vstream.pix_fmt = "yuv420p"
    nframes = int(seconds * 25)
    for i in range(nframes):
        if vstream:
            arr = np.zeros((240, 320, 3), dtype=np.uint8)
            arr[:, :, 0] = (i * 3) % 255
            vf = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for pkt in vstream.encode(vf):
                container.mux(pkt)
        if i % 25 == 0:  # 1s Audio in Blöcken
            block = np.empty((1, SR // 4 * 2), dtype=np.int16)
            t = np.arange(SR // 4) / SR
            val = (9000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
            block[0, 0::2] = val
            block[0, 1::2] = val
            af = av.AudioFrame.from_ndarray(block, format="s16", layout="stereo")
            af.sample_rate = SR
            for pkt in astream.encode(af):
                container.mux(pkt)
    for pkt in (vstream.encode() if vstream else []):
        container.mux(pkt)
    for pkt in astream.encode():
        container.mux(pkt)
    container.close()


def test_media_roundtrip(tmp_path):
    path = str(tmp_path / "clip.mp4")
    _make_clip(path)
    src = MediaSource(path, video_height=240, video_fps=25.0)
    src.open()
    try:
        assert src.duration > 1.5
        assert src.has_video
        blocks = []
        for block in src.audio_blocks():
            blocks.append(block)
            if len(blocks) >= 5:
                break
        assert blocks and blocks[0].shape[-1] == 960 * 2
        frames = []
        for when, frame in src.video_frames():
            frames.append((when, frame))
            if len(frames) >= 3:
                break
        assert len(frames) == 3
        assert frames[0][1].height <= 240
    finally:
        src.close()


def test_gain_and_silence():
    block = np.full((1, 960 * 2), 8000, dtype=np.int16)
    out = apply_gain(block, 0.5)
    assert abs(abs(out[0, 0]) - 4000) <= 2
    sil = silence_block()
    assert int(np.max(np.abs(sil))) == 0


def test_split_search_prefixes():
    from musikbot.ytdl import split_search
    assert split_search("never gonna") == ("ytsearch1", "never gonna")
    assert split_search("sc: daft punk") == ("scsearch1", "daft punk")
    assert split_search("Bili:foo") == ("bilisearch1", "foo")
    assert split_search("Re: Zero opening") == ("ytsearch1", "Re: Zero opening")


def test_seek_moves_forward_and_back_without_errors(tmp_path):
    """Regression: seek() scheiterte mit PyAV 17 (any_frame statt any, av.Error gibt es nicht)."""
    path = str(tmp_path / "clip.mp4")
    _make_clip(path, seconds=3.0)
    src = MediaSource(path, video_height=240, video_fps=25.0)
    src.open()
    try:
        for target in (1.5, 0.0, 2.2):
            src.seek(target)
            block = next(iter(src.audio_blocks(target)))
            assert block.shape[-1] == 960 * 2
    finally:
        src.close()


def _jpeg(w, h):
    import io

    import av
    import numpy as np
    buf = io.BytesIO()
    with av.open(buf, "w", format="mjpeg") as out:
        st = out.add_stream("mjpeg", rate=1)
        st.width, st.height, st.pix_fmt = w, h, "yuvj420p"
        frame = av.VideoFrame.from_ndarray(np.full((h, w, 3), 200, np.uint8), format="rgb24")
        for pkt in st.encode(frame.reformat(format="yuvj420p")):
            out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    return buf.getvalue()


def test_cover_frame_is_centered_with_black_bars_and_never_stretched():
    from musikbot.media import cover_frame
    f = cover_frame(_jpeg(120, 120), 64, 36)
    assert (f.width, f.height) == (64, 36)
    a = f.to_ndarray(format="rgb24")
    assert a[18, 32].min() > 150 and a[18, 2].max() == 0       # Mitte hell, linker Rand schwarz
    assert cover_frame(b"kein bild", 64, 36) is None


def test_player_shows_cover_only_for_tracks_without_video():
    from types import SimpleNamespace
    from musikbot.config import Config
    from musikbot.player import Player
    from musikbot.store import TrackStore
    import tempfile
    d = tempfile.mkdtemp()
    cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir=d, matrix_user_id="@b:x")
    from musikbot.hub import Hub
    p = Player(cfg, Hub(), TrackStore(d + "/db", d + "/tmp"))
    p.current = SimpleNamespace(key="k1")
    p._source = SimpleNamespace(has_video=False)
    p.video_enabled = True
    from musikbot.media import cover_frame
    p.set_cover("k1", cover_frame(_jpeg(64, 64), 64, 36))
    sink = p.hub.new_sink()
    p.set_cover("k1", cover_frame(_jpeg(64, 64), 64, 36))
    assert sink.take_video() is not None and p.cover_frame is not None
    p.current = SimpleNamespace(key="k2")                       # anderer Titel: altes Bild gilt nicht
    assert p.cover_frame is None
    p._source = SimpleNamespace(has_video=True)                 # echtes Video: kein Titelbild
    p.set_cover("k2", cover_frame(_jpeg(64, 64), 64, 36))
    assert sink.take_video() is None


def test_pick_lyric_entry_requires_matching_song():
    from musikbot.ytdl import pick_lyric_entry
    # Echter Fall: Suche nach Pashanim lieferte ein SXTN-Lyric-Video -> darf nicht genommen werden
    wrong = [{"title": "SXTN - Von Party zu Party (lyrics)", "channel": "Melerah"}]
    assert pick_lyric_entry(wrong, "mittelmeer pashanim goth remix") is None
    good = wrong + [{"title": "Pashanim - Mittelmeer (Goth Remix) [Lyrics]", "channel": "x"}]
    assert pick_lyric_entry(good, "mittelmeer pashanim goth remix")["title"].startswith("Pashanim")


def test_pick_lyric_entry_prefers_lyric_titles():
    from musikbot.ytdl import pick_lyric_entry
    entries = [
        {"title": "Song (Official Video)"},
        None,
        {"title": "Song (Lyric Video)"},
        {"title": "Song - Lyrics"},
    ]
    assert pick_lyric_entry(entries, "song")["title"] == "Song (Lyric Video)"
    assert pick_lyric_entry([{"title": "Song (Official Video)"}], "song") is None
    assert pick_lyric_entry([], "song") is None


def test_choose_entry_prefers_soundcloud_remix_over_youtube_original():
    from musikbot.ytdl import choose_entry
    yt = [{"title": "Pashanim - Mittelmeer", "channel": "Pashanim Nulldreinull"}]
    sc = [{"title": "Mittelmeer (Goth Remix)", "uploader": "fantemix"}]
    got = choose_entry(yt, [], sc, "mittelmeer pashanim goth remix")
    assert got["title"] == "Mittelmeer (Goth Remix)"


def test_choose_entry_prefers_original_and_lyrics_without_remix_in_query():
    from musikbot.ytdl import choose_entry
    yt = [{"title": "Pashanim - Mittelmeer", "channel": "Pashanim"}]
    ly = [{"title": "Pashanim - Mittelmeer (Lyrics)", "channel": "x"}]
    sc = [{"title": "Mittelmeer (Goth Remix)", "uploader": "fantemix"}]
    assert choose_entry(yt, ly, sc, "pashanim mittelmeer")["title"] == "Pashanim - Mittelmeer (Lyrics)"
    assert choose_entry(yt, [], sc, "pashanim mittelmeer")["title"] == "Pashanim - Mittelmeer"


def test_choose_entry_falls_back_to_first_youtube_hit():
    from musikbot.ytdl import choose_entry
    yt = [{"title": "Etwas ganz anderes"}]
    assert choose_entry(yt, [], [{"title": "auch nicht"}], "xyz qwe")["title"] == "Etwas ganz anderes"


def test_close_while_decoding_in_other_threads_never_crashes(tmp_path):
    """stop()/seek() schliessen den Container, waehrend die Wiedergabe im Thread dekodiert.
    Ohne Sperre stuerzt FFmpeg dabei ab (Segfault, Exit 139)."""
    import threading

    from musikbot.media import MediaSource

    path = str(tmp_path / "clip.mkv")
    _make_clip(path, seconds=4.0)
    for _ in range(20):
        src = MediaSource(path)
        src.open()
        audio, video = src.audio_blocks(0.0), src.video_frames(0.0)
        got = []

        def decode(it):
            for item in it:
                got.append(item)

        threads = [threading.Thread(target=decode, args=(it,)) for it in (audio, video)]
        for t in threads:
            t.start()
        src.seek(1.0)
        src.close()
        for t in threads:
            t.join(5)
            assert not t.is_alive()
    assert got
