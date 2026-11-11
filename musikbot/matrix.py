"""Matrix-Anbindung: Login, Sync-Loop, Empfinden/Senden von Events.

Duenn um matrix-nio: Der Bot laeuft als normaler Nutzer, verschluesselt
(E2EE) nach Moeglichkeit, versteht Textbefehle und m.call.*-Signaling
sowohl als Raum-Event als auch als To-Device-Nachricht.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Awaitable, Callable, Optional

import nio
from nio.crypto.attachments import decrypt_attachment
from nio.responses import (JoinedMembersResponse, KeysQueryResponse,
                          LoginResponse)

from . import cards
from .config import Config

log = logging.getLogger("musikbot.matrix")

SYNC_TIMEOUT_MS = 30_000

PLAIN_MEDIA = (nio.RoomMessageAudio, nio.RoomMessageVideo, nio.RoomMessageFile)
ENCRYPTED_MEDIA = (nio.RoomEncryptedAudio, nio.RoomEncryptedVideo, nio.RoomEncryptedFile)
MEDIA_EVENTS = PLAIN_MEDIA + ENCRYPTED_MEDIA


class Matrix:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # nio nutzt store_path als VERZEICHNIS und legt dort die E2EE-DBs ab;
        # das Verzeichnis wird von nio nicht selbst angelegt.
        os.makedirs(cfg.store_path, exist_ok=True)
        config = nio.AsyncClientConfig(
            encryption_enabled=cfg.encryption_enabled,
            store_sync_tokens=True,
        )
        self.client = nio.AsyncClient(
            cfg.homeserver, cfg.user, device_id=cfg.device_id,
            store_path=cfg.store_path, config=config)
        self._room_ids: list[str] = []
        self._devices: dict[str, dict[str, list[str]]] = {}  # room -> user -> devs
        self._key_req: set[str] = set()  # Session-IDs, fuer die Keys schon angefragt wurden
        self._stop = asyncio.Event()

        # Callbacks (von bot.py gesetzt):
        self.on_text: Callable[[str, str, str], Awaitable[None]] | None = None      # room_id, sender, body
        # room_id, sender, Dateiname, MIME-Typ, Inhalt (bereits entschluesselt)
        self.on_media: Callable[[str, str, str, str, bytes], Awaitable[None]] | None = None
        self.on_call: Callable[[Optional[str], str, Optional[str], dict], Awaitable[None]] | None = None  # room_id|None, sender, device, content
        self.on_member_join: Callable[[str, str], Awaitable[None]] | None = None    # room_id, user_id
        self.on_joined: Callable[[str], Awaitable[None]] | None = None              # room_id (Bot beigetreten)
        self.on_room_left: Callable[[str], Awaitable[None]] | None = None           # room_id (Bot verlassen)
        # Element Call: room_id, sender, state_key, content
        self.on_rtc_member: Callable[[str, str, str, dict], Awaitable[None]] | None = None

        self.client.add_event_callback(self._event_cb, nio.Event)
        self.client.add_to_device_callback(self._to_device_cb, nio.ToDeviceEvent)
        self.client.add_response_callback(self._login_cb, LoginResponse)

    # ------------------------------------------------------------------ login
    async def login(self) -> None:
        cfg = self.cfg
        if cfg.token:
            self.client.restore_login(cfg.user, cfg.device_id, cfg.token)
            me = await self.client.whoami()
            if isinstance(me, nio.WhoamiResponse):
                cfg.matrix_user_id = me.user_id
                log.info("Eingeloggt (Token) als %s", me.user_id)
            else:
                raise RuntimeError(f"Token-Login ungueltig: {me}")
        else:
            # Token aus frueherem Passwort-Login wiederverwenden: kein neuer
            # Login pro Neustart (Homeserver drosselt sonst mit HTTP 429).
            token_file = os.path.join(cfg.store_path, "access_token")
            restored = False
            if os.path.exists(token_file):
                with open(token_file) as fh:
                    saved = fh.read().strip()
                if saved:
                    self.client.restore_login(cfg.user, cfg.device_id, saved)
                    me = await self.client.whoami()
                    if isinstance(me, nio.WhoamiResponse):
                        cfg.matrix_user_id = me.user_id
                        restored = True
                        log.info("Eingeloggt als %s (gespeicherter Token, Geraet %s)",
                                 me.user_id, cfg.device_id)
                    else:
                        log.warning("Gespeicherter Token ungueltig, Passwort-Login: %s", me)
            if not restored:
                resp = await self.client.login(password=cfg.password,
                                               device_name="musikbot")
                if not isinstance(resp, LoginResponse):
                    raise RuntimeError(f"Login fehlgeschlagen: {resp}")
                cfg.matrix_user_id = resp.user_id
                with open(token_file, "w") as fh:
                    fh.write(resp.access_token)
                os.chmod(token_file, 0o600)
                log.info("Eingeloggt als %s (Geraet %s)", resp.user_id, resp.device_id)
        if cfg.matrix_user_id != cfg.user and cfg.user.startswith("@"):
            # ggf. anderer mxid -- Gleichstand fuer Filter verwenden
            cfg.user = cfg.matrix_user_id or cfg.user
        if self.cfg.encryption_enabled and self.client.olm is not None:
            log.info("E2EE aktiv (Geraeteschluessel werden hochgeladen)")

    def _login_cb(self, room, response) -> None:  # noqa: ARG002
        pass

    # ------------------------------------------------------------------ sync
    async def run(self) -> None:
        """Sync-Loop mit automatischem Wiederanlauf."""
        backoff = 1
        while not self._stop.is_set():
            try:
                resp = await self.client.sync(
                    timeout=SYNC_TIMEOUT_MS, since=self.client.next_batch,
                    set_presence="online")
            except Exception:
                log.exception("Sync-Fehler")
                resp = None
            if isinstance(resp, nio.SyncResponse):
                backoff = 1
                await self._crypto_housekeeping()
                await self._accept_invites()
            elif resp is not None:
                log.warning("Sync-Fehler: %s", resp)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _crypto_housekeeping(self) -> None:
        """Was nio.sync_forever() sonst automatisch macht: eigene Geraete-
        schluessel hochladen, fremde Geraete abfragen, Olm-Sessions claimen,
        ausstehende To-Device-Nachrichten senden. Ohne das kennt der Server
        keine Bot-Schluessel und niemand kann an den Bot verschluesseln."""
        c = self.client
        if not self.cfg.encryption_enabled or c.olm is None:
            return
        try:
            if c.should_upload_keys:
                r = await c.keys_upload()
                log.info("Geraeteschluessel hochgeladen: %s", type(r).__name__)
            if c.should_query_keys:
                await c.keys_query()
            if c.should_claim_keys:
                await c.keys_claim(c.get_users_for_key_claiming())
            await c.send_to_device_messages()
        except Exception:
            log.exception("E2EE-Schluesselpflege fehlgeschlagen")

    def stop(self) -> None:
        self._stop.set()

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
        if isinstance(event, nio.RoomMessageText):
            if event.sender == self.cfg.user or event.sender == self.cfg.matrix_user_id:
                return
            log.info("Text von %s in %s: %s", event.sender, room.room_id,
                     (event.body or "")[:80])
            if self.on_text:
                await self.on_text(room.room_id, event.sender, event.body or "")
        elif isinstance(event, MEDIA_EVENTS):
            await self._media_cb(room, event)
        elif isinstance(event, nio.MegolmEvent):
            # Verschluesseltes Event, dessen Raum-Key fehlt. nio fragt dafuer
            # NICHT automatisch nach (nur bei gezw-Session-Faellen) -> selbst
            # beim Absender anfragen. Element teilt Keys aber nur, wenn das
            # Bot-Geraet verifiziert ist oder Schluesselteilung aktiviert wurde.
            await self._request_room_key(event)
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

    async def _request_room_key(self, event: nio.MegolmEvent) -> None:
        """Raum-Key beim Absender anfragen (m.room_key_request als To-Device)."""
        session_id = getattr(event, "session_id", None)
        if not session_id or session_id in self._key_req:
            return
        self._key_req.add(session_id)
        try:
            msg = event.as_key_request(event.sender, self.cfg.device_id)
        except Exception:
            log.exception("Key-Request bauen fehlgeschlagen")
            return
        resp = await self.client.to_device(msg)
        if isinstance(resp, nio.ToDeviceResponse):
            log.info("Raum-Key angefragt bei %s (Session %s).",
                     event.sender, session_id[:12])
        else:
            self._key_req.discard(session_id)
            log.warning("Key-Request an %s fehlgeschlagen: %s", event.sender, resp)

    async def _membership_cb(self, room: nio.MatrixRoom, event) -> None:
        source = getattr(event, "source", {}) or {}
        etype = source.get("type", "")
        if etype != "m.room.member":
            return
        state_key = source.get("state_key", "")
        membership = (source.get("content") or {}).get("membership", "")
        me = self.cfg.user
        if state_key == me and membership == "invite" and self.cfg.auto_join_invites:
            asyncio.ensure_future(self._join(room.room_id))
        elif state_key == me and membership in ("leave", "ban"):
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
            if self.on_joined:
                asyncio.ensure_future(self.on_joined(room_id))
        else:
            log.warning("Raum-Beitritt %s fehlgeschlagen: %s", room_id, resp)

    async def _accept_invites(self) -> None:
        """Offene Einladungen annehmen.

        nio behandelt Einladungen im /sync separat als 'invited room' und feuert
        dafuer KEINE Event-Callbacks -- daher explizit nach jedem Sync pruefen.
        """
        if not self.cfg.auto_join_invites:
            return
        # nio pflegt offene Einladungen separat in client.invited_rooms.
        for room in list(self.client.invited_rooms.values()):
            log.info("Einladung erkannt: %s", room.room_id)
            await self._join(room.room_id)

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
        await self._accept_invites()
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

    def find_dm(self, user_id: str) -> Optional[str]:
        """Bestehender Zweier-Raum mit `user_id` (beigetreten oder eingeladen)."""
        me = (self.cfg.matrix_user_id or self.cfg.user).lower()
        want = user_id.lower()
        for room_id, room in self.client.rooms.items():
            members = {u.lower() for u in list(room.users) + list(getattr(room, "invited_users", {}))}
            if want in members and members <= {me, want}:
                return room_id
        return None

    async def send_dm(self, user_id: str, body: str, html: str = "") -> bool:
        """Direktnachricht schicken; legt den Zweier-Raum bei Bedarf an (verschluesselt)."""
        room_id = self.find_dm(user_id)
        if room_id is None:
            state = ([{"type": "m.room.encryption", "state_key": "",
                       "content": {"algorithm": "m.megolm.v1.aes-sha2"}}]
                     if self.cfg.encryption_enabled else [])
            resp = await self.client.room_create(
                is_direct=True, invite=[user_id], preset=nio.RoomPreset.trusted_private_chat,
                name="Musikbot", initial_state=state)
            if isinstance(resp, nio.ErrorResponse):
                log.error("DM-Raum mit %s nicht angelegt: %s", user_id, resp)
                return False
            room_id = resp.room_id
            await self._mark_direct(user_id, room_id)
            # Der Sync haengt bis zu SYNC_TIMEOUT_MS im Long-Poll: so lange auf den Raum warten
            for _ in range(int(SYNC_TIMEOUT_MS / 1000 * 4) + 40):
                if room_id in self.client.rooms:
                    break
                await asyncio.sleep(0.25)
            else:
                log.error("Neuer DM-Raum %s kam nicht im Sync an", room_id)
                return False
        content = {"msgtype": "m.text", "body": body}
        if html:
            content.update(format="org.matrix.custom.html", formatted_body=html)
        return await self.send_room_event(room_id, "m.room.message", content) is not None

    async def _mark_direct(self, user_id: str, room_id: str) -> None:
        """Raum in m.direct eintragen, damit Clients ihn als Direktnachricht zeigen."""
        from urllib.parse import quote
        me = self.cfg.matrix_user_id or self.cfg.user
        url = self._cs(f"/user/{quote(me)}/account_data/m.direct")
        try:
            try:
                data = await self.http_json("GET", url)
            except RuntimeError:
                data = {}
            rooms = list(data.get(user_id) or [])
            if room_id not in rooms:
                data[user_id] = rooms + [room_id]
                await self.http_json("PUT", url, data)
        except Exception:
            log.warning("m.direct nicht gesetzt", exc_info=True)

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
