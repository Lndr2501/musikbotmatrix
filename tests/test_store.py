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


def test_often_played_tracks_are_kept_and_pruned_after_days(tmp_path):
    import os
    import time
    from musikbot.store import TrackStore
    hot = str(tmp_path / "hot")
    st = TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp"), hot_dir=hot, hot_after=3, hot_days=3)
    st.note_request("yt:1", title="x")

    def play_once():
        path = str(tmp_path / "tmp" / "yt_1.m4a")
        open(path, "wb").write(b"audio")
        st.set_file("yt:1", path)
        st.note_download("yt:1")
        st.release_file("yt:1")
        return path

    for _ in range(2):                                   # 1. und 2. Download: nichts bleibt liegen
        assert not os.path.exists(play_once()) and st.hot_path("yt:1") is None
    play_once()                                          # 3. Download: wird gemerkt
    kept = st.hot_path("yt:1")
    assert kept and os.path.dirname(kept) == hot and os.path.exists(kept)
    st.set_file("yt:1", kept)                            # naechste Wiedergabe: aus dem Speicher
    st.release_file("yt:1")
    assert os.path.exists(kept)                          # ... und Datei bleibt liegen
    assert st.prune_hot() == 0
    st.db.execute("UPDATE hot SET last_used = ?", (int(time.time()) - 4 * 86400,))
    assert st.prune_hot() == 1 and not os.path.exists(kept) and st.hot_path("yt:1") is None
    # Neustart loescht gemerkte Titel nicht
    st.note_download("yt:1")
    st.close()


def test_hot_disabled_never_keeps(tmp_path):
    import os
    from musikbot.store import TrackStore
    st = TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp"), hot_dir=str(tmp_path / "hot"), hot_after=0)
    st.note_request("k", title="x")
    p = str(tmp_path / "tmp" / "k.m4a")
    open(p, "wb").write(b"a")
    st.set_file("k", p)
    st.note_download("k")
    st.release_file("k")
    assert not os.path.exists(p) and st.hot_path("k") is None
