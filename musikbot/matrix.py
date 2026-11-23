"""Matrix-Anbindung. Login, Sync, E2EE-Pflege, Einladungen, Raum-Schluessel-Anfragen, Befehlsliste
und Bot-Profil fuer Diddy uebernimmt die Bot-Library diddybot (`self.dbot`). Hier bleibt, was nur der
Musikbot braucht: m.call.*-Signaling und Element Call (To-Device, Geraete, OpenID, State), angehaengte
Dateien, Direktnachrichten und Karten. Die Aufrufe von bot.py, calls.py und elementcall.py sind
unveraendert.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from typing import Awaitable, Callable, Optional

import diddybot
import nio
from nio.crypto.attachments import decrypt_attachment
from nio.responses import JoinedMembersResponse

from . import cards
from .config import Config

log = logging.getLogger("musikbot.matrix")

SYNC_TIMEOUT_MS = 30_000

PLAIN_MEDIA = (nio.RoomMessageAudio, nio.RoomMessageVideo, nio.RoomMessageFile)
ENCRYPTED_MEDIA = (nio.RoomEncryptedAudio, nio.RoomEncryptedVideo, nio.RoomEncryptedFile)
MEDIA_EVENTS = PLAIN_MEDIA + ENCRYPTED_MEDIA


VOICE_FLAG = "org.matrix.msc3245.voice"   # so markieren Element und Diddy Sprachnachrichten


RTC_KEYS_EVENT = "io.element.call.encryption_keys"
_rtc_key_listeners: list[Callable[[str, Optional[str], dict], None]] = []


def install_rtc_key_hook() -> None:
    """Element Call schickt jedem Teilnehmer seinen Medien-Schluessel als Olm-verschluesseltes
    To-Device. matrix-nio entschluesselt das, kennt den Typ aber nicht und verwirft ihn
    ("unsupported Olm event"). Hier abfangen: nio hat Absender und Payload schon geprueft
    (`_verify_olm_payload`), das Geraet kommt aus dem Curve25519-Schluessel der Olm-Sitzung."""
    from nio.crypto import Olm
    if getattr(Olm, "_musikbot_rtc_hook", False):
        return
    original = Olm._handle_olm_event

    def handle(self, sender, sender_key, payload):
        if isinstance(payload, dict) and payload.get("type") == RTC_KEYS_EVENT:
            device = None
            try:
                for dev in self.device_store.active_user_devices(sender):
                    if dev.curve25519 == sender_key:
                        device = dev.id
                        break
            except Exception:  # noqa: BLE001
                log.debug("Geraet zum Medien-Schluessel nicht gefunden", exc_info=True)
            for cb in list(_rtc_key_listeners):
                try:
                    cb(sender, device, dict(payload.get("content") or {}))
                except Exception:  # noqa: BLE001
                    log.exception("Medien-Schluessel verarbeiten fehlgeschlagen")
            return None
        return original(self, sender, sender_key, payload)

    Olm._handle_olm_event = handle
    Olm._musikbot_rtc_hook = True


def on_rtc_key(cb: Callable[[str, Optional[str], dict], None]) -> None:
    """`cb(sender, device_id|None, content)` fuer jeden empfangenen Element-Call-Schluessel."""
    install_rtc_key_hook()
    _rtc_key_listeners.append(cb)


def is_voice_message(content: dict) -> bool:
    """True fuer Sprachnachrichten (MSC3245) – die sind kein Musikwunsch."""
    return isinstance(content, dict) and VOICE_FLAG in content


def adopt_legacy_token(cfg: Config) -> bool:
    """Update vom Bot vor diddybot: dessen Token (Datei `access_token` im Store) wird in die
    `credentials.json` der Library uebernommen. Gleiches Geraet, gleiche E2EE-Schluessel, kein neues
    Geraet. Ist der Token ungueltig, meldet sich der Bot mit dem Passwort neu an."""
    legacy = os.path.join(cfg.store_path, "access_token")
    target = os.path.join(cfg.store_path, "credentials.json")
    if cfg.token or os.path.exists(target) or not os.path.exists(legacy):
        return False
    with open(legacy, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        return False
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"homeserver": cfg.homeserver.rstrip("/"), "user_id": cfg.user,
                   "device_id": cfg.device_id, "access_token": token}, fh)
    log.info("Token des frueheren Bots uebernommen (Geraet %s)", cfg.device_id)
    return True


class Matrix:
    def __init__(self, cfg: Config, profile: Optional[dict] = None):
        self.cfg = cfg
        os.makedirs(cfg.store_path, exist_ok=True)
        adopt_legacy_token(cfg)
        # Die Library: Login, Sync, E2EE, Einladungen, Befehlsliste + Profil fuer Diddy.
        # `help_command=False`: bot.py hat eine eigene Hilfe-Karte.
        self.dbot = diddybot.Bot(
            cfg.homeserver, cfg.user, password=cfg.password, access_token=cfg.token,
            device_id=cfg.device_id, store_path=cfg.store_path,
            prefix=cfg.prefixes[0] if cfg.prefixes else "!", encryption=cfg.encryption_enabled,
            auto_join=cfg.auto_join_invites, help_command=False, **(profile or {}))
        self._room_ids: list[str] = []
        self._devices: dict[str, dict[str, list[str]]] = {}  # room -> user -> devs
        self._direct: dict[str, list[str]] = {}   # m.direct: Nutzer -> Privatchat-Raeume

        # Callbacks (von bot.py gesetzt):
        # room_id, sender, Dateiname, MIME-Typ, Inhalt (bereits entschluesselt)
        self.on_media: Callable[[str, str, str, str, bytes], Awaitable[None]] | None = None
        self.on_call: Callable[[Optional[str], str, Optional[str], dict], Awaitable[None]] | None = None  # room_id|None, sender, device, content
        self.on_member_join: Callable[[str, str], Awaitable[None]] | None = None    # room_id, user_id
        self.on_room_left: Callable[[str], Awaitable[None]] | None = None           # room_id (Bot verlassen)
        # Element Call: room_id, sender, state_key, content
        self.on_rtc_member: Callable[[str, str, str, dict], Awaitable[None]] | None = None

        # Rohe nio-Ereignisse, die die Library nicht kennt (Anrufe, Dateien, Mitglieder)
        self.dbot.add_event_callback(self._event_cb, nio.Event)
        self.dbot.add_to_device_callback(self._to_device_cb, nio.ToDeviceEvent)

    @property
    def client(self) -> nio.AsyncClient:
        """Der nio-Client (existiert nach `login()`)."""
        return self._client if self._client is not None else self.dbot.client

    @client.setter
    def client(self, value: object) -> None:      # nur fuer Tests: Attrappe statt Verbindung
        self._client = value

    _client = None

    # ------------------------------------------------------------------ login / sync
    async def login(self) -> None:
        await self.dbot.login()
        uid = self.dbot.user_id
        self.cfg.matrix_user_id = uid
        if uid != self.cfg.user and self.cfg.user.startswith("@"):
            self.cfg.user = uid or self.cfg.user     # ggf. anderer mxid -- Gleichstand fuer Filter
        if self.cfg.encryption_enabled and self.client.olm is not None:
            log.info("E2EE aktiv (Geraeteschluessel werden hochgeladen)")

    async def run(self) -> None:
        """Sync-Loop (mit Wiederanlauf) bis `stop()`; `login()` ist schon gelaufen."""
        await self.dbot.start(login=False)

    def stop(self) -> None:
        self.dbot.stop()

    # ------------------------------------------------------------------ callbacks
    async def _event_cb(self, room: nio.MatrixRoom, event) -> None:
        src = getattr(event, "source", {}) or {}
        if src.get("type") == "org.matrix.msc3401.call.member" and "state_key" in src:
            if self.on_rtc_member:
                await self.on_rtc_member(room.room_id, src.get("sender", ""),
                                         src["state_key"], src.get("content") or {})
            return
        if getattr(event, "sender", None) in (self.cfg.user, self.cfg.matrix_user_id):
            return
        if isinstance(event, MEDIA_EVENTS):
            await self._media_cb(room, event)
        elif isinstance(event, nio.UnknownEncryptedEvent):
            log.warning("Verschluesseltes Event in %s nicht entschluesselbar "
                        "(Room-Key fehlt).", room.room_id)
        elif isinstance(event, (nio.CallInviteEvent, nio.CallAnswerEvent,
                                nio.CallCandidatesEvent, nio.CallHangupEvent,
                                nio.UnknownEvent, nio.UnknownEncryptedEvent)):
            content = (getattr(event, "source", {}) or {}).get("content") or {}
            etype = (getattr(event, "source", {}) or {}).get("type", "")
            if not etype.startswith("m.call."):
                return
            content = dict(content)
            content["type"] = etype
            if self.on_call:
                await self.on_call(room.room_id, event.sender, None, content)
        await self._membership_cb(room, event)

    async def _media_cb(self, room: nio.MatrixRoom, event) -> None:
        """Angehaengte Datei laden (ggf. entschluesseln) und an den Bot geben."""
        if not self.on_media:
            return
        content = (getattr(event, "source", {}) or {}).get("content") or {}
        if is_voice_message(content):
            return                   # Sprachnachricht unter Menschen – nicht fuer den Bot
        info = content.get("info") or {}
        mime = str(info.get("mimetype") or getattr(event, "mimetype", "") or "")
        name = str(content.get("filename") or event.body or "datei")
        size = info.get("size")
        if isinstance(size, int) and size > self.cfg.max_upload_mb * 1024 * 1024:
            await self.on_media(room.room_id, event.sender, name, mime, b"")
            return
        try:
            resp = await self.client.download(mxc=event.url)
            if not isinstance(resp, nio.MemoryDownloadResponse):
                log.warning("Datei-Download fehlgeschlagen: %s", resp)
                return
            data = resp.body
            if isinstance(event, ENCRYPTED_MEDIA):
                data = decrypt_attachment(data, event.key["k"], event.hashes["sha256"], event.iv)
        except Exception:
            log.exception("Datei %s konnte nicht geladen werden", name)
            return
        await self.on_media(room.room_id, event.sender, name, mime, data)

    async def _membership_cb(self, room: nio.MatrixRoom, event) -> None:
        source = getattr(event, "source", {}) or {}
        etype = source.get("type", "")
        if etype != "m.room.member":
            return
        state_key = source.get("state_key", "")
        membership = (source.get("content") or {}).get("membership", "")
        me = self.cfg.user
        if state_key == me and membership in ("leave", "ban"):
            if self.on_room_left:
                await self.on_room_left(room.room_id)
        elif state_key and membership == "join" and state_key != me:
            if self.on_member_join:
                await self.on_member_join(room.room_id, state_key)
            self._devices.pop(room.room_id, None)

    def _to_device_cb(self, event) -> None:
        etype = getattr(event, "type", "") or \
            (getattr(event, "source", {}) or {}).get("type", "")
        if not etype.startswith("m.call."):
            return
        source = getattr(event, "source", {}) or {}
        content = dict(source.get("content") or {})
        content["type"] = etype
        if self.on_call:
            asyncio.ensure_future(self.on_call(
                None, event.sender, source.get("sender_device"), content))

    async def _join(self, room_id: str) -> None:
        resp = await self.client.join(room_id)
        if isinstance(resp, nio.JoinResponse):
            log.info("Raum beigetreten: %s", room_id)
        else:
            log.warning("Raum-Beitritt %s fehlgeschlagen: %s", room_id, resp)

    # ------------------------------------------------------------------ Start
    async def prepare_rooms(self) -> list[str]:
        """Konfigurierte Raeume aufloesen und betreten; liefert Raum-IDs."""
        ids: list[str] = []
        for spec in self.cfg.rooms:
            rid = await self.resolve_room(spec)
            if rid:
                ids.append(rid)
            else:
                log.error("Raum nicht aufgeloest: %s", spec)
        joined = await self.client.joined_rooms()
        if isinstance(joined, nio.JoinedRoomsResponse):
            have = set(joined.rooms)
            for rid in ids:
                if rid not in have:
                    await self._join(rid)
        self._room_ids = ids
        return ids

    async def resolve_room(self, spec: str) -> Optional[str]:
        if spec.startswith("!"):
            return spec
        if spec.startswith("#"):
            resp = await self.client.room_resolve_alias(spec)
            if isinstance(resp, nio.RoomResolveAliasResponse):
                return resp.room_id
            return None
        # wie ein Alias behandeln
        resp = await self.client.room_resolve_alias(f"#{spec}")
        if isinstance(resp, nio.RoomResolveAliasResponse):
            return resp.room_id
        return None

    # ------------------------------------------------------------------ senden
    async def send_text(self, room_id: str, body: str) -> None:
        await self.send_room_event(room_id, "m.room.message",
                                   {"msgtype": "m.text", "body": body})

    async def send_notice(self, room_id: str, body: str) -> None:
        await self.send_room_event(
            room_id, "m.room.message", {"msgtype": "m.notice", "body": body})

    async def send_room_event(self, room_id: str, evtype: str,
                              content: dict) -> str | None:
        """Sendet ein Raum-Event, gibt dessen event_id zurueck (None bei Fehler)."""
        try:
            resp = await self.client.room_send(
                room_id, evtype, content, ignore_unverified_devices=True)
            if isinstance(resp, nio.ErrorResponse):
                log.error("Senden (%s) fehlgeschlagen: %s", evtype, resp)
                return None
            return getattr(resp, "event_id", None)
        except Exception:
            log.exception("Senden (%s) fehlgeschlagen", evtype)
            return None

    def _is_dm_with(self, room_id: str, user_id: str) -> bool:
        room = self.client.rooms.get(room_id)
        if room is None:
            return False
        me = (self.cfg.matrix_user_id or self.cfg.user).lower()
        want = user_id.lower()
        members = {u.lower() for u in list(room.users) + list(getattr(room, "invited_users", {}))}
        return want in members and members <= {me, want}

    def find_dm(self, user_id: str) -> Optional[str]:
        """Bestehender Zweier-Raum mit `user_id` (beigetreten oder eingeladen).
        Bevorzugt die Raeume aus m.direct (zuletzt angelegter zuerst)."""
        for room_id in reversed(self._direct.get(user_id.lower(), [])):
            if self._is_dm_with(room_id, user_id):
                return room_id
        for room_id in self.client.rooms:
            if self._is_dm_with(room_id, user_id):
                return room_id
        return None

    async def load_direct(self) -> None:
        """m.direct (Liste der Privatchats je Nutzer) vom Server holen."""
        from urllib.parse import quote
        me = self.cfg.matrix_user_id or self.cfg.user
        try:
            data = await self.http_json("GET", self._cs(f"/user/{quote(me)}/account_data/m.direct"))
        except Exception:
            data = {}
        self._direct = {u.lower(): list(rooms) for u, rooms in data.items() if isinstance(rooms, list)}

    async def send_dm(self, user_id: str, body: str, html: str = "") -> bool:
        """Direktnachricht schicken (nutzt einen vorhandenen Privatchat, sonst legt die Library
        einen verschluesselten an)."""
        try:
            room_id = await self.dbot._t.dm_room(user_id, "Musikbot")
            await self.dbot.send(room_id, body, html=html or None, notice=False)
        except Exception:
            log.exception("Direktnachricht an %s fehlgeschlagen", user_id)
            return False
        return True

    async def send_card(self, room_id: str, content: dict) -> str | None:
        """Karte fuer Diddy senden (siehe cards.py). event_id oder None."""
        return await self.send_room_event(room_id, "m.room.message", content)

    async def upload_image(self, data: bytes, content_type: str, name: str) -> str | None:
        """Bild (z. B. Titelbild) auf den Medien-Server laden. mxc://-Adresse oder None."""
        try:
            resp, _keys = await self.client.upload(
                io.BytesIO(data), content_type=content_type, filename=name, filesize=len(data))
            if isinstance(resp, nio.UploadResponse):
                return resp.content_uri
            log.warning("Bild-Upload fehlgeschlagen: %s", resp)
        except Exception:
            log.exception("Bild-Upload fehlgeschlagen")
        return None

    async def edit_card(self, room_id: str, event_id: str, content: dict) -> bool:
        """Karte per Bearbeitung (m.replace) aktualisieren. True, wenn gesendet."""
        edit = cards.edit_content(event_id, content)
        return await self.send_room_event(room_id, "m.room.message", edit) is not None

    async def send_to_device(self, recipients: list[tuple[str, str]],
                             evtype: str, content: dict) -> None:
        for uid, dev in recipients:
            try:
                msg = nio.ToDeviceMessage(evtype, uid, dev, content)
                resp = await self.client.to_device(msg)
                if isinstance(resp, nio.ErrorResponse):
                    log.warning("To-Device %s -> %s/%s: %s",
                                evtype, uid, dev, resp)
            except Exception:
                log.exception("To-Device %s -> %s/%s", evtype, uid, dev)

    # ------------------------------------------------------------------ Gerate
    async def refresh_devices(self, room_id: str) -> dict[str, list[str]]:
        """user_id -> [device_id,...] fuer alle Mitglieder eines Raums."""
        members = await self.client.joined_members(room_id)
        if not isinstance(members, JoinedMembersResponse):
            log.warning("Mitglieder von %s nicht ladbar: %s", room_id, members)
            return {}
        users = [m.user_id for m in members.members
                 if m.user_id != self.cfg.user]
        devices: dict[str, list[str]] = {}
        if self.client.olm is not None:
            # nio wirft LocalProtocolError, wenn keine Abfrage noetig ist;
            # die bekannten Geraete stehen dann schon im device_store.
            if self.client.should_query_keys:
                await self.client.keys_query()
            store = self.client.device_store
            for uid in users:
                devices[uid] = sorted(d.id for d in store.active_user_devices(uid))
        else:
            devices = await self._query_devices_raw(users)
        self._devices[room_id] = devices
        return devices

    async def _query_devices_raw(self, users: list[str]) -> dict[str, list[str]]:
        """Geraeteliste ohne E2EE (nio.keys_query braucht Olm)."""
        if not users:
            return {}
        import aiohttp
        url = f"{self.client.homeserver}/_matrix/client/v3/keys/query"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"device_keys": {u: [] for u in users}},
                                  headers={"Authorization": f"Bearer {self.client.access_token}"}) as r:
                    data = await r.json()
        except Exception:
            log.warning("keys/query fehlgeschlagen", exc_info=True)
            return {}
        dk = data.get("device_keys") or {}
        return {u: sorted((dk.get(u) or {}).keys()) for u in users}

    def devices_of_room(self, room_id: str) -> dict[str, list[str]]:
        """Nicht-blockierender Zugriff auf den zuletzt geladenen Stand."""
        return self._devices.get(room_id, {})

    async def ensure_room_state(self, room_id: str) -> None:
        await self.refresh_devices(room_id)

    # ------------------------------------------------------------------ Element Call
    async def http_json(self, method: str, url: str, body: Optional[dict] = None,
                        *, auth: bool = True) -> dict:
        import aiohttp
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.client.access_token}"
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, json=body, headers=headers) as r:
                text = await r.text()
                if r.status >= 300:
                    raise RuntimeError(f"{method} {url} -> HTTP {r.status}: {text[:300]}")
                import json as _json
                return _json.loads(text) if text else {}

    def _cs(self, path: str) -> str:
        return f"{self.client.homeserver}/_matrix/client/v3{path}"

    async def openid_token(self) -> dict:
        from urllib.parse import quote
        uid = self.cfg.matrix_user_id or self.cfg.user
        return await self.http_json(
            "POST", self._cs(f"/user/{quote(uid)}/openid/request_token"), {})

    async def wellknown_livekit(self) -> Optional[str]:
        try:
            data = await self.http_json(
                "GET", f"{self.client.homeserver}/.well-known/matrix/client", auth=False)
        except Exception:
            log.warning("well-known nicht ladbar", exc_info=True)
            return None
        for f in data.get("org.matrix.msc4143.rtc_foci") or []:
            if f.get("type") == "livekit" and f.get("livekit_service_url"):
                return f["livekit_service_url"]
        return None

    def room_encrypted(self, room_id: str) -> bool:
        room = self.client.rooms.get(room_id)
        return bool(room and room.encrypted)

    async def get_state_events(self, room_id: str, evtype: str) -> dict[str, dict]:
        """state_key -> content (+ '_sender') fuer alle State-Events eines Typs."""
        from urllib.parse import quote
        events = await self.http_json("GET", self._cs(f"/rooms/{quote(room_id)}/state"))
        out: dict[str, dict] = {}
        for e in events or []:
            if e.get("type") != evtype:
                continue
            c = dict(e.get("content") or {})
            if c:
                c["_sender"] = e.get("sender", "")
            out[e.get("state_key", "")] = c
        return out

    async def put_state(self, room_id: str, evtype: str, state_key: str,
                        content: dict) -> None:
        resp = await self.client.room_put_state(room_id, evtype, content,
                                                state_key=state_key)
        if not isinstance(resp, nio.RoomPutStateResponse):
            raise RuntimeError(f"State {evtype} in {room_id}: {resp}")

    async def send_encrypted_to_device(self, targets: list[tuple[str, str]],
                                       evtype: str, content: dict) -> list[tuple[str, str]]:
        """Olm-verschluesseltes To-Device an (user, device). Gibt Erfolge zurueck."""
        c = self.client
        if c.olm is None:
            return []
        def _dev(u: str, d: str):
            try:
                return c.device_store[u].get(d)
            except KeyError:
                return None

        if c.should_query_keys:
            await c.keys_query()
        # Geraete, die wir noch nicht kennen, gezielt abfragen
        unknown = [u for u, d in targets if _dev(u, d) is None]
        if unknown:
            c.olm.users_for_key_query.update(unknown)
            try:
                await c.keys_query()
            except Exception:
                log.debug("keys_query", exc_info=True)
        need_claim: dict[str, list[str]] = {}
        for u, d in targets:
            dev = _dev(u, d)
            if dev and not c.olm.session_store.get(dev.curve25519):
                need_claim.setdefault(u, []).append(d)
        if need_claim:
            try:
                await c.keys_claim(need_claim)
            except Exception:
                log.warning("Olm-Keys claimen fehlgeschlagen", exc_info=True)
        ok: list[tuple[str, str]] = []
        for u, d in targets:
            dev = _dev(u, d)
            session = c.olm.session_store.get(dev.curve25519) if dev else None
            if dev is None or session is None:
                log.warning("Kein Olm-Kanal zu %s/%s", u, d)
                continue
            payload = c.olm._olm_encrypt(session, dev, evtype, content)
            resp = await c.to_device(nio.ToDeviceMessage("m.room.encrypted", u, d, payload))
            if isinstance(resp, nio.ToDeviceResponse):
                ok.append((u, d))
            else:
                log.warning("To-Device an %s/%s: %s", u, d, resp)
        return ok

    # ------------------------------------------------------------------ TURN
    async def fetch_turn(self) -> Optional[dict]:
        """Ephemere TURN-Credentials vom Homeserver."""
        if not self.cfg.turn_discovery:
            return None
        try:
            url = f"{self.client.homeserver}/_matrix/client/v3/voip/turnServer"
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={
                        "Authorization": f"Bearer {self.client.access_token}"}
                ) as r:
                    if r.status != 200:
                        log.warning("turnServer HTTP %s", r.status)
                        return None
                    return await r.json()
        except Exception:
            log.warning("TURN-Discovery fehlgeschlagen", exc_info=True)
            return None
