"""MediaSource: dekodiert einen mit PyAV erzeugten Clip (Audio + Video)."""
import math

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
