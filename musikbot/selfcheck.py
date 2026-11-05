"""Selbsttest: /musikbot --check prueft Zugang, Verschluesselung, Codecs, ICE."""
from __future__ import annotations

import logging

log = logging.getLogger("musikbot.selfcheck")


async def run_check(bot) -> None:
    ok = True

    def line(label: str, good: bool, info: str = "") -> None:
        nonlocal ok
        mark = "OK " if good else "FEHLER"
        if not good:
            ok = False
        print(f"[{mark}] {label}" + (f": {info}" if info else ""))

    cfg = bot.cfg
    print(f"Musikbot-Selbsttest – Nutzer {cfg.matrix_user_id or cfg.user}")

    # 1) Raeume
    joined = await bot.matrix.client.joined_rooms()
    rooms = getattr(joined, "rooms", [])
    line("Raeume", bool(rooms) or bool(cfg.rooms), f"{len(rooms)} beigetreten")
    if not rooms and not cfg.rooms:
        print("     -> MATRIX_ROOMS setzen oder den Bot in einen Raum einladen.")

    # 2) Verschluesselung
    for rid in rooms:
        room = bot.matrix.client.rooms.get(rid)
        enc = bool(getattr(room, "encrypted", False)) if room else False
        print(f"     Raum {rid}: verschluesselt={enc}")

    # 3) Geraeteschluessel (E2EE aktiv?)
    crypto_ready = bot.matrix.client.olm is not None
    line("E2EE/Krypto", crypto_ready or not cfg.encryption_enabled,
         "vodozemac geladen" if crypto_ready else "deaktiviert/verfuegbar")

    # 4) Codecs
    try:
        from aiortc.codecs import opus, vp8
        assert opus and vp8
        line("Codecs", True, "Opus + VP8/VPX verfuegbar (libopus/libvpx)")
    except Exception as exc:
        line("Codecs", False, str(exc))

    # 5) ICE/TURN
    servers = bot.calls.ice_servers
    line("ICE-Server", True, f"{len(servers)} konfiguriert "
         f"(STUN {'an' if cfg.stun_uris else 'aus'}, "
         f"TURN-Discovery {'an' if cfg.turn_discovery else 'aus'})")

    # 6) Dateien
    import os
    writable = os.access(cfg.data_dir, os.W_OK) if os.path.isdir(cfg.data_dir) else False
    line("Datenverzeichnis", writable, cfg.data_dir)

    # 7) Peers
    print(f"     Aktive WebRTC-Peers: {len(bot.calls.peers)}")

    print("Ergebnis:", "alles okay" if ok else "siehe FEHLER-Zeilen oben")
