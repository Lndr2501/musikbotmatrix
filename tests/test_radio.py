"""Radio-Ordner: !24/7 radio spielt zufaellig eigene Dateien, loescht aber nie etwas."""
import asyncio
import os

from musikbot.bot import Bot
from musikbot.config import Config


def _bot(tmp_path):
    radio = tmp_path / "radio"
    radio.mkdir()
    cfg = Config(homeserver="http://h", user="@bot:x", password="p", data_dir=str(tmp_path / "d"),
                 radio_dir=str(radio), matrix_user_id="@bot:x")
    return Bot(cfg), radio


def test_scan_picks_audio_only_and_picks_up_new_files(tmp_path):
    bot, radio = _bot(tmp_path)
    (radio / "a.mp3").write_bytes(b"x")
    (radio / "sub").mkdir()
    (radio / "sub" / "b.FLAC").write_bytes(b"x")
    (radio / "notes.txt").write_text("x")
    (radio / ".hidden.mp3").write_bytes(b"x")
    keys = bot._radio_scan()
    assert len(keys) == 2 and all(k.startswith("radio:") for k in keys)
    assert {bot.store.get(k).title for k in keys} == {"a", "b"}
    (radio / "c.ogg").write_bytes(b"x")
    assert len(bot._radio_scan()) == 3


def test_shuffle_never_repeats_last_first(tmp_path):
    bot, radio = _bot(tmp_path)
    for n in "abc":
        (radio / f"{n}.mp3").write_bytes(b"x")
    last = bot._radio_scan()[0]
    assert all(bot._radio_shuffled(last)[0] != last for _ in range(30))


def test_radio_files_are_never_deleted(tmp_path):
    bot, radio = _bot(tmp_path)
    f = radio / "a.mp3"
    f.write_bytes(b"x")
    key = bot._radio_scan()[0]
    bot.store.set_file(key, str(f))
    bot.store.release_file(key)
    assert os.path.exists(f)


def test_fetch_radio_sets_file_and_rejects_missing(tmp_path):
    bot, radio = _bot(tmp_path)
    f = radio / "a.mp3"
    f.write_bytes(b"x")
    key = bot._radio_scan()[0]
    track = bot.store.get(key)
    track.duration = 1.0                                   # kein Probe noetig
    asyncio.run(bot._fetch(track))
    assert track.playable
    os.remove(f)
    bot.store.release_file(key)
    try:
        asyncio.run(bot._fetch(track))
    except Exception as exc:
        assert "nicht mehr" in str(exc)
    else:
        raise AssertionError("fehlende Datei muss abgelehnt werden")
