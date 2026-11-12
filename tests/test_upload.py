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


def test_voice_messages_are_not_music_requests():
    from musikbot.matrix import is_voice_message

    voice = {"msgtype": "m.audio", "body": "Voice message", "url": "mxc://x/y",
             "org.matrix.msc3245.voice": {},
             "org.matrix.msc1767.audio": {"duration": 3000, "waveform": [1, 2, 3]}}
    song = {"msgtype": "m.audio", "body": "song.mp3", "url": "mxc://x/z",
            "info": {"mimetype": "audio/mpeg"}}
    assert is_voice_message(voice)
    assert not is_voice_message(song)
    assert not is_voice_message({})
