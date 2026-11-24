"""Posen-Paket fuer den Baer-DJ bauen: freigestellte Standbilder auf Buehne und Pult eines 3D-Pakets.

    python tools/posen_paket.py --vorlage /data/baer-3d --out /data/baer-posen \\
        groove:neutral=neutral.png groove:mix=mixen.png groove:sway=links.png,rechts.png \\
        hype:cheer=jubeln.png talk:talk=sprechen-zu.png+sprechen-offen.png \\
        shout:shout=rufen-zu.png+rufen-offen.png special:coffee=kaffee.png react:winken=winken.png

Jede Pose ist ``Sorte:Name=Datei`` (Sorten: groove, hype, special, talk, shout, react). Mehrere
Dateien mit Komma ergeben einen Loop (gleichmaessig ueber 4 Beats verteilt, ``links,rechts`` wechselt
alle 2 Beats), ``+Datei`` haengt das Bild mit offenem Mund an (Lippen-Sync beim Reden). Clips der
Sorte ``react`` waehlt der Baer nie selbst, nur ueber ``events``/``moods``/``seasons``. Alle Bilder
muessen gleich gross und freigestellt sein (RGBA) und den Baeren im selben Massstab zeigen –
die erste Pose legt fest, wo Kopf und Fuesse landen (Kopf auf ``anchors.head``, Fuesse auf
``anchors.floor`` der Vorlage). ``--groesse 1.1`` macht den Baeren 10 % groesser (Fuesse bleiben).
Buehne (``stage/``) und Pult (``desk/``) werden aus der Vorlage kopiert.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image

KINDS = ("groove", "hype", "special", "talk", "shout", "react")


def parse_pose(spec: str) -> tuple[str, str, list[Path], Path | None]:
    """``Sorte:Name=Datei[,Datei…][+Mund-offen]`` -> (Sorte, Name, Loop-Bilder, Bild mit offenem Mund)."""
    try:
        kind_name, files = spec.split("=", 1)
        kind, name = kind_name.split(":", 1)
    except ValueError:
        raise SystemExit(f"Pose '{spec}': erwartet Sorte:Name=Datei") from None
    if kind not in KINDS:
        raise SystemExit(f"Pose '{spec}': Sorte muss eine von {', '.join(KINDS)} sein")
    if not name.isidentifier():
        raise SystemExit(f"Pose '{spec}': Name nur aus Buchstaben/Ziffern/_")
    files, _, mouth = files.partition("+")
    frames = [Path(f) for f in files.split(",") if f]
    if not frames:
        raise SystemExit(f"Pose '{spec}': keine Datei")
    if mouth and len(frames) > 1:
        raise SystemExit(f"Pose '{spec}': offener Mund nur fuer Einzelbilder")
    return kind, name, frames, Path(mouth) if mouth else None


def placement(ref: Image.Image, canvas: tuple[int, int], head: float, floor: float,
              size: float = 1.0) -> tuple[float, float, float]:
    """(Skala, dx, dy): Quellbild -> Leinwand der Vorlage, Kopf auf `head`, Fuesse auf `floor`."""
    box = ref.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if box is None:
        raise SystemExit("Erste Pose ist leer (kein Alphakanal?)")
    left, top, right, bottom = box
    scale = (floor - head) / (bottom - top) * size
    dx = canvas[0] / 2 - (left + right) / 2 * scale
    dy = floor - bottom * scale
    return scale, dx, dy


def place(img: Image.Image, canvas: tuple[int, int], scale: float, dx: float, dy: float) -> Image.Image:
    w, h = round(img.width * scale), round(img.height * scale)
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    small = img.resize((w, h), Image.LANCZOS)
    out.paste(small, (round(dx), round(dy)), small)
    return out


def build(vorlage: Path, out: Path, poses: list[tuple], size: float = 1.0, quality: int = 92) -> dict:
    """poses: (Sorte, Name, Datei) oder (Sorte, Name, [Loop-Dateien], Mund-offen|None)."""
    m = json.loads((vorlage / "manifest.json").read_text())
    if "anchors" not in m or "size" not in m:
        raise SystemExit("Vorlage braucht 'anchors' und 'size' (3D-Paket)")
    canvas = tuple(int(v) for v in m["size"])
    images = []
    for pose in poses:
        kind, name, frames = pose[:3]
        mouth = pose[3] if len(pose) > 3 else None
        frames = [frames] if isinstance(frames, (str, Path)) else frames
        images.append((kind, name, [Image.open(f).convert("RGBA") for f in frames],
                       Image.open(mouth).convert("RGBA") if mouth else None))
    sizes = {img.size for _, _, fr, mo in images for img in fr + ([mo] if mo else [])}
    if len(sizes) != 1:
        raise SystemExit(f"Posen haben verschiedene Groessen: {sorted(sizes)}")
    scale, dx, dy = placement(images[0][2][0], canvas, m["anchors"]["head"], m["anchors"]["floor"], size)
    out.mkdir(parents=True, exist_ok=True)
    clips = {}
    for kind, name, frames, mouth in images:
        for variant, imgs in ((name, frames), (name + "_open", [mouth] if mouth else [])):
            d = out / variant
            if d.exists():
                shutil.rmtree(d)
            if not imgs:
                continue
            d.mkdir()
            for i, img in enumerate(imgs):
                place(img, canvas, scale, dx, dy).save(d / f"{i:03d}.webp", quality=quality, method=6)
        clips[name] = {"kind": kind, "talk": True} if mouth else {"kind": kind}
    for sub in ("stage", "desk"):
        if (vorlage / sub).is_dir():
            shutil.copytree(vorlage / sub, out / sub, dirs_exist_ok=True)
    manifest = {"bob": True, "fpb": m.get("fpb", 16), "beats": m.get("beats", 4), "size": list(canvas),
                "anchors": m["anchors"], "clips": clips}
    for key in ("stage", "desk"):
        if key in m:
            manifest[key] = m[key]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vorlage", type=Path, required=True, help="3D-Paket mit anchors, stage/ und desk/")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--groesse", type=float, default=1.0, help="Faktor auf die Baerengroesse (Fuesse bleiben)")
    ap.add_argument("--qualitaet", type=int, default=92, help="WebP-Qualitaet")
    ap.add_argument("posen", nargs="+", help="Sorte:Name=Datei, die erste legt die Lage fest")
    a = ap.parse_args()
    m = build(a.vorlage, a.out, [parse_pose(p) for p in a.posen], a.groesse, a.qualitaet)
    print(f"{a.out}: {len(m['clips'])} Posen ({', '.join(n + '=' + c['kind'] for n, c in m['clips'].items())})")


if __name__ == "__main__":
    main()
