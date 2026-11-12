"""Start: python -m musikbot  (Konfiguration komplett ueber Umgebungsvariablen)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from .bot import Bot
from .config import Config, ConfigError


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)


async def amain() -> int:
    check = "--check" in sys.argv
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Konfiguration unvollstaendig: {exc}", file=sys.stderr)
        print("Siehe .env.example / README.md", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)
    os.makedirs(cfg.data_dir, exist_ok=True)

    bot = Bot(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        rooms = await bot.startup()
        if check:
            from .selfcheck import run_check
            await run_check(bot)
            return 0
        await bot.start_web()
        bot.start_watchers()
        log = logging.getLogger("musikbot")
        log.info("Bot laeuft. Raeume: %s", ", ".join(rooms) or "(keine)")
        sync = asyncio.create_task(bot.matrix.run())
        stop_task = asyncio.create_task(stop.wait())
        done, _pending = await asyncio.wait(
            {sync, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if sync in done and sync.exception():
            logging.getLogger("musikbot").error("Sync-Loop abgestuerzt",
                                                exc_info=sync.exception())
            return 1
    finally:
        await bot.shutdown()
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
