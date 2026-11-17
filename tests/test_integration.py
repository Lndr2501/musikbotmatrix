"""Ende zu Ende gegen einen echten Homeserver in einem verschluesselten Raum (nur mit
DIDDY_TEST_HOMESERVER, z. B. ein lokaler Synapse mit offener Registrierung)::

    DIDDY_TEST_HOMESERVER=http://127.0.0.1:8008 pytest tests/test_integration.py

Prueft den echten Startweg des Bots: Login, Beitritt per Einladung, Befehlsliste und Profil fuer
Diddy, Befehle in verschluesselten Raeumen, Karten, Neustart mit dem alten Token.
"""
import asyncio
import json
import os
import secrets
import urllib.request

import pytest

HS = os.environ.get("DIDDY_TEST_HOMESERVER", "")
pytestmark = pytest.mark.skipif(not HS, reason="DIDDY_TEST_HOMESERVER nicht gesetzt")

if HS:
    import nio

    from musikbot.bot import Bot
    from musikbot.config import Config


def register(name: str, password: str) -> str:
    body = json.dumps({"username": name, "password": password,
                       "auth": {"type": "m.login.dummy"}}).encode()
    req = urllib.request.Request(f"{HS}/_matrix/client/v3/register", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)["user_id"]


class User:
    """A person with a normal E2EE client (what Diddy does, minus the drawing)."""

    def __init__(self, name, password, store):
        os.makedirs(store, exist_ok=True)
        self.c = nio.AsyncClient(HS, name, store_path=store,
                                 config=nio.AsyncClientConfig(encryption_enabled=True))
        self.password = password
        self.events = []
        self.c.add_event_callback(self._on, nio.Event)

    async def _on(self, room, event):
        self.events.append((room.room_id, event))

    async def login(self):
        r = await self.c.login(self.password)
        assert isinstance(r, nio.LoginResponse), r
        await self.sync()

    async def sync(self, timeout=0):
        await self.c.sync(timeout=timeout, full_state=True)
        if self.c.should_upload_keys:
            await self.c.keys_upload()
        if self.c.should_query_keys:
            await self.c.keys_query()
        if self.c.should_claim_keys:
            await self.c.keys_claim(self.c.get_users_for_key_claiming())
        await self.c.send_to_device_messages()

    async def wait_for(self, pred, seconds=20):
        for _ in range(int(seconds * 4)):
            for room_id, ev in self.events:
                if pred(room_id, ev):
                    return ev
            await self.sync(250)
        raise AssertionError("timed out; last events: " + "; ".join(
            f"{type(e).__name__} {getattr(e, 'sender', '')}" for _, e in self.events[-6:]))

    async def say(self, room_id, body, **extra):
        r = await self.c.room_send(room_id, "m.room.message",
                                   {"msgtype": "m.text", "body": body, **extra},
                                   ignore_unverified_devices=True)
        assert isinstance(r, nio.RoomSendResponse), r
        return r.event_id


def content(ev):
    return (getattr(ev, "source", None) or {}).get("content") or {}


def make_config(tmp_path, user, password="botpass"):
    cfg = Config(homeserver=HS, user=user, password=password, data_dir=str(tmp_path),
                 radio_dir=str(tmp_path / "radio"), enable_video=False)
    cfg.turn_discovery = False
    return cfg


async def test_commands_and_cards_in_encrypted_room(tmp_path):
    tag = secrets.token_hex(3)
    bot_id = register(f"musik{tag}", "botpass")
    alice_id = register(f"alice{tag}", "alicepass")
    bot = Bot(make_config(tmp_path, bot_id))
    await bot.startup()
    runner = asyncio.ensure_future(bot.matrix.run())
    alice = User(alice_id, "alicepass", str(tmp_path / "alice"))
    try:
        await alice.login()
        room = await alice.c.room_create(
            invite=[bot_id], name="Musik",
            initial_state=[{"type": "m.room.encryption", "state_key": "",
                            "content": {"algorithm": "m.megolm.v1.aes-sha2"}}],
            power_level_override={"users": {alice_id: 100, bot_id: 50}})
        rid = room.room_id
        await alice.wait_for(lambda r, e: r == rid and isinstance(e, nio.RoomMemberEvent)
                             and e.state_key == bot_id and e.membership == "join")
        for _ in range(40):
            st = await alice.c.room_get_state_event(rid, "dev.diddy.commands", bot_id)
            if isinstance(st, nio.RoomGetStateEventResponse):
                break
            await asyncio.sleep(0.25)
        names = [c["name"] for c in st.content["commands"]]
        assert names[:3] == ["play", "dashboard", "autoplay"] and "24/7" in names
        assert all("options" not in c for c in st.content["commands"])
        prof = await alice.c.room_get_state_event(rid, "dev.diddy.bot", bot_id)
        assert prof.content["about"].startswith("Spielt Musik")

        await asyncio.sleep(1)
        await alice.sync()
        await alice.say(rid, "!hilfe")                     # Alias: Hilfe-Karte
        reply = await alice.wait_for(lambda r, e: r == rid and e.sender == bot_id
                                     and content(e).get("dev.diddy.card", {}).get("kind") == "help")
        assert content(reply)["dev.diddy.card"]["groups"]
        await alice.say(rid, "!status")
        reply = await alice.wait_for(lambda r, e: r == rid and e.sender == bot_id
                                     and e is not reply and "dev.diddy.card" in content(e))
        assert reply
        device = bot.matrix.client.device_id
    finally:
        bot.matrix.stop()
        await asyncio.wait_for(runner, 40)
        await bot.shutdown()
        await alice.c.close()

    # Neustart: der gespeicherte Token wird benutzt (kein neues Geraet, kein Passwort noetig)
    bot2 = Bot(make_config(tmp_path, bot_id, password="falsch"))
    await bot2.matrix.login()
    assert bot2.matrix.client.device_id == device
    await bot2.matrix.client.close()


async def test_old_token_file_is_adopted(tmp_path):
    """Update vom Bot vor diddybot: Token aus `access_token` im Store, gleiches Geraet."""
    tag = secrets.token_hex(3)
    bot_id = register(f"alt{tag}", "botpass")
    c = nio.AsyncClient(HS, bot_id, device_id="musikbot")
    resp = await c.login("botpass", device_name="musikbot")
    await c.close()
    cfg = make_config(tmp_path, bot_id, password="falsch")
    os.makedirs(cfg.store_path)
    with open(os.path.join(cfg.store_path, "access_token"), "w") as fh:
        fh.write(resp.access_token)
    bot = Bot(cfg)
    await bot.matrix.login()
    assert bot.matrix.client.device_id == "musikbot"
    assert bot.matrix.client.access_token == resp.access_token
    await bot.matrix.client.close()
