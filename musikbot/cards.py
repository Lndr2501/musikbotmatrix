"""Karten fuer Diddy (dev.diddy.card): strukturierte Daten neben dem normalen Text.

Jede Nachricht behaelt ihren lesbaren `body` (fuer Element & Co.); Diddy zeichnet aus dem Zusatzfeld
`dev.diddy.card` eine Karte. Aktualisiert wird per Bearbeitung (m.replace), die Karte muss dann auch in
`m.new_content` stehen, damit Suchen -> Laden -> Spielt in einer Karte laufen. Protokoll siehe
docs/bot-cards.md im Diddy-Repo. Rein (kein nio), damit alles ohne Matrix testbar ist.
"""
from __future__ import annotations

from typing import Optional

CARD_KEY = "dev.diddy.card"
VERSION = 1

TONES = ("info", "success", "warning", "error")
TRACK_STATES = ("searching", "downloading", "playing", "paused", "stopped", "error")


def _clean(card: dict) -> dict:
    """Leere Felder weglassen, Version setzen."""
    out = {k: v for k, v in card.items() if v not in (None, "", [], {})}
    out["v"] = VERSION
    return out


def card_content(body: str, card: dict, msgtype: str = "m.notice") -> dict:
    """Nachrichteninhalt: Rueckfall-Text plus Karte."""
    return {"msgtype": msgtype, "body": body, CARD_KEY: _clean(card)}


def edit_content(event_id: str, content: dict) -> dict:
    """Inhalt einer Bearbeitung (m.replace) fuer `event_id`; die Karte steht auch in m.new_content."""
    new = {k: v for k, v in content.items() if k in ("msgtype", "body", CARD_KEY)}
    edit = dict(new)
    edit["body"] = "* " + new["body"]
    edit["m.new_content"] = new
    edit["m.relates_to"] = {"rel_type": "m.replace", "event_id": event_id}
    return edit


def action(label: str, send: str) -> dict:
    return {"label": label, "send": send}


# Knopfreihen: Pause/Weiter, naechster Titel, 10 s zurueck/vor, leiser/lauter (relativ), Rest.
# Die Zeitleiste der Karte spult per Klick ("seek": "!seek" -> Diddy sendet "!seek 1:23").
_SEEK_AND_VOLUME = (
    action("⏪ 10 s", "!zurueck 10"), action("⏩ 10 s", "!vor 10"),
    action("🔉 −", "!volume -10"), action("🔊 +", "!volume +10"),
)
def controls(paused: bool = False, loop: bool = False) -> tuple:
    """Knopfreihe der Titel-Karte. Der Loop-Knopf zeigt, ob die Schleife gerade an ist."""
    first = action("▶ Weiter", "!resume") if paused else action("⏸ Pause", "!pause")
    loop_btn = action("🔁 Loop: an" if loop else "🔁 Loop", "!loop")
    rest = (action("📜 Warteschlange", "!queue"),) if not paused else ()
    return (first, action("⏭ Nächster", "!skip"), *_SEEK_AND_VOLUME, loop_btn, *rest,
            action("⏹ Stopp", "!stop"))


CONTROLS_PLAYING = controls(False)
CONTROLS_PAUSED = controls(True)
SEEK_COMMAND = "!seek"


