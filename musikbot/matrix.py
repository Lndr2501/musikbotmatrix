"""Matrix-Anbindung: Login, Sync-Loop, Empfinden/Senden von Events.

Duenn um matrix-nio: Der Bot laeuft als normaler Nutzer, verschluesselt
(E2EE) nach Moeglichkeit, versteht Textbefehle und m.call.*-Signaling
sowohl als Raum-Event als auch als To-Device-Nachricht.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import nio
from nio.responses import (JoinedMembersResponse, KeysQueryResponse,
                          LoginResponse)

from .config import Config

log = logging.getLogger("musikbot.matrix")

SYNC_TIMEOUT_MS = 30_000


class Matrix:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        config = nio.AsyncClientConfig(
            encryption_enabled=cfg.encryption_enabled,
            store_sync_tokens=True,
        )
        self.client = nio.AsyncClient(
            cfg.homeserver, cfg.user, device_id=cfg.device_id,
            store_path=cfg.store_path, config=config)
        self._room_ids: list[str] = []
        self._devices: dict[str, dict[str, list[str]]] = {}  # room -> user -> devs
        self._stop = asyncio.Event()

        # Callbacks (von bot.py gesetzt):
        self.on_text: Callable[[str, str, str], Awaitable[None]] | None = None      # room_id, sender, body
        self.on_call: Callable[[Optional[str], str, Optional[str], dict], Awaitable[None]] | None = None  # room_id|None, sender, device, content
        self.on_member_join: Callable[[str, str], Awaitable[None]] | None = None    # room_id, user_id
        self.on_room_left: Callable[[str], Awaitable[None]] | None = None           # room_id (Bot verlassen)

        self.client.add_event_callback(self._event_cb, nio.Event)
        self.client.add_to_device_callback(self._to_device_cb, nio.ToDeviceEvent)
        self.client.add_response_callback(LoginResponse, self._login_cb)

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
            resp = await self.client.login(password=cfg.password)
            if not isinstance(resp, LoginResponse):
                raise RuntimeError(f"Login fehlgeschlagen: {resp}")
            cfg.matrix_user_id = resp.user_id
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
            elif resp is not None:
                log.warning("Sync-Fehler: %s", resp)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ callbacks
    async def _event_cb(self, room: nio.MatrixRoom, event) -> None:
        if getattr(event, "sender", None) in (self.cfg.user, self.cfg.matrix_user_id):
            return
        if isinstance(event, nio.RoomMessageText):
            if event.sender == self.cfg.user or event.sender == self.cfg.matrix_user_id:
                return
            if self.on_text:
                await self.on_text(room.room_id, event.sender, event.body or "")
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
                              content: dict) -> None:
        try:
            resp = await self.client.room_send(
                room_id, evtype, content, ignore_unverified_devices=True)
            if isinstance(resp, nio.ErrorResponse):
                log.error("Senden (%s) fehlgeschlagen: %s", evtype, resp)
        except Exception:
            log.exception("Senden (%s) fehlgeschlagen", evtype)

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
        keys = await self.client.keys_query()
        devices: dict[str, list[str]] = {}
        if isinstance(keys, KeysQueryResponse):
            for uid in users:
                per_user = keys.device_keys.get(uid) or {}
                devices[uid] = sorted(per_user.keys())
        self._devices[room_id] = devices
        return devices

    def devices_of_room(self, room_id: str) -> dict[str, list[str]]:
        """Nicht-blockierender Zugriff auf den zuletzt geladenen Stand."""
        return self._devices.get(room_id, {})

    async def ensure_room_state(self, room_id: str) -> None:
        await self.refresh_devices(room_id)

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
