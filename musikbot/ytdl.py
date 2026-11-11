"""yt-dlp: Suche, Metadaten, Download. Alles blocking – über Executor aufrufen."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import yt_dlp

from .config import Config
from .util import is_url

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    """Benutzerfreundlicher Fehler für Suche/Download."""


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Suchpraefix im Befehl -> yt-dlp-Suchschluessel, z. B. `!play sc: daft punk`.
# Links aller von yt-dlp unterstuetzten Seiten (1000+) gehen ohne Praefix.
SEARCH_PREFIXES: dict[str, str] = {
    "yt": "ytsearch1", "youtube": "ytsearch1",
    "sc": "scsearch1", "soundcloud": "scsearch1",
    "bili": "bilisearch1", "bilibili": "bilisearch1",
    "nico": "nicosearch1", "niconico": "nicosearch1",
}
_PREFIX_RE = re.compile(r"^([A-Za-z]{2,10}):\s*(\S.*)$")


def split_search(query: str) -> tuple[str, str]:
    """`sc: foo` -> ('scsearch1', 'foo'); ohne bekanntes Praefix -> YouTube."""
    m = _PREFIX_RE.match(query)
    if m and m.group(1).lower() in SEARCH_PREFIXES:
        return SEARCH_PREFIXES[m.group(1).lower()], m.group(2).strip()
    return "ytsearch1", query


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_IGNORED_WORDS = {"lyric", "lyrics", "video", "official", "audio", "the", "und", "and", "feat", "ft"}


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= 2 and w not in _IGNORED_WORDS}


def pick_lyric_entry(entries: list, query: str) -> Optional[dict]:
    """Erstes Lyric-Video, das wirklich zur Suche passt.

    Der Titel (plus Kanal) muss "lyric" enthalten UND alle Suchwoerter, bei mehr als drei
    Woertern reicht eins weniger. Sonst None -> normale Suche (lieber richtiger Song ohne
    Text als falscher Song mit Text)."""
    wanted = _words(query)
    if not wanted:
        return None
    need = len(wanted) if len(wanted) <= 3 else len(wanted) - 1
    for e in entries:
        if not e:
            continue
        title = str(e.get("title") or "")
        if "lyric" not in title.lower():
            continue
        have = _words(f"{title} {e.get('channel') or e.get('uploader') or ''}")
        if len(wanted & have) >= need:
            return e
    return None


# Woerter, die eine bestimmte Fassung meinen. Steht eins in der Suche, muss es im Treffer
# stehen; steht keins in der Suche, wird das Original bevorzugt.
VERSION_WORDS = {"remix", "bootleg", "edit", "sped", "slowed", "nightcore", "mashup", "flip",
                 "rework", "cover", "live", "acoustic", "instrumental", "reverb", "techno",
                 "hardstyle", "phonk", "goth", "rmx", "vip"}


def score_entry(entry: dict, query: str) -> float:
    """Wie gut passt ein Suchtreffer zur Anfrage? 0..1 (+ kleine Boni), Fassung beachtet."""
    wanted = _words(query)
    if not wanted or not entry:
        return 0.0
    title = str(entry.get("title") or "")
    have = _words(f"{title} {entry.get('channel') or entry.get('uploader') or ''}")
    score = len(wanted & have) / len(wanted)
    want_version = wanted & VERSION_WORDS
    has_version = _words(title) & VERSION_WORDS
    if want_version and not (want_version & has_version):
        score -= 0.5            # Remix gesucht, Original gefunden
    if not want_version and has_version:
        score -= 0.3            # Original gesucht, Remix gefunden
    return score


def choose_entry(youtube: list, lyrics: list, soundcloud: list, query: str) -> Optional[dict]:
    """Besten Treffer aus YouTube, YouTube-Lyrics und SoundCloud waehlen.

    Bei gleicher Passung: Lyric-Video > YouTube > SoundCloud. Passt nichts ordentlich
    (< 0.5), bleibt es beim ersten YouTube-Treffer wie frueher."""
    best, best_score = None, -9.0
    pools = ((lyrics, 0.06), (youtube, 0.03), (soundcloud, 0.0))
    for pool, bonus in pools:
        for rank, e in enumerate(pool or []):
            if not e:
                continue
            sc = score_entry(e, query)
            if pool is lyrics and "lyric" not in str(e.get("title") or "").lower():
                continue
            sc += bonus - rank * 0.01
            if sc > best_score:
                best, best_score = e, sc
    if best is not None and best_score >= 0.5:
        return best
    first = [e for e in (youtube or []) if e]
    return first[0] if first else best


def _thumb_of(info: dict) -> str:
    """Titelbild-Adresse aus den yt-dlp-Infos (Albumcover oder Video-Vorschaubild), nur http(s)."""
    url = str(info.get("thumbnail") or "")
    if not url:
        thumbs = [t for t in (info.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
        if thumbs:
            url = str(thumbs[-1]["url"])
    return url if url.startswith(("http://", "https://")) else ""


@dataclass
class Resolved:
    key: str
    title: str
    url: str
    duration: float
    uploader: str
    thumbnail: str = ""


class MediaResolver:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------ Optionen
    def _base_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "restrictfilenames": True,
            "no_color": True,
        }
        # Viele Seiten blocken Python-TLS mit 403; mit curl_cffi als Chrome auftreten.
        try:
            import curl_cffi  # noqa: F401
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception:
            pass
        if self.cfg.yt_cookies_file:
            opts["cookiefile"] = self.cfg.yt_cookies_file
        if self.cfg.yt_player_client:
            opts["extractor_args"] = {"youtube": {"player_client": [self.cfg.yt_player_client]}}
        return opts

    # -------------------------------------------------------------- Suche
    def resolve(self, query: str) -> Resolved:
        """Link oder Suchbegriff -> ein konkreter Titel (erstes Suchergebnis)."""
        query = query.strip()
        if not query:
            raise MediaError("Bitte Link oder Suchbegriff angeben, z. B. "
                             "`/play Never Gonna Give You Up`.")
        try:
            if is_url(query):
                info = self._extract(query)
            else:
                key, term = split_search(query)
                entries = []
                if key == "ytsearch1":
                    entry = self._search_everywhere(term)
                    if entry:
                        entries = [entry]
                else:
                    listing = self._extract(f"{key}:{term}", flat=True)
                    entries = [e for e in (listing.get("entries") or []) if e]
                if not entries:
                    raise MediaError(f"Zu {query!r} wurde nichts gefunden.")
                url = entries[0].get("url") or entries[0].get("webpage_url")
                if not url:
                    raise MediaError("Suchergebnis hat keine Adresse.")
                info = self._extract(url)
        except MediaError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise MediaError(f"yt-dlp Fehler: {_clean(exc)}") from exc

        return self._to_resolved(info)

    def _search_everywhere(self, term: str) -> Optional[dict]:
        """YouTube, YouTube-Lyrics und SoundCloud parallel durchsuchen, besten Treffer waehlen."""
        lyric_term = term if "lyric" in term.lower() else f"{term} lyrics"
        targets = {"youtube": f"ytsearch5:{term}", "lyrics": f"ytsearch5:{lyric_term}",
                   "soundcloud": f"scsearch5:{term}"}

        def run(target: str) -> list:
            try:
                return list(self._extract(target, flat=True).get("entries") or [])
            except Exception as exc:  # eine Quelle darf ausfallen
                log.info("Suche %s fehlgeschlagen: %s", target, _clean(exc) if isinstance(exc, yt_dlp.utils.DownloadError) else exc)
                return []

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            found = dict(zip(targets, pool.map(run, targets.values())))
        entry = choose_entry(found["youtube"], found["lyrics"], found["soundcloud"], term)
        if entry:
            log.info("Suche %r -> %s (%s)", term, entry.get("title"), entry.get("ie_key") or entry.get("url"))
        return entry

    def _extract(self, target: str, flat: bool = False) -> dict:
        opts = self._base_opts()
        opts["extract_flat"] = "in_playlist" if flat else False
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        if info is None:
            raise MediaError("Keine Infos zu diesem Link bekommen.")
        return info

    def _to_resolved(self, info: dict) -> Resolved:
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise MediaError("Die Playlist ist leer oder nicht lesbar.")
            info = self._extract(entries[0].get("url") or entries[0].get("webpage_url"))
        extractor = info.get("extractor") or info.get("extractor_key") or "media"
        vid = str(info.get("id") or "")
        if not vid:
            raise MediaError("Der Anbieter liefert keine Titel-ID.")
        key = f"{_SAFE.sub('_', extractor).lower()}:{vid}"
        duration = info.get("duration") or 0
        return Resolved(
            key=key,
            title=str(info.get("title") or vid),
            url=str(info.get("webpage_url") or info.get("url") or ""),
            duration=float(duration or 0),
            uploader=str(info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""),
            thumbnail=_thumb_of(info),
        )

    # ----------------------------------------------------------- Download
    def download(self, resolved: Resolved, want_video: bool, dest_dir: str) -> str:
        """Lädt den Titel und gibt den fertigen Dateipfad zurück."""
        os.makedirs(dest_dir, exist_ok=True)
        stem = _SAFE.sub("_", resolved.key)[:120]
        opts = self._base_opts()
        opts.update({
            "outtmpl": os.path.join(dest_dir, stem + ".%(ext)s"),
            "format": self.cfg.resolved_video_format() if want_video else self.cfg.audio_format,
            "overwrites": True,
        })
        if want_video:
            opts["merge_output_format"] = "mkv"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(resolved.url or resolved.key.split(":", 1)[1],
                                        download=True)
                path = ydl.prepare_filename(info) if info else ""
        except yt_dlp.utils.DownloadError as exc:
            raise MediaError(f"Download fehlgeschlagen: {_clean(exc)}") from exc
        if path and not os.path.exists(path):
            # gemergte Dateien haben manchmal eine andere Endung
            stem_only = os.path.splitext(path)[0]
            for ext in (".mkv", ".webm", ".mp4", ".m4a", ".opus", ".mp3", ".ogg"):
                cand = stem_only + ext
                if os.path.exists(cand):
                    path = cand
                    break
        if not path or not os.path.exists(path):
            candidates = sorted(
                (os.path.join(dest_dir, f) for f in os.listdir(dest_dir)
                 if f.startswith(stem)),
                key=os.path.getmtime, reverse=True)
            if not candidates:
                raise MediaError("Download wurde beendet, aber keine Datei gefunden.")
            path = candidates[0]
        log.info("Heruntergeladen: %s (%.1f MB)", os.path.basename(path),
                 os.path.getsize(path) / 1e6)
        return path

    # -------------------------------------------------------- async Wrapper
    async def resolve_async(self, query: str) -> Resolved:
        return await asyncio.to_thread(self.resolve, query)

    async def download_async(self, resolved: Resolved, want_video: bool, dest_dir: str) -> str:
        return await asyncio.to_thread(self.download, resolved, want_video, dest_dir)


def _clean(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return (text[:300] + "…") if len(text) > 300 else text