def fmt_len(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def track_card(
    state: str,
    title: str,
    *,
    artist: str = "",
    duration: float = 0.0,
    position: Optional[float] = None,
    started_at_ms: Optional[int] = None,
    url: str = "",
    note: str = "",
    seek: str = "",
    volume: Optional[float] = None,
    thumbnail: str = "",
    actions=(),
) -> dict:
    """Karte eines Titels. `position` (s) gilt zum Zeitpunkt `started_at_ms` (Bot-Uhr).
    `seek`: Befehl fuer die Zeitleiste (Klick = "<seek> mm:ss"), `volume`: aktuelle Lautstaerke."""
    assert state in TRACK_STATES, state
    return {
        "kind": "track", "state": state, "title": title, "artist": artist or None,
        "duration": duration or None, "position": position, "startedAt": started_at_ms,
        "url": url or None, "note": note or None, "seek": seek or None, "volume": volume,
        "thumbnail": thumbnail or None, "actions": list(actions),
    }


_TRACK_ICON = {
    "searching": "🔎", "downloading": "⬇️", "playing": "🎵", "paused": "⏸",
    "stopped": "⏹", "error": "⚠️",
}
_TRACK_VERB = {
    "searching": "Suchen", "downloading": "Lade herunter", "playing": "Spielt",
    "paused": "Pausiert", "stopped": "Gestoppt", "error": "Fehler",
}


def track_body(card: dict) -> str:
    """Lesbarer Rueckfall-Text zu einer Titel-Karte (so sah die Nachricht vorher aus)."""
    dur = fmt_len(card.get("duration"))
    extra = f" ({dur})" if dur else ""
    return f"{_TRACK_ICON[card['state']]} {_TRACK_VERB[card['state']]}: {card['title']}{extra}"


def queue_card(now: Optional[dict], items: list[dict], total: int, actions=()) -> dict:
    return {"kind": "queue", "now": now, "items": items[:50], "total": total, "actions": list(actions)}


def queue_body(now: Optional[dict], items: list[dict], total: int) -> str:
    lines = []
    if now:
        lines.append(f"Läuft: {now['title']}")
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. {it['title']}")
    if total > len(items):
        lines.append(f"… und {total - len(items)} weitere")
    return "\n".join(lines) if lines else "Warteschlange ist leer."


def note_card(text: str, tone: str = "info", icon: str = "") -> dict:
    assert tone in TONES, tone
    return {"kind": "note", "text": text, "tone": tone, "icon": icon or None}


def note_body(text: str, icon: str = "") -> str:
    return f"{icon} {text}".strip()


def list_card(title: str, items: list[dict], total: int = 0, actions=(), empty: str = "") -> dict:
    """Allgemeine Liste (z. B. Favoriten): items = [{title, subtitle?, duration?}]."""
    return {"kind": "list", "title": title, "items": items[:50], "total": total or len(items),
            "empty": empty or None, "actions": list(actions)}


def list_body(title: str, items: list[dict], empty: str = "") -> str:
    if not items:
        return f"{title}: {empty or 'leer'}"
    return "\n".join([f"{title}:"] + [f"{i}. {it['title']}" for i, it in enumerate(items, 1)])


def stats_card(title: str, charts: list[dict], actions=()) -> dict:
    """Diagramme: charts = [{title, unit?, items: [{label, value, sub?}]}] als Balken."""
    return {"kind": "stats", "title": title, "charts": charts, "actions": list(actions)}


def stats_body(title: str, charts: list[dict]) -> str:
    lines = [title]
    for ch in charts:
        lines.append(f"{ch['title']}:")
        unit = ch.get("unit") or ""
        for i, it in enumerate(ch["items"], 1):
            lines.append(f"{i}. {it['label']} ({it['value']:g}{' ' + unit if unit else ''})")
    return "\n".join(lines)


def help_card(groups: list[dict], note: str = "") -> dict:
    return {"kind": "help", "groups": groups, "note": note or None}


def _g(title: str, *rows: tuple[str, str]) -> dict:
    return {"title": title, "items": [{"usage": u, "desc": d} for u, d in rows]}


HELP_GROUPS = [
    _g("Wiedergabe",
       ("!play <Link oder Suchbegriff>", "Link von fast jeder Seite (yt-dlp), sonst YouTube-Suche"),
       ("!play sc: <Begriff>", "SoundCloud-Suche (auch bili: und nico:)"),
       ("!pause · !resume", "pausieren · weiter"),
       ("!stop · !skip · !previous", "stoppen · nächster Titel · vorheriger Titel"),
       ("!status", "was läuft gerade?"),
       ("!seek <mm:ss | +30 | -30>", "springen"),
       ("!vor [s] · !zurueck [s]", "vorspulen · zurückspulen (Standard 10 s)")),
    _g("Warteschlange",
       ("!queue · !clear", "anzeigen · leeren"),
       ("!shuffle", "mischen"),
       ("!loop on|off", "aktuellen Titel wiederholen")),
    _g("Ton und Bild",
       ("!volume <0-300 | +10 | -10>", "Lautstärke (60 = Original, darüber Boost), +/- relativ"),
       ("!video on|off", "Video im Call")),
    _g("Call",
       ("!joincall · !leavecall", "Bot kommt in den Call · verlässt ihn"),
       ("!dashboard", "Web-Dashboard: Link + persönliches Passwort per Direktnachricht")),
    _g("Playlists und Dauerbetrieb",
       ("!playlist create|delete <name>", "anlegen · löschen"),
       ("!playlist add <name> [Link]", "Titel hinzufügen (ohne Link: der laufende)"),
       ("!playlist remove <name> <nr>", "Titel entfernen"),
       ("!playlist show <name> · list", "anzeigen · alle Listen"),
       ("!playlist play <name> [shuffle]", "abspielen · !playlist enqueue <name> einreihen"),
       ("!fav · !fav list · !fav remove <nr>", "laufenden Titel merken · deine Favoriten · entfernen"),
       ("!fav play [shuffle]", "deine Favoriten abspielen (Steuern: nur im Call)"),
       ("!stats", "Diagramme: meistgespielte Titel, wer am meisten Musik hört"),
       ("!24/7 song [Link] · playlist <name> · off", "Dauerschleife, Bot bleibt im Call")),
]
HELP_NOTE = ("Steuern dürfen nur Leute, die selbst im Call sind (Lesen: help, status, queue, "
             "playlist list/show geht immer). Ist die Warteschlange leer und der Song zu Ende, "
             "verlässt der Bot den Call (außer im 24/7-Modus). Titel werden nur gespeichert, wenn "
             "sie oft gespielt werden (ab 5 Downloads); nach 3 Tagen ohne Wiedergabe wird die Datei gelöscht.")


# Befehlsliste fuer die Eingabe-Vorschlaege der Clients (State-Event dev.diddy.commands, state_key = Bot-ID).
COMMANDS_EVENT = "dev.diddy.commands"
COMMAND_LIST = [
    ("play", "!play <Link oder Suchbegriff>", "Titel abspielen"),
    ("dashboard", "!dashboard", "Web-Dashboard: Link + Passwort per DM"),
    ("pause", "!pause", "pausieren"),
    ("resume", "!resume", "weiterspielen"),
    ("stop", "!stop", "stoppen"),
    ("skip", "!skip", "nächster Titel"),
    ("previous", "!previous", "vorheriger Titel"),
    ("queue", "!queue", "Warteschlange anzeigen"),
    ("clear", "!clear", "Warteschlange leeren"),
    ("status", "!status", "was läuft gerade?"),
    ("seek", "!seek <mm:ss | +30 | -30>", "zu einer Stelle springen"),
    ("vor", "!vor [s]", "vorspulen"),
    ("zurueck", "!zurueck [s]", "zurückspulen"),
    ("volume", "!volume <0-300 | +10 | -10>", "Lautstärke"),
    ("shuffle", "!shuffle", "Warteschlange mischen"),
    ("loop", "!loop on|off", "Titel wiederholen"),
    ("video", "!video on|off", "Video im Call"),
    ("joincall", "!joincall", "Bot kommt in den Call"),
    ("leavecall", "!leavecall", "Bot verlässt den Call"),
    ("playlist", "!playlist create|add|remove|show|list|play|enqueue|delete", "Playlists"),
    ("fav", "!fav [list | remove <nr> | play [shuffle]]", "deine Favoriten"),
    ("stats", "!stats", "Diagramme: meistgespielt, wer hört am meisten"),
    ("24/7", "!24/7 song [Link] | playlist <name> | off", "Dauerschleife"),
    ("help", "!help", "alle Befehle"),
]


def commands_content(prefix: str = "!") -> dict:
    """Inhalt des State-Events: Praefix plus Befehle (name ohne Praefix, usage, desc)."""
    return {"v": VERSION, "prefix": prefix,
            "commands": [{"name": n, "usage": u, "desc": d} for n, u, d in COMMAND_LIST]}
