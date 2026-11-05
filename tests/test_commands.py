"""Kommandos: Parsen, Aliase, Praefixe."""
from musikbot.commands import COMMANDS, parse


def test_basic_play():
    p = parse("/play Never gonna give you up", ("/",))
    assert p and p.name == "play"
    assert p.arg == "Never gonna give you up"


def test_prefixes_and_aliases():
    assert parse("!p Rick Astley", ("/", "!")).name == "play"
    assert parse(".pause", ("/", "!", ".")).name == "pause"
    assert parse("pause") is None              # ohne Praefix: kein Kommando
    assert parse("/hilfe").name == "help"
    assert parse("/abspielen x").name == "play"
    assert parse("/pausieren").name == "pause"


def test_unknown_and_empty():
    assert parse("/diesgibtesnicht") is None
    assert parse("/") is None
    assert parse("   ") is None
    assert parse("hallo zusammen") is None


def test_volume_arg():
    p = parse("/vol 80")
    assert p.name == "volume" and p.arg == "80"


def test_all_canon_names_parse():
    for name in COMMANDS:
        assert parse(f"/{name}").name == name
