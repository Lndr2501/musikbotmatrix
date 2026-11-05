"""m.call-Signaling (Matrix VoIP) + WebRTC-Peers.

Klassische Matrix-Calls: Signaling laeuft als m.call.invite/answer/
candidates/hangup-Nachrichten (ToDevice, zusaendlich als Raum-Events in
unverschluesselten Raeumen), die Medien laufen Peer-zu-Peer (WebRTC/ICE).
Genau das nutzen Element Web/Telefon ueber das Telefon-Icon. Gruppen sind
ein Mesh: jedes Geraet verbindet sich direkt mit dem Bot; der Bot hoert
alle und sendet allen denselben Mix.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from typing import Callable, Optional

import aiortc
from aioice.ice import Candidate as AioiceCandidate

from .config import Config
from .player import Player
from .util import now_ms

log = logging.getLogger("musikbot.calls")

CANDIDATE_RE = re.compile(r"^a=candidate:(.+)$")


def _new_id(prefix: str) -> str:
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}{now_ms()}{rand}"


def parse_local_candidates(sdp: str) -> list[dict]:
    """a=candidate:-Zeilen aus dem lokalen SDP als m.call.candidates-Objekte."""
    # Abschnitts-Mids bestimmen (aiortc nutzt mid == Abschnittsindex)
    out: list[dict] = []
    seen = set()
    mline_index: dict[int, int] = {}
    idx = -1
    for line in sdp.splitlines():
        if line.startswith("m="):
            idx += 1
        elif line.startswith("a=mid:"):
            try:
                mline_index[int(line[6:].strip())] = idx
            except ValueError:
                pass
    mid_of_section = {v: k for k, v in mline_index.items()}
    idx = -1
    for line in sdp.splitlines():
        if line.startswith("m="):
            idx += 1
        m = CANDIDATE_RE.match(line)
        if not m:
            continue
        parts = m.group(1).split()
        if len(parts) < 7:
            continue
        # aioice-Format: foundation component proto priority ip port typ type
        foundation, comp_s, proto, prio_s, ip, port_s, typ_kw, typ = parts[:8] \
            if len(parts) >= 8 and parts[6] == "typ" else ("", "", "", "", "", "", "", "")
        if not typ:
            continue
        try:
            component = int(comp_s)
            priority = int(prio_s)
            port = int(port_s)
        except ValueError:
            continue
        raddr = rport = None
        for i, p in enumerate(parts):
            if p == "raddr" and i + 2 < len(parts):
                raddr = parts[i + 1]
                try:
                    rport = int(parts[i + 2])
                except ValueError:
                    pass
                break
        key = (proto, ip, port, typ)
        if key in seen:
            continue
        seen.add(key)
        mid = mid_of_section.get(idx, str(idx))
        cand = {
            "candidate": m.group(1),
            "sdpMid": mid,
            "sdpMLineIndex": idx,
            "foundation": foundation,
            "component": component,
            "protocol": proto,
            "priority": priority,
            "ip": ip,
            "port": port,
            "type": typ,
        }
        if raddr and rport is not None:
            cand["raddr"] = raddr
            cand["rport"] = rport
        out.append(cand)
    return out


def to_aioice_candidate(c: dict) -> Optional[AioiceCandidate]:
    try:
        foundation = str(c.get("foundation", "0"))
        component = int(c.get("component", 1))
        proto = str(c.get("protocol", "udp")).lower()
        proto = "tcp" if proto == "tcp" else "udp"
        priority = int(c.get("priority", 0))
        ip = str(c.get("ip", c.get("address", "")))
        port = int(c.get("port", 0))
        typ = str(c.get("type", "host")).lower()
        if typ not in ("host", "srflx", "prflx", "relay"):
            typ = "host"
        host = None
        if c.get("raddr") and c.get("rport"):
            host = AioiceCandidate(foundation, 1, "udp", int(c["rport"]),
                                   str(c["raddr"]), priority)
        return AioiceCandidate(foundation, component, proto, port, ip,
                               priority, type=typ, host_candidate=host)
    except Exception:
        log.debug("unbekannter ICE-Kandidat: %r", c)
        return None


class Peer:
    """Ein WebRTC-Peer = ein Geraet eines Nutzers in einem Call."""

    def __init__(self, mgr: "CallManager", room_id: str, conf_id: str,
                 user_id: str, device_id: Optional[str], call_id: str,
                 party_id: str, caller: bool, version: str = "1"):
        self.mgr = mgr
        self.room_id = room_id
        self.conf_id = conf_id
        self.user_id = user_id
        self.device_id = device_id
        self.call_id = call_id
        self.party_id = party_id
        self.caller = caller          # wir haben eingeladen
        self.version = version        # m.call spec-version ("0" oder "1")
        self.live = True
        self.answered = False
        self.pc: Optional[aiortc.RTCPeerConnection] = None
        self.audio_track = None
        self.video_track = None
        self._gathering = asyncio.Event()
        self._pending: list[dict] = []
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.pc = pc = aiortc.RTCPeerConnection(iceServers=self.mgr.ice_servers)
        self.audio_track = self.mgr.player.add_peer_audio(self)
        self.video_track = self.mgr.player.add_peer_video(self)
        pc.addTrack(self.audio_track)
        if self.video_track is not None:
            pc.addTrack(self.video_track)

        @pc.addEventListener("icegatheringstatechange")
        def _ice(_evt):
            if pc.iceGatheringState == "complete":
                self._gathering.set()

        @pc.addEventListener("connectionstatechange")
        def _conn(_evt):
            state = pc.connectionState
            log.debug("RTC %s: %s (%s)", state, self.user_id, self.device_id)
            if state in ("failed", "closed") and self.live:
                asyncio.ensure_future(self.close("rtc-" + state))
            elif state == "connected":
                log.info("RTC verbunden: %s [%s]", self.user_id, self.device_id or "-")

        if self.caller:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._wait_gathering()
            desc = pc.localDescription
            if self.version == "0":
                await self.mgr.send_call(self, "invite", {
                    "type": desc.type, "sdp": desc.sdp, "lifetime": 120000})
            else:
                await self.mgr.send_call(self, "invite", {
                    "offer": {"type": desc.type, "sdp": desc.sdp},
                    "lifetime": 120000,
                })
            await self._send_candidates()

    async def _wait_gathering(self) -> None:
        try:
            await asyncio.wait_for(self._gathering.wait(),
                                   timeout=self.mgr.cfg.ice_gather_timeout)
        except asyncio.TimeoutError:
            log.warning("ICE-Gathering Timeout (%s)", self.party_id)

    async def _send_candidates(self) -> None:
        if self.pc is None or not self.live:
            return
        cands = parse_local_candidates(self.pc.localDescription.sdp)
        if cands:
            await self.mgr.send_call(self, "candidates", {"candidates": cands})

    async def handle_offer(self, sdp: str) -> None:
        """Wir wurden eingeladen: Offer entgegennehmen, Answer senden."""
        async with self._lock:
            pc = self.pc
            if pc is None or self.answered:
                return
            desc = aiortc.RTCSessionDescription(sdp=sdp, type="offer")
            await pc.setRemoteDescription(desc)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            self.answered = True
            local = pc.localDescription
            if self.version == "0":
                await self.mgr.send_call(self, "answer",
                                         {"type": local.type, "sdp": local.sdp})
            else:
                await self.mgr.send_call(self, "answer",
                                         {"answer": {"type": local.type,
                                                    "sdp": local.sdp}})
            await self._wait_gathering()
            await self._send_candidates()

    async def handle_answer(self, sdp: str) -> None:
        """Unser Invite wurde beantwortet."""
        async with self._lock:
            pc = self.pc
            if pc is None or self.answered:
                return
            desc = aiortc.RTCSessionDescription(sdp=sdp, type="answer")
            await pc.setRemoteDescription(desc)
            self.answered = True
            for c in self._pending:
                await self._add_candidate(c)
            self._pending.clear()

    async def handle_candidates(self, cands: list[dict]) -> None:
        async with self._lock:
            if self.pc is None or not self.answered:
                self._pending.extend(cands)
                return
        for c in cands:
            await self._add_candidate(c)

    async def _add_candidate(self, c: dict) -> None:
        cand = to_aioice_candidate(c)
        if cand is None or self.pc is None:
            return
        try:
            await self.pc.addIceCandidate(cand)
        except Exception:
            log.debug("ICE-Kandidat abgelehnt", exc_info=True)

    async def close(self, reason: str = "user", send_hangup: bool = True) -> None:
        if not self.live:
            return
        self.live = False
        self.mgr.player.remove_peer(self)
        if send_hangup:
            try:
                await self.mgr.send_call(self, "hangup",
                                         {"reason": reason if reason.startswith(("user", "ice_failed", "invite_timeout")) else "user"})
            except Exception:
                pass
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                pass
        self.mgr.peers.discard(self)
        log.info("Peer getrennt (%s): %s [%s]", reason, self.user_id,
                 self.device_id or "-")


class CallManager:
    """Signaling + WebRTC-Verwaltung fuer alle Geraete in allen Calls."""

    def __init__(self, cfg: Config, player: Player):
        self.cfg = cfg
        self.player = player
        self.peers: set[Peer] = set()
        self.ice_servers: list[aiortc.RTCIceServer] = []
        if cfg.turn_uris:
            self.ice_servers.append(aiortc.RTCIceServer(
                urls=list(cfg.turn_uris), username=cfg.turn_username,
                credential=cfg.turn_password))
        self.active_conf: Optional[str] = None
        self.active_room: Optional[str] = None
        self._conf_of_call: dict[str, str] = {}
        # Callbacks, die matrix.py setzt:
        self.send_room_event: Callable[[str, str, dict], asyncio.Future] | None = None
        self.send_to_device: Callable[[list[tuple[str, str]], str, dict], asyncio.Future] | None = None
        self.devices_of_room: Callable[[str], dict[str, list[str]]] | None = None

    # ------------------------------------------------------------------ senden
    async def send_call(self, peer: Peer, action: str, content: dict) -> None:
        body = {
            "version": peer.version,
            "call_id": peer.call_id,
            "party_id": peer.party_id,
            **content,
        }
        if peer.version != "0":
            body["conf_id"] = peer.conf_id
        evtype = f"m.call.{action}"
        if self.send_to_device and peer.device_id:
            await self.send_to_device([(peer.user_id, peer.device_id)],
                                      evtype, body)
        elif self.send_room_event:
            if self.cfg.encryption_enabled:
                log.warning("Kein Geraet fuer %s bekannt -- Signaling in "
                            "verschluesseltem Raum moeglich", peer.user_id)
            await self.send_room_event(peer.room_id, evtype, body)

    # ------------------------------------------------------------------ empfangen
    async def handle_event(self, room_id: Optional[str], sender: str,
                           sender_device: Optional[str], content: dict) -> None:
        msgtype = content.get("msgtype") or content.get("type") or ""
        if not msgtype.startswith("m.call."):
            return
        action = msgtype[len("m.call."):]
        call_id = content.get("call_id") or ""
        conf_id = content.get("conf_id") or content.get("confId") or call_id
        party_id = content.get("party_id") or call_id
        if not call_id or not isinstance(content, dict):
            return
        if room_id is None:
            room_id = self._conf_of_call.get(call_id) or self._conf_of_call.get(conf_id)
        if room_id is None and action == "invite":
            return
        self._conf_of_call.setdefault(call_id, conf_id)

        if action == "invite":
            await self._on_invite(room_id, sender, sender_device, call_id,
                                  party_id, conf_id, content)
        elif action == "answer":
            peer = self._find(call_id, party_id)
            if peer and peer.caller:
                answer = content.get("answer") or content.get("sdp") or {}
                sdp = answer.get("sdp") if isinstance(answer, dict) else answer
                if sdp:
                    await peer.handle_answer(sdp)
                    self._reject_other_devices(peer)
        elif action == "candidates":
            peer = self._find(call_id, party_id)
            cands = content.get("candidates") or []
            if peer:
                await peer.handle_candidates([c for c in cands if isinstance(c, dict)])
        elif action in ("hangup", "reject"):
            peer = self._find(call_id, party_id)
            if peer:
                await peer.close("remote-" + action, send_hangup=False)
        elif action == "end":
            await self.hangup_all("call ended", send_hangup=False)
        elif action == "select":
            pass  # Multi-Device: eine andere Session wurde gewaehlt

    def _find(self, call_id: str, party_id: str) -> Optional[Peer]:
        for p in self.peers:
            if p.call_id == call_id and (p.party_id == party_id or party_id == call_id):
                return p
        # conf_id-Match (Element kombiniert Multi-Device-Calls)
        for p in self.peers:
            if p.call_id == call_id:
                return p
        return None

    async def _on_invite(self, room_id, user_id, device_id, call_id, party_id,
                         conf_id, content) -> None:
        if not self.cfg.auto_answer:
            return
        # Gruppenanruf erkennbar an conf_id (v1); 1:1-Calls haben keinen.
        is_group = bool(content.get("conf_id") or content.get("confId"))
        mode = self.cfg.mesh_invite
        if mode == "off" and is_group and self.active_conf and conf_id != self.active_conf:
            return
        peer = self._find(call_id, party_id)
        if peer:
            return
        peer = Peer(self, room_id, conf_id, user_id, device_id, call_id,
                    party_id, caller=False, version=str(content.get("version", "1")))
        self.peers.add(peer)
        self.active_conf = conf_id
        self.active_room = room_id
        offer = content.get("offer") or {}
        sdp = offer.get("sdp") if isinstance(offer, dict) else None
        if not sdp:
            await peer.close("bad-invite", send_hangup=False)
            return
        await peer.start()
        try:
            await peer.handle_offer(sdp)
        except Exception:
            log.exception("Invite beantworten fehlgeschlagen")
            await peer.close("bad-invite")
        if mode == "all" or (mode == "auto" and is_group):
            await self._mesh_join(room_id, conf_id, {p.user_id for p in self.peers
                                                     if p.conf_id == conf_id})

    async def _mesh_join(self, room_id: str, conf_id: str,
                         known_users: set[str]) -> None:
        """Weitere Geraete im Raum einladen, damit der Bot ein volles Mesh hat."""
        if self.devices_of_room is None:
            return
        members = self.devices_of_room(room_id)
        for uid, devices in members.items():
            if uid in known_users or uid == self.cfg.matrix_user_id:
                continue
            for dev in (devices or [None]):
                await self._invite_device(room_id, conf_id, uid, dev)

    async def _invite_device(self, room_id: str, conf_id: str, uid: str,
                             dev: Optional[str]) -> None:
        call_id = _new_id("call")
        party_id = _new_id("part")
        self._conf_of_call[call_id] = conf_id
        peer = Peer(self, room_id, conf_id, uid, dev, call_id, party_id,
                    caller=True)
        self.peers.add(peer)
        await peer.start()
        log.info("Invite gesendet: %s [%s]", uid, dev or "-")

    def _reject_other_devices(self, answered: Peer) -> None:
        """Sobald ein Geraetantwortet, ziehen wir die anderen ein."""
        for p in list(self.peers):
            if (p is not answered and p.user_id == answered.user_id
                    and p.conf_id == answered.conf_id and not p.answered):
                asyncio.ensure_future(p.close("answered elsewhere",
                                              send_hangup=False))

    # ------------------------------------------------------------ Bot als Initiator
    async def start_call_in_room(self, room_id: str) -> None:
        """Bot eroeffnet einen Call im Raum (Commands /joincall bzw. /play)."""
        if self.active_room == room_id and any(
                p.live for p in self.peers if p.room_id == room_id):
            return
        conf_id = _new_id("conf")
        self.active_conf = conf_id
        self.active_room = room_id
        members = (self.devices_of_room or (lambda r: {}))(room_id)
        for uid, devices in members.items():
            if uid == self.cfg.matrix_user_id:
                continue
            for dev in (devices or [None]):
                await self._invite_device(room_id, conf_id, uid, dev)
        log.info("Call im Raum %s eroeffnet (%d Geraete)", room_id, len(self.peers))

    async def on_member_joined(self, room_id: str, user_id: str) -> None:
        if self.active_room != room_id or user_id == self.cfg.matrix_user_id:
            return
        members = (self.devices_of_room or (lambda r: {}))(room_id)
        for dev in (members.get(user_id) or [None]):
            await self._invite_device(room_id, self.active_conf, user_id, dev)

    async def hangup_all(self, reason: str = "bye",
                         send_hangup: bool = True) -> None:
        self.active_conf = None
        self.active_room = None
        for peer in list(self.peers):
            await peer.close(reason, send_hangup)

    async def on_room_left(self, room_id: str) -> None:
        for peer in [p for p in self.peers if p.room_id == room_id]:
            await peer.close("left room")

    # ---------------------------------------------------------------- TURN
    def set_turn(self, data: dict) -> None:
        """TURN-Credentials vom Homeserver (_matrix/client/v3/voip/turnServer)."""
        if not self.cfg.use_turn:
            return
        try:
            urls = data.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            if not urls:
                return
            static: list[aiortc.RTCIceServer] = []
            if self.cfg.turn_uris:
                static.append(aiortc.RTCIceServer(
                    urls=list(self.cfg.turn_uris),
                    username=self.cfg.turn_username,
                    credential=self.cfg.turn_password))
            self.ice_servers = static + [aiortc.RTCIceServer(
                urls=list(urls),
                username=str(data.get("username", "")),
                credential=str(data.get("password", data.get("credential", ""))),
            )]
            log.info("TURN-Server aktiv: %s", urls)
        except Exception:
            log.exception("TURN fehlgeschlagen")
