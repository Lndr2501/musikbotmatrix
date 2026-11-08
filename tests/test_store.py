import os

from musikbot.store import TrackStore


def test_files_are_never_kept(tmp_path):
    legacy = tmp_path / "library"
    legacy.mkdir()
    (legacy / "old.opus").write_bytes(b"x")
    tmp = tmp_path / "tmp"
    store = TrackStore(str(tmp_path / "db.sqlite3"), str(tmp), str(legacy))
    assert not legacy.exists()  # Altbestand beim Start entfernt

    track = store.note_request("yt:1", title="T", url="u")
    f = tmp / "yt_1.opus"
    f.write_bytes(b"audio")
    (tmp / "yt_1.opus.part").write_bytes(b"p")
    store.set_file("yt:1", str(f))
    assert track.playable

    store.release_file("yt:1")
    assert not track.playable
    assert os.listdir(tmp) == []
    assert store.get("yt:1").title == "T"  # nur Metadaten bleiben
