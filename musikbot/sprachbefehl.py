"""Sprachbefehle: "DJ Qwen, skip!" -> ("skip", "").

Rein und gut testbar. Ein Befehl zaehlt nur, wenn der DJ im selben Satz angesprochen wird
(oder der Satz davor nur aus der Ansprache besteht: "DJ Qwen! Naechster Song."). Alles andere
(Fragen, Plaudern) liefert None – dann antwortet das Ohr wie bisher ueber das Sprachmodell.

Hier liegen auch die gemeinsamen Helfer fuers Erkennen der Ansprache (`addressed`,
`name_tokens`); ohr.py importiert sie von hier.
"""
from __future__ import annotations

import re
from typing import Optional

_DJ_MARKERS = {"dj", "didschei", "deejay", "dschei", "didschey", "deejays", "djs", "dijay", "deeschei"}
# hoechstens ein Rufwort vor dem Weckwort ("Hey DJ Qwen", "Ok DJ Qwen")
_CALLS = {"hey", "hallo", "ok", "okay", "yo", "ey", "hi", "na", "hej"}
_QWEN_VARIANTS = {"qwen", "kwen", "quen", "quenn", "kwenn", "qwenn", "gwen", "kuen", "kven", "kwän", "quinn"}

MAX_WORDS = 8          # mehr Woerter (ohne Fuellwoerter) = eher Plaudern als Befehl
MAX_QUERY_WORDS = 12


