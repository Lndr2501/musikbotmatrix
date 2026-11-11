"""Chat-Anhaenge: Dauer-Pruefung und Abspielen."""
import wave

import pytest

from musikbot.bot import probe_duration


def _wav(path, seconds=1, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)


def test_probe_duration_wav(tmp_path):
    f = tmp_path / "a.wav"
    _wav(f, 2)
    assert probe_duration(str(f)) == pytest.approx(2, abs=0.1)


def test_probe_rejects_non_media(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"das ist kein audio")
    with pytest.raises(Exception):
        probe_duration(str(f))
