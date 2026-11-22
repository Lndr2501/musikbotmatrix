"""Publikum: wer im Call ist, schwebt als runder Avatar im Hintergrund des Baer-Bilds.

Teilnehmer kommen aus LiveKit (wer wirklich verbunden ist), Name und Avatar aus dem
synchronisierten Raumzustand. Avatare werden einmal geladen und pro mxc-Adresse gemerkt.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Optional

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from .matrix import Matrix

log = logging.getLogger(__name__)

SIZE = 160          # Kantenlaenge der vorbereiteten Avatare (Pixel)
MAX_FACES = 8


def user_of(identity: str) -> str:
    """LiveKit-Identity '@lndr:server:GERAET' -> '@lndr:server'."""
    if identity.count(":") >= 2:
        return identity.rsplit(":", 1)[0]
    return identity


def round_avatar(data: bytes, size: int = SIZE) -> Optional[Image.Image]:
    """Bild -> quadratisch zugeschnitten, rund maskiert, RGBA."""
    try:
        img = Image.open(io.BytesIO(data))
        img.seek(0)                          # animierte Avatare: erstes Bild
        img = img.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


class Publikum:
    def __init__(self, matrix: "Matrix") -> None:
        self.matrix = matrix
        self._cache: dict[str, Optional[Image.Image]] = {}

    async def faces(self, room_id: str, users: list[str]) -> list[tuple[str, Optional[Image.Image]]]:
        """[(Anzeigename, runder Avatar oder None)] in der Reihenfolge von `users`."""
        try:
            room = self.matrix.client.rooms.get(room_id)
        except Exception:  # noqa: BLE001
            room = None
        out = []
        for uid in users[:MAX_FACES]:
            name = uid.lstrip("@").split(":", 1)[0]
            mxc = None
            if room is not None:
                try:
                    name = room.user_name(uid) or name
                    mxc = room.avatar_url(uid)
                except Exception:  # noqa: BLE001
                    pass
            out.append((name, await self._avatar(mxc) if mxc else None))
        return out

    async def _avatar(self, mxc: str) -> Optional[Image.Image]:
        if mxc in self._cache:
            return self._cache[mxc]
        img = None
        try:
            import nio
            resp = await self.matrix.client.download(mxc=mxc)
            if isinstance(resp, nio.MemoryDownloadResponse):
                img = round_avatar(resp.body)
            else:
                log.debug("Avatar %s nicht ladbar: %s", mxc, resp)
        except Exception:  # noqa: BLE001
            log.debug("Avatar %s nicht ladbar", mxc, exc_info=True)
        self._cache[mxc] = img
        return img