def _norm(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\bd\s*\.?\s*j\b\.?", " dj ", t)             # "D.J." / "D J" -> "dj"
    t = re.sub(r"[^\wäöüß.!?]+", " ", t)
    return t


def name_tokens(*names: str) -> set[str]:
    """Kernwoerter der DJ-Namen ohne "DJ"; fuer Qwen-artige Namen samt Whisper-Schreibweisen."""
    out: set[str] = set()
    for n in names:
        for w in re.findall(r"[\wäöüß]+", (n or "").lower()):
            if w not in _DJ_MARKERS:
                out.add(w)
    if out & _QWEN_VARIANTS:
        out |= _QWEN_VARIANTS
    if "bär" in out:
        out |= {"baer", "bear"}
    return out


def _wake_at(words: list[str], names: set[str]) -> Optional[int]:
    """Beginnt der Satz mit dem Weckwort? Dann Index des ersten Worts danach, sonst None.

    Weckwort = [ein Rufwort] + "DJ" + Name ("Hey DJ Qwen", "DJ Gwen", "Didschei Kwen", "DJQwen")
    oder der blosse Name als allererstes Wort ("Kwen, skip"). Erwaehnungen mitten im Satz
    ("ich find DJ Qwen cool") zaehlen nicht."""
    if not words or not names:
        return None
    i = 1 if words[0] in _CALLS and len(words) > 1 else 0
    w = words[i]
    if w in _DJ_MARKERS and i + 1 < len(words) and words[i + 1] in names:
        return i + 2
    if w.startswith("dj") and w[2:] in names:                     # "DJQwen" zusammengeschrieben
        return i + 1
    if i == 0 and w in names:
        return 1
    return None


def _sentences(text: str) -> list[tuple[list[str], bool]]:
    """[(Woerter, endet mit '?')] fuer jeden Satz."""
    out = []
    for m in re.finditer(r"([^.!?]+)([.!?]*)", _norm(text)):
        words = m.group(1).split()
        if words:
            out.append((words, "?" in m.group(2)))
    return out


def wake(text: str, names: set[str]) -> Optional[str]:
    """Rest nach dem Weckwort (Rest des Satzes plus die folgenden Saetze), "" wenn nichts folgt,
    None ohne Weckwort. Whisper haengt das Weckwort oft an einen vorigen Satz derselben Person:
    deshalb wird jeder Satzanfang geprueft."""
    if not text or not names:
        return None
    text = re.sub(r"\bd\s*\.\s*j\b\.?", "DJ", text, flags=re.I)    # "D.J." nicht als Satzende lesen
    parts = [m.group(0) for m in re.finditer(r"[^.!?]+[.!?]*", text)]
    for k, part in enumerate(parts):
        tokens = list(re.finditer(r"[\wäöüß]+", part))
        at = _wake_at([t.group(0).lower() for t in tokens], names)
        if at is None:
            continue
        rest = part[tokens[at - 1].end():].lstrip(" ,;:-–!?.").strip()
        follow = "".join(parts[k + 1:]).strip()
        out = (rest + " " + follow).strip() if rest else follow
        return out if re.search(r"[\wäöüß]", out) else ""
    return None


def addressed(text: str, names: set[str]) -> bool:
    """Beginnt ein Satz mit dem Weckwort ("DJ Qwen, …")?"""
    return wake(text, names) is not None


# ------------------------------------------------------------------ Befehle
_FILLER = {"mal", "bitte", "doch", "jetzt", "eben", "einfach", "schnell", "gerne", "gern", "danke",
           "hier", "kurz", "nun", "ja", "also", "und", "äh", "ähm", "hey", "hallo", "ok", "okay", "yo",
           "ey", "kannst", "könntest", "koenntest", "würdest", "wuerdest", "du", "uns", "mir", "bro",
           "digga", "alter", "please", "can", "you", "the"}
_QUESTION_START = {"wie", "was", "warum", "wieso", "weshalb", "wer", "wo", "wann", "welche", "welcher",
                   "welches", "welchen", "ist", "sind", "findest", "magst", "hast", "bist", "kennst",
                   "weißt", "weisst", "war", "hattest", "how", "what", "why", "do", "is", "who"}
_NEGATION = {"nicht", "nie", "kein", "keine", "keinen", "niemals", "don", "dont", "not"}

_SONG = r"(?:song|titel|track|lied|nummer|stück|stueck)"
_VOL_ABS = re.compile(r"\b(?:lautstärke|lautstaerke|volume|vol)\s+(?:auf\s+|to\s+|bei\s+)?(\d{1,3})\b")
_VOL_REL = re.compile(r"\b(viel|deutlich|richtig|ordentlich|noch|ganz)?\s*(lauter|leiser|louder|quieter)\b")
_VOL_PHRASE = (
    (re.compile(r"\b(?:nicht\s+so\s+laut|zu\s+laut|volume\s+down|turn\s+it\s+down)\b"), "-15"),
    (re.compile(r"\b(?:zu\s+leise|volume\s+up|turn\s+it\s+up|aufdrehen|dreh\s+auf)\b"), "+15"),
)
# "hör auf zu reden" heisst nicht "Musik aus"
_STOP = re.compile(r"\b(?:stopp?|stoppen|(?:hör|hoer)\s+auf|aufhören|aufhoeren|musik\s+aus|mach\s+aus|"
                   r"ausmachen|schluss\s+jetzt|feierabend)\b(?!\s+zu\b)")
_PREVIOUS = re.compile(r"\b(?:vorherig\w*|vorige\w*|previous|zur[üu]ck\s+zum\s+letzten|"
                       rf"letzte[nrs]?\s+{_SONG}\s+(?:nochmal|noch\s+mal|wieder))\b")
_SKIP = re.compile(r"\b(?:skip\w*|überspring\w*|ueberspring\w*|next|"
                   rf"n[äa]chste[nrs]?(?:\s+{_SONG})?|andere[nrs]?\s+{_SONG}|{_SONG}\s+nervt)\b")
_PAUSE = re.compile(r"\b(?:pause|pausier\w*|anhalten|halt\s+(?:mal\s+)?an)\b")
_RESUME = re.compile(r"\b(?:weiter\s*spielen|weiterspielen|weitermachen|mach\s+weiter|fortsetzen|resume|"
                     r"spiel\s+weiter|play\s+again)\b")
_SHUFFLE = re.compile(r"\b(?:misch\w*|shuffle)\b")
_PLAY = (
    re.compile(r"^(?:spiel|spiele|spielst|play|leg|lege)\s+(?:auf\s+)?(.+?)(?:\s+auf)?$"),
    re.compile(r"^mach\w*\s+(.+?)\s+an$"),
    re.compile(r"^(?:ich|wir)\s+(?:will|wollen|möchte|moechte|möchten|moechten|würde\s+gern|wuerde\s+gern)\s+"
               r"(?:gern\s+|gerne\s+|mal\s+)*(.+?)\s+h[öo]e?ren$"),
    re.compile(r"^(?:kannst|könntest|koenntest|würdest|wuerdest)\s+du\s+(?:mal\s+|bitte\s+|uns\s+|mir\s+|"
               r"noch\s+)*(.+?)\s+(?:spielen|auflegen|anmachen)$"),
)
_QUERY_LEAD = re.compile(r"^(?:(?:mal|bitte|doch|du|uns|mir|einfach|jetzt|noch|einmal|nochmal)\s+)+")
_PLAY_VERBS = {"spiel", "spiele", "spielst", "play", "leg", "lege", "legst"}
_QUERY_TAIL = re.compile(r"(?:\s+(?:bitte|ab|an|danke|mal|jetzt|auf|please))+$")
_QUERY_FROM = re.compile(r"^(?:etwas|was|irgendwas|ein\s+lied|einen\s+song|ein\s+stück|ein\s+stueck|"
                         r"musik|songs?|lieder)\s+von\s+")


def _command(rest: list[str], question: bool) -> Optional[tuple[str, str]]:
    """Befehl aus den Woertern ausser der Ansprache."""
    core = [w for w in rest if w not in _FILLER]
    if not core:
        return None
    s = " ".join(rest)
    first = core[0]
    request = bool(rest) and (rest[0] in {"kannst", "könntest", "koenntest", "würdest", "wuerdest", "can"}
                              or first in _PLAY_VERBS)          # "Spielst du mal Sandstorm?" = Wunsch
    if question and first in _QUESTION_START and not request:
        return None                                   # "Wie findest du den Song?" -> Plaudern
    negated = any(w in _NEGATION for w in core)

    m = _VOL_ABS.search(s)
    if m and len(core) <= MAX_WORDS:
        return "volume", str(min(300, int(m.group(1))))
    for rx, arg in _VOL_PHRASE:
        if rx.search(s) and len(core) <= MAX_WORDS:
            return "volume", arg
    m = _VOL_REL.search(s)
    if m and not negated and len(core) <= MAX_WORDS:
        step = 30 if m.group(1) in ("viel", "deutlich", "richtig", "ordentlich") else 15
        return "volume", ("+" if m.group(2) in ("lauter", "louder") else "-") + str(step)

    # Spielwunsch: Verb steht vorne ("spiel Sandstorm"), nicht mitten im Satz ("wie war das Spiel")
    lead = _QUERY_LEAD.sub("", " ".join(w for w in rest if w not in {"hey", "hallo", "ok", "okay", "ey", "yo"}))
    if not negated and not (question and not request):
        for rx in _PLAY:
            m = rx.match(lead)
            if not m:
                continue
            query = _QUERY_TAIL.sub("", _QUERY_LEAD.sub("", m.group(1).strip())).strip()
            if query in ("weiter", "was", "etwas", "musik", "irgendwas") or _RESUME.search("spiel " + query):
                break                                 # "spiel weiter" -> resume (unten)
            if re.fullmatch(r"(?:was|etwas|mal)?\s*(?:anderes|neues|ander[ne]s?\s+lied)", query):
                return "skip", ""                     # "spiel mal was anderes"
            query = _QUERY_FROM.sub("", query).strip()
            if sum(c.isalpha() for c in query) >= 2 and len(query.split()) <= MAX_QUERY_WORDS:
                return "play", query
            return None

    if len(core) > MAX_WORDS:
        return None
    if _RESUME.search(s) or core == ["weiter"]:
        return None if negated else ("resume", "")
    if negated:
        return None
    for rx, name in ((_STOP, "stop"), (_PREVIOUS, "previous"), (_SKIP, "skip"),
                     (_PAUSE, "pause"), (_SHUFFLE, "shuffle")):
        if rx.search(s):
            return name, ""
    return None


def only_filler(text: str) -> bool:
    """Nichts ausser Fuell-/Rufwoertern ("DJ Qwen?" -> "", "DJ Qwen, hey!" -> "hey")?"""
    return not [w for w in _norm(text or "").replace(".", " ").replace("!", " ").replace("?", " ").split()
                if w not in _FILLER and w not in _CALLS]


def command(text: str) -> Optional[tuple[str, str]]:
    """Befehl aus dem, was nach dem Weckwort kam (oder aus einem Folgesatz ohne Weckwort):
    der erste Satz mit Inhalt zaehlt, spaetere Saetze (Plaudern) nicht."""
    for words, question in _sentences(text or ""):
        if not [w for w in words if w not in _FILLER]:
            continue
        return _command(words, question)
    return None


def parse(text: str, names: set[str]) -> Optional[tuple[str, str]]:
    """Gesprochenen Satz -> (Befehl, Argument) wie im Chat, oder None.
    Nur mit Weckwort am Satzanfang ("DJ Qwen, skip", "Hey DJ Qwen! Nächster Song.")."""
    rest = wake(text, names)
    return command(rest) if rest else None
