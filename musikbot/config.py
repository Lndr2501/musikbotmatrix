"""Konfiguration – alles über Umgebungsvariablen (docker-freundlich)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Konfiguration fehlt oder ist inkonsistent."""


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass
class Config:
    # --- Matrix-Zugang -----------------------------------------------------
    homeserver: str = ""
    user: str = ""
    password: str = ""
    token: str = ""
    device_id: str = "musikbot"
    # Räume (Room-IDs oder Aliasse). Leer = alle Räume, in denen der Bot ist.
    rooms: tuple[str, ...] = ()
    # Wer darf kommandieren? Leer = alle Raum-Mitglieder.
    allowed_users: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ("!",)
    auto_join_invites: bool = True

    # --- Dateien -----------------------------------------------------------
    data_dir: str = "/data"

    # --- Medien ------------------------------------------------------------
    # Wer oft gespielt wird, bleibt gespeichert: ab `hot_after` Downloads (0 = nie speichern),
    # geloescht nach `hot_days` Tagen ohne Wiedergabe.
    hot_after: int = 5
    hot_days: float = 3.0
    # Laengere Titel werden abgelehnt (0 = kein Limit).
    max_song_seconds: int = 600
    enable_video: bool = True
    video_height: int = 480
    video_fps: float = 24.0
    video_bitrate: int = 600_000
    default_volume: int = 60
    # Call automatisch verlassen, wenn so lange nichts spielt (0 = nie).
    idle_leave_seconds: int = 60
    # Call verlassen, sobald der letzte Titel zu Ende ist und die Warteschlange leer.
    leave_when_done: bool = True
    # Nur wer selbst im Call ist, darf den Bot steuern.
    call_only_control: bool = True

    # --- Call / WebRTC -----------------------------------------------------
    auto_answer: bool = True
    # auto: bei Gruppen-Calls (confId) alle Raum-Mitglieder einladen,
    # all: immer alle einladen, off: nie selbst einladen.
    mesh_invite: str = "auto"
    # E2EE: verschluesselte Raeume (Gerate + Raum-Verschluesselung aktivieren).
    encryption_enabled: bool = True
    # Sekunden, die ICE-Self-Candidates maximal gesammelt werden.
    ice_gather_timeout: float = 8.0
    stun_uris: tuple[str, ...] = ("stun:stun.l.google.com:19302",)
    turn_uris: tuple[str, ...] = ()
    turn_username: str = ""
    turn_password: str = ""
    # Ephemere TURN-Credentials vom Homeserver holen (/_matrix/client/v3/voip/turnServer)
    turn_discovery: bool = True
    livekit_service_url: str = ""  # leer -> aus Call-Mitgliedern / well-known

    # --- yt-dlp ------------------------------------------------------------
    yt_player_client: str = ""  # z.B. "tv" oder "android", falls YouTube zickt
    yt_cookies_file: str = ""
    audio_format: str = "bestaudio/best"
    video_format: str = ""  # leer -> automatisch aus video_height abgeleitet

    # --- Sonstiges ---------------------------------------------------------
    log_level: str = "INFO"
    extra: dict[str, str] = field(default_factory=dict)
    # Wird nach dem Login gesetzt (eigene User-ID, um Selbst-Einladungen zu vermeiden).
    matrix_user_id: str = ""

    # ---------------------------------------------------------------- utils
    def __post_init__(self) -> None:
        self.prefixes = tuple(p if p else "!" for p in self.prefixes)
        if self.mesh_invite not in ("auto", "all", "off"):
            raise ConfigError(f"MESH_INVITE muss auto|all|off sein, ist {self.mesh_invite!r}")
        if not (0 <= self.default_volume <= 300):
            raise ConfigError("DEFAULT_VOLUME muss zwischen 0 und 300 liegen")

    @property
    def use_turn(self) -> bool:
        return bool(self.turn_uris) or self.turn_discovery

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.data_dir, "cache")

    @property
    def hot_dir(self) -> str:
        """Dauerhaft gemerkte Titel (oft gespielt); wird nie beim Start geleert."""
        return os.path.join(self.data_dir, "hot")

    @property
    def tmp_dir(self) -> str:
        """Nur für die gerade laufende Wiedergabe; wird nach jedem Titel geleert."""
        return os.path.join(self.data_dir, "tmp")

    @property
    def library_dir(self) -> str:
        return os.path.join(self.data_dir, "library")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "musikbot.sqlite3")

    @property
    def store_path(self) -> str:
        return os.path.join(self.data_dir, "matrix-store")

    def resolved_video_format(self) -> str:
        if self.video_format:
            return self.video_format
        h = self.video_height
        return (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}]/best"
        )

    # ---------------------------------------------------------------- load
    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            homeserver=_str("MATRIX_HOMESERVER"),
            user=_str("MATRIX_USER"),
            password=_str("MATRIX_PASSWORD"),
            token=_str("MATRIX_TOKEN"),
            device_id=_str("MATRIX_DEVICE_ID", "musikbot"),
            rooms=_list("MATRIX_ROOMS"),
            allowed_users=_list("ALLOWED_USERS"),
            prefixes=_list("BOT_PREFIXES", ("!",)),
            auto_join_invites=_bool("AUTO_JOIN_INVITES", True),
            data_dir=_str("DATA_DIR", "/data"),
            max_song_seconds=_int("MAX_SONG_SECONDS", 600),
            hot_after=_int("HOT_AFTER", 5),
            hot_days=_float("HOT_DAYS", 3.0),
            enable_video=_bool("ENABLE_VIDEO", True),
            video_height=_int("VIDEO_HEIGHT", 480),
            video_fps=_float("VIDEO_FPS", 24.0),
            video_bitrate=_int("VIDEO_BITRATE", 600_000),
            default_volume=_int("DEFAULT_VOLUME", 60),
            idle_leave_seconds=_int("IDLE_LEAVE_SECONDS", 60),
            leave_when_done=_bool("LEAVE_WHEN_DONE", True),
            call_only_control=_bool("CALL_ONLY_CONTROL", True),
            auto_answer=_bool("AUTO_ANSWER", True),
            mesh_invite=_str("MESH_INVITE", "auto").lower(),
            encryption_enabled=_bool("ENCRYPTION_ENABLED", True),
            ice_gather_timeout=_float("ICE_GATHER_TIMEOUT", 8.0),
            stun_uris=_list("STUN_URIS", ("stun:stun.l.google.com:19302",)),
            turn_uris=_list("TURN_URIS"),
            turn_username=_str("TURN_USERNAME"),
            turn_password=_str("TURN_PASSWORD"),
            turn_discovery=_bool("TURN_DISCOVERY", True),
            livekit_service_url=_str("LIVEKIT_SERVICE_URL"),
            yt_player_client=_str("YTDLP_PLAYER_CLIENT"),
            yt_cookies_file=_str("YTDLP_COOKIES_FILE"),
            audio_format=_str("YTDLP_FORMAT_AUDIO", "bestaudio/best"),
            video_format=_str("YTDLP_FORMAT_VIDEO"),
            log_level=_str("LOG_LEVEL", "INFO"),
        )
        missing = []
        if not cfg.homeserver:
            missing.append("MATRIX_HOMESERVER")
        if not cfg.user:
            missing.append("MATRIX_USER")
        if not cfg.password and not cfg.token:
            missing.append("MATRIX_PASSWORD oder MATRIX_TOKEN")
        if missing:
            raise ConfigError("Fehlende Umgebungsvariablen: " + ", ".join(missing))
        return cfg
