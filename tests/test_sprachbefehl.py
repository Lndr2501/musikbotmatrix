"""Sprachbefehle: Weckwort am Satzanfang, dann Regeln (kein Sprachmodell)."""
import pytest

from musikbot.sprachbefehl import command, name_tokens, only_filler, parse, wake

QWEN = name_tokens("DJ Qwen", "DJ Quenn")


@pytest.mark.parametrize("text,expected", [
    # skip
    ("DJ Gwen, nächster Song.", ("skip", "")),
    ("Kwen, skip!", ("skip", "")),
    ("DJ Qwen, überspring den", ("skip", "")),
    ("DJ Qwen, der Song nervt", ("skip", "")),
    ("DJ Qwen, anderer Song bitte", ("skip", "")),
    ("DJ Qwen next", ("skip", "")),
    ("DJ Qwen, spiel mal was anderes", ("skip", "")),
    ("DJ Qwen, kannst du was anderes spielen?", ("skip", "")),
    ("DJ Qwen! Nächster Song.", ("skip", "")),                    # Befehl im naechsten Satz
    ("Das war geil. DJ Qwen, skip!", ("skip", "")),               # Weckwort an vorigen Satz gehaengt
    ("D.J. Qwen, skippen", ("skip", "")),
    ("DJQwen skip", ("skip", "")),
    # previous
    ("DJ Qwen, vorheriger Song", ("previous", "")),
    ("DJ Qwen, letzter Song nochmal", ("previous", "")),
    ("DJ Qwen, zurück zum letzten", ("previous", "")),
    # pause / resume / stop
    ("DJ Qwen, Pause!", ("pause", "")),
    ("DJ Qwen, halt mal an", ("pause", "")),
    ("DJ Qwen, pausier mal", ("pause", "")),
    ("DJ Qwen, weiter", ("resume", "")),
    ("DJ Qwen, mach weiter", ("resume", "")),
    ("Ok DJ Qwen spiel weiter", ("resume", "")),
    ("DJ Qwen, hör auf!", ("stop", "")),
    ("DJ Qwen, Musik aus", ("stop", "")),
    ("DJ Qwen stopp", ("stop", "")),
    # volume
    ("Hey DJ Qwen, mach mal lauter!", ("volume", "+15")),
    ("DJ Qwen, viel leiser bitte", ("volume", "-30")),
    ("DJ Qwen, leiser", ("volume", "-15")),
    ("DJ Qwen, Lautstärke auf 80", ("volume", "80")),
    ("DJ Qwen, das ist zu laut", ("volume", "-15")),
    ("Didschei Kwen, kannst du lauter machen?", ("volume", "+15")),
    # play
    ("DJ Qwen spiel bitte Sandstorm von Darude", ("play", "sandstorm von darude")),
    ("DJ Gwen spiel Sandstorm", ("play", "sandstorm")),
    ("DJ Qwen, mach mal Sandstorm an", ("play", "sandstorm")),
    ("DJ Qwen, ich will Sandstorm hören", ("play", "sandstorm")),
    ("DJ Qwen spiel was von Darude", ("play", "darude")),
    ("DJ Qwen, kannst du mal Sandstorm spielen?", ("play", "sandstorm")),
    ("DJ Qwen, spielst du mal Sandstorm?", ("play", "sandstorm")),
    ("DJ Qwen, spiel was Schnelles", ("play", "was schnelles")),
    ("DJ Qwen, play Despacito", ("play", "despacito")),
    # shuffle
    ("DJ Qwen, misch mal die Liste", ("shuffle", "")),
])
def test_commands(text, expected):
    assert parse(text, QWEN) == expected


@pytest.mark.parametrize("text", [
    "DJ Qwen, wie findest du den Song?",            # Frage -> Sprachmodell
    "DJ Qwen, was läuft da?",
    "DJ Qwen, wie war das Spiel gestern?",          # "Spiel" als Nomen
    "der Song ist nicht zum Skippen gut?",           # nicht angesprochen
    "Max, spiel mal Fußball",                        # jemand anderes
    "ich find DJ Qwen cool, mach lauter",            # Name mitten im Satz: kein Weckwort
    "frag mal DJ Qwen, ob er skippt",
    "Hey Kwen, skip",                                # blosser Name nur als allererstes Wort
    "DJ Qwen, nicht skippen!",                       # verneint
    "DJ Qwen, hör auf zu reden",                     # meint nicht die Musik
    "DJ Qwen?",                                      # nur Weckwort -> Rueckfrage, kein Befehl
    "DJ Qwen spiel",
    "Hallo zusammen, hier ist DJ Qwen. Wir starten mit voll.",
    "DJ Qwen, gestern hab ich auf der Party gehört, dass der nächste Song von denen richtig gut wird",
    "",
])
def test_no_command(text):
    assert parse(text, QWEN) is None


def test_wake_returns_what_follows_with_original_case():
    assert wake("Hey DJ Qwen, wie heißt der Song?", QWEN) == "wie heißt der Song?"
    assert wake("DJ Qwen! Nächster Song.", QWEN) == "Nächster Song."
    assert wake("DJ Qwen?", QWEN) == ""
    assert wake("ich find DJ Qwen cool", QWEN) is None
    assert wake("Das ist von D.J. Qwen gemacht", QWEN) is None


def test_follow_up_sentence_without_wake_word_and_filler():
    assert command("nächster Song") == ("skip", "")
    assert command("äh, mach mal lauter") == ("volume", "+15")
    assert command("wie heißt der?") is None
    assert only_filler("") and only_filler("hey!") and only_filler("ja bitte")
    assert not only_filler("wie heißt der Song?")


def test_other_dj_names():
    names = name_tokens("DJ Bär", "")
    assert parse("DJ Bär, skip", names) == ("skip", "")
    assert parse("Bär, lauter", names) == ("volume", "+15")
    assert parse("der Bär soll skippen", names) is None
