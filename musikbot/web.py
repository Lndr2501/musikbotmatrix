"""Web-Dashboard: kleine JSON-API + statische Seite (aiohttp, laeuft im Bot-Prozess).

Login ueber den persoenlichen Link aus der DM (`!dashboard`). Die Sitzung steckt in
einem HttpOnly-Cookie; jede schreibende Anfrage braucht zusaetzlich den Header
`X-Musikbot: 1` – den kann eine fremde Seite ohne CORS-Freigabe nicht setzen.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Awaitable, Callable

from aiohttp import web

from .ytdl import MediaError

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)

COOKIE = "musikbot_session"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CSP = ("default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def _security(request: web.Request, handler: Handler) -> web.StreamResponse:
    if request.method == "POST" and request.path.startswith("/api/") \
            and request.headers.get("X-Musikbot") != "1":
        return web.json_response({"error": "Ungueltige Anfrage."}, status=403)
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        if not request.path.startswith("/api/"):
            raise
        resp = web.json_response({"error": exc.text or exc.reason}, status=exc.status)
    except MediaError as exc:
        resp = web.json_response({"error": str(exc)}, status=400)
    except Exception:  # noqa: BLE001
        log.exception("Dashboard-Anfrage %s fehlgeschlagen", request.path)
        resp = web.json_response({"error": "Interner Fehler – siehe Log."}, status=500)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _client_ip(request: web.Request) -> str:
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote or "")


def _https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto") == "https" \
        or bool(request.headers.get("CF-Visitor", "").count("https"))


class Dashboard:
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self.auth = bot.webauth
        self._runner: web.AppRunner | None = None
        self._index_html = self._build_index()
        self.app = web.Application(middlewares=[_security], client_max_size=64 * 1024)
        r = self.app.router
        r.add_get("/", self.index)
        r.add_static("/static/", STATIC_DIR, show_index=False)
        r.add_post("/api/link", self.link)
        r.add_post("/api/logout", self.logout)
        r.add_get("/api/state", self.state)
        r.add_get("/api/library", self.library)
        r.add_get("/api/search", self.search)
        r.add_post("/api/cmd", self.cmd)
        r.add_post("/api/add", self.add)
        r.add_post("/api/queue", self.queue)
        r.add_post("/api/fade", self.fade)
        r.add_post("/api/playlist", self.playlist)

    # --------------------------------------------------------------- Lifecycle
    async def start(self) -> None:
        cfg = self.bot.cfg
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, cfg.web_host, cfg.web_port)
        await site.start()
        log.info("Dashboard laeuft auf http://%s:%d", cfg.web_host, cfg.web_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------- Auth
    def _user(self, request: web.Request) -> str:
        user = self.auth.session_user(request.cookies.get(COOKIE))
        if not user or not self.bot._allowed(user):
            raise web.HTTPUnauthorized(text="Bitte einloggen.")
        return user

    async def index(self, request: web.Request) -> web.StreamResponse:
        resp = web.Response(text=self._index_html, content_type="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @staticmethod
    def _build_index() -> str:
        """index.html mit Versions-Anhang an allen /static/-Links (?v=<Hash der Dateien>), damit
        Browser und Cloudflare nach einem Update nie alte Dateien ausliefern."""
        import hashlib
        import re
        digest = hashlib.sha256()
        for name in sorted(os.listdir(STATIC_DIR)):
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                digest.update(name.encode() + fh.read())
        version = digest.hexdigest()[:10]
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        return re.sub(r'(["(])(/static/[\w.-]+)', rf"\1\2?v={version}", html)

    async def link(self, request: web.Request) -> web.StreamResponse:
        data = await request.json()
        found = self.auth.login_link(str(data.get("token", ""))[:200], _client_ip(request))
        if found is None:
            return web.json_response(
                {"error": "Der Link ist abgelaufen. Schreib !dashboard im Chat für einen neuen."},
                status=401)
        user, token = found
        return self._session_response(request, user, token)

    def _session_response(self, request: web.Request, user: str, token: str) -> web.StreamResponse:
        resp = web.json_response({"ok": True, "user": user})
        resp.set_cookie(COOKIE, token, max_age=90 * 86400, httponly=True,
                        secure=_https(request), samesite="Strict", path="/")
        return resp

    async def logout(self, request: web.Request) -> web.StreamResponse:
        self.auth.logout(request.cookies.get(COOKIE))
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    # -------------------------------------------------------------------- API
    async def state(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        return web.json_response(self.bot.web_state(user))

    async def library(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        return web.json_response(await self.bot.web_library(user))

    async def search(self, request: web.Request) -> web.StreamResponse:
        self._user(request)
        q = request.query.get("q", "").strip()[:200]
        return web.json_response({"results": await self.bot.resolver.search_async(q) if q else []})

    async def cmd(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        data = await request.json()
        await self.bot.web_command(user, str(data.get("name", "")), str(data.get("arg", ""))[:300])
        return web.json_response(self.bot.web_state(user))

    async def add(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        data = await request.json()
        await self.bot.web_add(user, str(data.get("query", ""))[:500], str(data.get("mode", "end")))
        return web.json_response(self.bot.web_state(user))

    async def queue(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        data = await request.json()
        self.bot.web_queue(user, str(data.get("op", "")), int(data.get("index", -1)),
                           int(data.get("to", -1)))
        return web.json_response(self.bot.web_state(user))

    async def fade(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        data = await request.json()
        await self.bot.web_fade(user, data)
        return web.json_response(self.bot.web_state(user))

    async def playlist(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        data = await request.json()
        await self.bot.web_playlist(user, str(data.get("op", "")), str(data.get("name", "")),
                                    str(data.get("key", "")), int(data.get("index", 0) or 0),
                                    str(data.get("query", ""))[:500])
        return web.json_response(await self.bot.web_library(user))
