"""Diddy-Karten: Aufbau, Rueckfall-Text und Bearbeitungen."""
import json

from musikbot import cards
from musikbot.commands import HELP


def test_card_content_keeps_a_plain_fallback_and_drops_empty_fields():
    card = cards.track_card("playing", "Cheri Cheri Lady", artist="", duration=197,
                            position=0.0, started_at_ms=1000, actions=cards.CONTROLS_PLAYING)
    c = cards.card_content(cards.track_body(card), card)
    assert c["msgtype"] == "m.notice"
    assert c["body"] == "🎵 Spielt: Cheri Cheri Lady (3:17)"
    inner = c[cards.CARD_KEY]
    assert inner["v"] == 1 and inner["kind"] == "track" and inner["state"] == "playing"
    assert "artist" not in inner and "note" not in inner          # leer -> weggelassen
    assert inner["position"] == 0.0                                 # 0 ist ein Wert, kein "leer"
    assert inner["actions"][0] == {"label": "⏸ Pause", "send": "!pause"}
    json.dumps(c)                                                   # muss JSON-serialisierbar sein


def test_edit_content_repeats_the_card_in_new_content():
    first = cards.track_card("searching", "cherry")
    second = cards.track_card("downloading", "Cheri Cheri Lady", duration=197)
    c2 = cards.card_content(cards.track_body(second), second)
    e = cards.edit_content("$abc", c2)
    assert e["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$abc"}
    assert e["body"] == "* " + c2["body"]
    assert e["m.new_content"]["body"] == c2["body"]
    assert e["m.new_content"][cards.CARD_KEY]["state"] == "downloading"
    assert e[cards.CARD_KEY]["state"] == "downloading"
    assert first["state"] == "searching"


def test_all_track_states_have_a_fallback_body():
    for state in cards.TRACK_STATES:
        body = cards.track_body(cards.track_card(state, "X"))
        assert "X" in body


def test_queue_card_and_body():
    now = {"title": "A"}
    items = [{"title": "B", "duration": 60}, {"title": "C"}]
    assert cards.queue_body(now, items, 2) == "Läuft: A\n1. B\n2. C"
    assert cards.queue_body(None, [], 0) == "Warteschlange ist leer."
    assert cards.queue_body(now, items, 5).endswith("… und 3 weitere")
    card = cards.queue_card(now, items, 5, cards.CONTROLS_PAUSED)
    assert card["total"] == 5 and card["kind"] == "queue"
    assert len(cards.queue_card(None, [{"title": str(i)} for i in range(80)], 80)["items"]) == 50


def test_note_card():
    c = cards.card_content(cards.note_body("pausiert.", "⏸"), cards.note_card("pausiert.", "warning", "⏸"))
    assert c["body"] == "⏸ pausiert."
    assert c[cards.CARD_KEY]["tone"] == "warning"


def test_help_groups_cover_every_command_of_the_text_help():
    """Die Karte darf keinen Befehl weglassen, den die Text-Hilfe nennt."""
    usages = " ".join(i["usage"] for g in cards.HELP_GROUPS for i in g["items"])
    for cmd in ("!play", "!pause", "!resume", "!stop", "!skip", "!previous", "!queue", "!clear",
                "!status", "!seek", "!vor", "!zurueck", "!volume", "!video", "!shuffle", "!loop",
                "!joincall", "!leavecall", "!playlist", "!24/7"):
        assert cmd in usages, cmd
        assert cmd in HELP, cmd
    card = cards.help_card(cards.HELP_GROUPS, cards.HELP_NOTE)
    json.dumps(card)
    assert all(g["items"] for g in card["groups"])


def test_track_card_has_seek_volume_and_the_new_controls():
    card = cards.track_card("playing", "X", duration=100, position=1.0, started_at_ms=5,
                            seek=cards.SEEK_COMMAND, volume=60, actions=cards.CONTROLS_PLAYING)
    c = cards.card_content(cards.track_body(card), card)[cards.CARD_KEY]
    assert c["seek"] == "!seek" and c["volume"] == 60
    sends = [a["send"] for a in c["actions"]]
    for cmd in ("!pause", "!skip", "!zurueck 10", "!vor 10", "!volume -10", "!volume +10", "!stop"):
        assert cmd in sends, cmd
    assert len(sends) <= 10                                  # Diddy zeigt hoechstens zehn Knoepfe
    paused = [a["send"] for a in cards.CONTROLS_PAUSED]
    assert "!resume" in paused and "!volume +10" in paused and "!pause" not in paused
    assert cards._clean(cards.track_card("playing", "X")).get("seek") is None   # leer -> weggelassen


def test_commands_content_lists_every_command_with_usage():
    c = cards.commands_content("!")
    names = {x["name"] for x in c["commands"]}
    assert c["prefix"] == "!" and {"play", "pause", "fav", "stats", "help"} <= names
    assert all(x["usage"].startswith("!") and x["desc"] for x in c["commands"])
