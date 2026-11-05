"""Regel-Tests: 3x anfordern -> Library, Cache-Größe, Fenster-Regel."""

import pytest

from musikbot.policy import CACHE, LIBRARY, Policy, register_request

P = Policy(save_after=3, cache_max=10, window_self=2, window_others=9)


def req(entries, key):
    return register_request(entries, key, P)


def test_drei_anforderungen_machen_library():
    entries = {}
    _, o1 = None, req(entries, "a")
    assert o1.first_request and entries["a"].state == CACHE
    o2 = req(entries, "a")
    assert entries["a"].state == CACHE
    o3 = req(entries, "a")
    assert o3.promoted
    assert entries["a"].state == LIBRARY
    assert entries["a"].requests == 3


def test_library_titel_wird_nie_geloescht():
    entries = {}
    for _ in range(3):
        req(entries, "keep")
    for i in range(30):
        req(entries, f"other{i}")
    assert "keep" in entries
    assert entries["keep"].state == LIBRARY


def test_cache_grenze_von_zehn():
    entries = {}
    cap = Policy(save_after=99, cache_max=10, window_self=99, window_others=99)
    for i in range(15):
        register_request(entries, f"t{i}", cap)
    cache = [e for e in entries.values() if e.state == CACHE]
    assert len(cache) <= 10
    # die letzten zehn sind geblieben
    assert all(f"t{i}" in entries for i in range(5, 15))


def test_fenster_regel_song_fliegt_ohne_zusatz_anfragen_raus():
    entries = {}
    req(entries, "wackelig")
    # 9 andere Anforderungen, ohne dass "wackelig" nochmal kommt
    for i in range(8):
        req(entries, f"andere{i}")
        assert "wackelig" in entries, f"zu früh raus nach {i + 1} anderen"
    out = req(entries, "andere8")
    assert "wackelig" not in entries
    assert "wackelig" in out.evicted


def test_zwei_zusaetzliche_anfragen_schuetzen():
    entries = {}
    req(entries, "liebling")
    req(entries, "x1")
    req(entries, "liebling")  # 2. Anforderung -> Schutz fast erreicht
    req(entries, "x2")
    out = req(entries, "liebling")  # 3. insgesamt -> Library, aber vorher geschützt
    assert out.entry.settled or out.promoted
    # eigenständiger Schutz-Fall ohne Promotion:
    entries2 = {}
    p = Policy(save_after=99, cache_max=10, window_self=2, window_others=9)
    register_request(entries2, "p", p)
    register_request(entries2, "y1", p)
    register_request(entries2, "p", p)
    register_request(entries2, "y2", p)
    register_request(entries2, "p", p)
    assert entries2["p"].settled
    for i in range(20):
        register_request(entries2, f"z{i}", p)
    assert "p" in entries2 and entries2["p"].state == CACHE


def test_geschuetzte_fliegen_erst_bei_platzmangel():
    entries = {}
    p = Policy(save_after=99, cache_max=3, window_self=2, window_others=9)
    register_request(entries, "alt", p)      # bleibt ungeschützt
    register_request(entries, "fest", p)
    register_request(entries, "fest", p)     # geschützt
    register_request(entries, "neu1", p)
    register_request(entries, "neu2", p)     # 4 im Cache -> einer muss weg
    assert "fest" in entries
    assert "alt" not in entries


def test_lru_als_letzter_anker():
    entries = {}
    p = Policy(save_after=99, cache_max=2, window_self=99, window_others=99)
    register_request(entries, "a", p, now=1)
    register_request(entries, "b", p, now=2)
    register_request(entries, "a", p, now=3)  # a ist frischer (gleiche Fensterschritte)
    register_request(entries, "c", p, now=4)  # b muss weichen
    assert "b" not in entries
    assert {"a", "c"} <= set(entries)


def test_evicted_entries_sind_weg_und_gemeldet():
    entries = {}
    p = Policy(save_after=99, cache_max=1, window_self=99, window_others=99)
    register_request(entries, "a", p)
    out = register_request(entries, "b", p)
    assert out.evicted == ["a"]
    assert [e.key for e in out.removed] == ["a"]
    assert "a" not in entries
