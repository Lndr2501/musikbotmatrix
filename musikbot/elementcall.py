"""Element Call (MatrixRTC + LiveKit) als Sender.

Der Bot tritt einem laufenden Element-Call bei, so wie Element Web/X es tut:

1. OpenID-Token vom Homeserver -> LiveKit-JWT vom lk-jwt-service (/sfu/get)
2. Verbindung zur LiveKit-SFU, Audio (und optional Video) aus dem Player
   veroeffentlichen
3. State-Event ``org.matrix.msc3401.call.member`` setzen (Bot ist "im Call")
4. In verschluesselten Raeumen: eigener Medien-Schluessel (per-participant
   E2EE, HKDF) per Olm-verschluesseltem To-Device
   ``io.element.call.encryption_keys`` an jedes Call-Geraet schicken

Protokoll nachgebaut nach matrix-js-sdk ``src/matrixrtc`` (MembershipManager,
ToDeviceKeyTransport, CallMembership) und Element Call ``MatrixKeyProvider``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import TYPE_CHECKING, Optional

import numpy as np

from .hub import Sink
from .media import silence_block
from .player import SAMPLE_RATE

if TYPE_CHECKING:
    from .config import Config
    from .matrix import Matrix
    from .player import Player

log = logging.getLogger("musikbot.elementcall")

MEMBER_EVENT = "org.matrix.msc3401.call.member"
KEYS_EVENT = "io.element.call.encryption_keys"
EXPIRES_MS = 4 * 60 * 60 * 1000          # wie matrix-js-sdk DEFAULT_EXPIRE_DURATION
REFRESH_S = 3 * 60 * 60                  # Membership rechtzeitig erneuern
BLOCK_SAMPLES = 960                      # 20 ms bei 48 kHz
# Musik statt Sprache: ohne feste Vorgabe nimmt das LiveKit-SDK Sprach-Defaults
# (niedrige Opus-Bitrate, DTX schneidet leise Stellen weg) -> klingt dumpf/komisch.
AUDIO_BITRATE = 128_000
STATS_S = 30                             # Aussetzer-Statistik so oft loggen


def audio_publish_options():
    from livekit import rtc
    from livekit.rtc._proto import room_pb2
    return rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_MICROPHONE,
        audio_encoding=room_pb2.AudioEncoding(max_bitrate=AUDIO_BITRATE),
        dtx=False, red=True)


def member_state_key(user_id: str, device_id: str) -> str:
    return f"_{user_id}_{device_id}_m.call"


def active_members(state: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """state_key -> content  =>  (user, device) -> content (nur aktive)."""
    now = time.time() * 1000
    out: dict[tuple[str, str], dict] = {}
    for _sk, content in state.items():
        if not content or not content.get("device_id"):
            continue
        if content.get("application", "m.call") != "m.call":
            continue
        created = content.get("created_ts")
        expires = content.get("expires")
        if created and expires and created + expires < now:
            continue
        user = content.get("_sender")
        if user:
            out[(user, content["device_id"])] = content
    return out


class ElementCall:
    def __init__(self, cfg: "Config", matrix: "Matrix", player: "Player") -> None:
        self.cfg = cfg
        self.matrix = matrix
        self.player = player
        self.room_id: Optional[str] = None
        self.members: dict[str, dict[str, dict]] = {}   # room -> state_key -> content(+_sender)
        self._lk = None
        self._sink: Optional[Sink] = None
        self._key: Optional[bytes] = None
        self._keyed: set[tuple[str, str]] = set()
        self._tasks: list[asyncio.Task] = []
        self._service_url: Optional[str] = None
        self._lock = asyncio.Lock()
        # Wird vom Bot gesetzt: True = nicht verlassen, auch wenn der Call leer ist (24/7).
        self.keep_alive = None

    # ------------------------------------------------------------ Zustand
    @property
    def active(self) -> bool:
        return self.room_id is not None

    def others_in_call(self, room_id: str) -> dict[tuple[str, str], dict]:
        me = (self.cfg.matrix_user_id or self.cfg.user, self.cfg.device_id)
        return {k: v for k, v in active_members(self.members.get(room_id, {})).items()
                if k != me}

    def livekit_others(self) -> Optional[int]:
        """Wie viele andere gerade wirklich mit dem Medienserver verbunden sind (None: nicht verbunden).
        Verlaesslicher als die call.member-Eintraege, die nach einem Absturz stundenlang stehen bleiben."""
        if self._lk is None:
            return None
        try:
            return len(self._lk.remote_participants)
        except Exception:  # noqa: BLE001
            return None

    def users_in_call(self, room_id: str) -> set[str]:
        """Nutzer-IDs (klein geschrieben) aller aktiven Teilnehmer ausser dem Bot."""
        return {u.lower() for (u, _d) in self.others_in_call(room_id)}

    async def load_members(self, room_id: str) -> None:
        state = await self.matrix.get_state_events(room_id, MEMBER_EVENT)
        self.members[room_id] = state

    async def on_member_event(self, room_id: str, sender: str, state_key: str,
                              content: dict) -> None:
        """Von matrix.py fuer jedes call.member-State-Event aufgerufen."""
        c = dict(content or {})
        if c:
            c["_sender"] = sender
        self.members.setdefault(room_id, {})[state_key] = c
        if self.room_id != room_id:
            return
        others = self.others_in_call(room_id)
        if not others and not (self.keep_alive and self.keep_alive()):
            log.info("Element Call in %s leer – Bot verlaesst den Call", room_id)
            asyncio.ensure_future(self.leave())
            return
        if self._key is not None:
            new = [k for k in others if k not in self._keyed]
            if new:
                await self._send_key(new)

    # ------------------------------------------------------------ Beitreten
    async def join(self, room_id: str) -> None:
        async with self._lock:
            if self.room_id == room_id:
                return
            if self.room_id is not None:
                await self._leave_locked()
            await self._join_locked(room_id)

    async def _join_locked(self, room_id: str) -> None:
        from livekit import rtc  # erst hier: schwere native Lib

        await self.load_members(room_id)
        service_url = await self._resolve_service_url(room_id)
        if not service_url:
            raise RuntimeError("Kein LiveKit-Dienst gefunden (well-known rtc_foci fehlt)")
        openid = await self.matrix.openid_token()
        sfu = await self.matrix.http_json(
            "POST", service_url.rstrip("/") + "/sfu/get",
            {"room": room_id, "openid_token": openid,
             "device_id": self.cfg.device_id}, auth=False)
        url, jwt = sfu["url"], sfu["jwt"]
        identity = f"{self.cfg.matrix_user_id or self.cfg.user}:{self.cfg.device_id}"

        encrypted = self.matrix.room_encrypted(room_id)
        options = rtc.RoomOptions(auto_subscribe=False)
        if encrypted:
            self._key = os.urandom(32)
            options = rtc.RoomOptions(
                auto_subscribe=False,
                encryption=rtc.E2EEOptions(
                    key_provider_options=rtc.KeyProviderOptions(
                        ratchet_window_size=10, key_ring_size=256,
                        key_derivation_function=rtc.KeyDerivationFunction.HKDF),
                    encryption_type=rtc.EncryptionType.GCM))
        else:
            self._key = None

        room = rtc.Room()
        await room.connect(url, jwt, options)
        self._lk = room
        lid = room.local_participant.identity
        if lid != identity:
            log.warning("LiveKit-Identity %s statt erwartet %s", lid, identity)
        if encrypted:
            room.e2ee_manager.key_provider.set_key(lid, self._key, 0)

        self._sink = self.player.hub.new_sink()
        audio_src = rtc.AudioSource(SAMPLE_RATE, 2)
        atrack = rtc.LocalAudioTrack.create_audio_track("musik", audio_src)
        await room.local_participant.publish_track(atrack, audio_publish_options())
        self._tasks = [asyncio.create_task(self._pump_audio(audio_src), name="ec-audio")]

        if self.player.video_enabled:
            h = int(self.cfg.video_height) // 2 * 2
            w = (h * 16 // 9) // 2 * 2
            video_src = rtc.VideoSource(w, h)
            vtrack = rtc.LocalVideoTrack.create_video_track("video", video_src)
            await room.local_participant.publish_track(
                vtrack, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA))
            self._tasks.append(asyncio.create_task(
                self._pump_video(video_src, w, h), name="ec-video"))

        self.room_id = room_id
        await self._send_membership(service_url)
        self._tasks.append(asyncio.create_task(self._refresh(service_url), name="ec-refresh"))
        self._keyed.clear()
        if encrypted:
            await self._send_key(list(self.others_in_call(room_id)))
        log.info("Element Call beigetreten: %s als %s (E2EE %s, %d andere)",
                 room_id, lid, "an" if encrypted else "aus",
                 len(self.others_in_call(room_id)))

    async def _resolve_service_url(self, room_id: str) -> Optional[str]:
        if self.cfg.livekit_service_url:
            return self.cfg.livekit_service_url
        # 1) Fokus, den die anderen Teilnehmer nutzen
        for content in active_members(self.members.get(room_id, {})).values():
            for f in content.get("foci_preferred") or []:
                if f.get("type") == "livekit" and f.get("livekit_service_url"):
                    return f["livekit_service_url"]
        # 2) .well-known des Homeservers
        if self._service_url is None:
            self._service_url = await self.matrix.wellknown_livekit()
        return self._service_url

    async def _send_membership(self, service_url: str) -> None:
        user = self.cfg.matrix_user_id or self.cfg.user
        content = {
            "application": "m.call",
            "call_id": "",
            "scope": "m.room",
            "device_id": self.cfg.device_id,
            "membershipID": f"{user}:{self.cfg.device_id}",
            "expires": EXPIRES_MS,
            "created_ts": int(time.time() * 1000),
            "m.call.intent": "video" if self.player.video_enabled else "audio",
            "focus_active": {"type": "livekit", "focus_selection": "multi_sfu"},
            "foci_preferred": [{"type": "livekit",
                                "livekit_service_url": service_url,
                                "livekit_alias": self.room_id}],
        }
        await self.matrix.put_state(self.room_id, MEMBER_EVENT,
                                    member_state_key(user, self.cfg.device_id), content)

    async def _refresh(self, service_url: str) -> None:
        while True:
            await asyncio.sleep(REFRESH_S)
            try:
                await self._send_membership(service_url)
            except Exception:
                log.warning("Membership-Erneuerung fehlgeschlagen", exc_info=True)

    async def _send_key(self, targets: list[tuple[str, str]]) -> None:
        if self._key is None or not targets:
            return
        user = self.cfg.matrix_user_id or self.cfg.user
        member_id = f"{user}:{self.cfg.device_id}"
        media_key = {"index": 0,
                     "key": base64.b64encode(self._key).decode()}
        content = {
            "room_id": self.room_id,
            "member_id": member_id,
            "media_key": media_key,
            "keys": media_key,
            "member": {"claimed_device_id": self.cfg.device_id, "id": member_id},
            "sent_ts": int(time.time() * 1000),
        }
        ok = await self.matrix.send_encrypted_to_device(targets, KEYS_EVENT, content)
        self._keyed.update(ok)
        missing = [t for t in targets if t not in ok]
        log.info("Medien-Schluessel an %d Geraete verteilt%s", len(ok),
                 f" (fehlgeschlagen: {missing})" if missing else "")

    # ------------------------------------------------------------ Medien
    async def _pump_audio(self, source) -> None:
        from livekit import rtc
        loop = asyncio.get_running_loop()
        anchor = loop.time()
        stats_at = anchor + STATS_S
        gaps = late = 0
        try:
            while True:
                block = self._sink.take_audio() if self._sink else None
                if block is None:
                    if getattr(self.player, "state", "") == "playing":
                        gaps += 1               # Puffer leer, obwohl Musik laeuft
                    block = silence_block()
                block = np.ascontiguousarray(block, dtype=np.int16)
                samples = block.shape[-1] // 2
                frame = rtc.AudioFrame(block.tobytes(), SAMPLE_RATE, 2, samples)
                await source.capture_frame(frame)
                anchor += samples / SAMPLE_RATE
                delay = anchor - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:
                    anchor = loop.time()
                    late += 1
                now = loop.time()
                if now >= stats_at:
                    dropped = self._sink.dropped_audio if self._sink else 0
                    if gaps or late or dropped:
                        log.warning("Audio-Aussetzer in %ds: %d leere Bloecke (je 20 ms), "
                                    "%d Takt-Spruenge, %d verworfen", STATS_S, gaps, late, dropped)
                        if self._sink:
                            self._sink.dropped_audio = 0
                    gaps = late = 0
                    stats_at = now + STATS_S
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Audio-Pumpe abgestuerzt")

    async def _pump_video(self, source, w: int, h: int) -> None:
        from livekit import rtc
        loop = asyncio.get_running_loop()
        fps = max(1.0, float(self.cfg.video_fps))
        anchor = loop.time()
        last = None
        black = rtc.VideoFrame(w, h, rtc.VideoBufferType.RGBA, bytes(w * h * 4))
        try:
            while True:
                frame = self._sink.take_video() if self._sink else None
                if frame is not None:
                    try:
                        img = frame.reformat(width=w, height=h, format="rgba")
                        last = rtc.VideoFrame(w, h, rtc.VideoBufferType.RGBA,
                                              img.to_ndarray().tobytes())
                    except Exception:
                        log.debug("Videoframe-Konvertierung fehlgeschlagen", exc_info=True)
                if last is None:                # Beitritt mitten im Titel: Titelbild statt Schwarz
                    cover = getattr(self.player, "cover_frame", None)
                    if cover is not None:
                        try:
                            img = cover.reformat(width=w, height=h, format="rgba")
                            last = rtc.VideoFrame(w, h, rtc.VideoBufferType.RGBA,
                                                  img.to_ndarray().tobytes())
                        except Exception:
                            log.debug("Titelbild-Konvertierung fehlgeschlagen", exc_info=True)
                source.capture_frame(last or black)
                anchor += 1.0 / fps
                delay = anchor - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:
                    anchor = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Video-Pumpe abgestuerzt")

    # ------------------------------------------------------------ Verlassen
    async def leave(self) -> None:
        async with self._lock:
            await self._leave_locked()

    async def _leave_locked(self) -> None:
        room_id, self.room_id = self.room_id, None
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self._sink is not None:
            self.player.hub.drop_sink(self._sink)
            self._sink = None
        if self._lk is not None:
            try:
                await self._lk.disconnect()
            except Exception:
                log.debug("LiveKit-Trennung", exc_info=True)
            self._lk = None
        if room_id:
            user = self.cfg.matrix_user_id or self.cfg.user
            try:
                await self.matrix.put_state(room_id, MEMBER_EVENT,
                                            member_state_key(user, self.cfg.device_id), {})
            except Exception:
                log.warning("Membership entfernen fehlgeschlagen", exc_info=True)
            log.info("Element Call verlassen: %s", room_id)
        self._key = None
        self._keyed.clear()
