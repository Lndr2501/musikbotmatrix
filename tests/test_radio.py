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


def test_safe_radio_name():
    from musikbot.bot import safe_radio_name
    assert safe_radio_name("../../etc/passwd.MP3") == "passwd.mp3"
    assert safe_radio_name("C:\\Users\\x\\Song: Teil 1?.mp3") == "Song Teil 1.mp3"
    assert safe_radio_name("..hidden.mp3") == "hidden.mp3"
    assert safe_radio_name("") == "upload"


def test_dashboard_upload_adds_files_never_overwrites(tmp_path):
    import io
    import wave

    import pytest
    pytest.importorskip("aiohttp")
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer

    from musikbot.web import Dashboard

    bot, radio = _bot(tmp_path)
    bot._allowed = lambda user: True
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    audio = buf.getvalue()

    async def go():
        link = bot.webauth.issue_link_token("@a:x", "!r:x")
        client = TestClient(TestServer(Dashboard(bot).app))
        await client.start_server()
        h = {"X-Musikbot": "1"}
        try:
            def form(name, data):
                f = FormData()
                f.add_field("file", data, filename=name, content_type="audio/wav")
                return f
            up = lambda name, data, hh=h: client.post("/api/radio/upload", data=form(name, data), headers=hh)
            assert (await up("a.wav", audio)).status == 401                 # ohne Login
            assert (await client.post("/api/link", json={"token": link}, headers=h)).status == 200
            assert (await up("a.wav", audio, {})).status == 403            # ohne Schutz-Header
            r = await up("a.wav", audio)
            assert r.status == 200 and (await r.json())["saved"] == ["a.wav"]
            r = await up("a.wav", audio)                                   # gleicher Name
            assert (await r.json())["saved"] == ["a (2).wav"] and (await r.json())["radio"] == 2
            assert (await up("fake.mp3", b"kein audio")).status == 400     # kein Audio
            assert (await up("x.txt", audio)).status == 400                # falsche Endung
        finally:
            await client.close()

    asyncio.run(go())
    assert sorted(os.listdir(radio)) == ["a (2).wav", "a.wav"]              # keine Temp-Reste
    assert len(bot._radio_scan()) == 2
