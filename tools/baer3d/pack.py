"""PNG-Clips aus baer3d.py in ein Baer-Paket fuer den Bot packen (WebP, ~16 MB statt ~200 MB).

  python tools/baer3d/pack.py CLIPS_DIR PACK_DIR [--quality 88]

CLIPS_DIR ist der --out-Ordner von baer3d.py (Clips + optional desk/ von --desk und
stage/ von --stage).

Danach PACK_DIR auf den Docker-Host kopieren (lesbar fuer UID 10000) und
BAER_ASSETS=/data/<name> setzen.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--quality", type=int, default=88)
    a = ap.parse_args()
    manifest = json.loads((a.clips / "manifest.json").read_text())
    if "units" not in manifest and "anchors" not in manifest:
        raise SystemExit("manifest.json ohne 'anchors' – mit aktuellem baer3d.py neu rendern")
    a.out.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, clip in manifest["clips"].items():
        variants = [name] + ([f"{name}_open"] if clip.get("talk") else [])
        variants += [f"{name}@in", f"{name}@out"] if clip.get("edges") else []
        for variant in variants:
            src, dst = a.clips / variant, a.out / variant
            if dst.exists():
                shutil.rmtree(dst)
            dst.mkdir()
            for png in sorted(src.glob("*.png")):
                target = dst / (png.stem + ".webp")
                Image.open(png).save(target, "WEBP", quality=a.quality, method=4)
                total += target.stat().st_size
            print(variant, len(list(dst.iterdir())), "Bilder")
    desk = a.clips / "desk"                       # Pult: baer3d.py --desk
    if (desk / "desk.json").exists():
        info = json.loads((desk / "desk.json").read_text())
        dst = a.out / "desk"
        dst.mkdir(exist_ok=True)
        for key in ("base", "glow"):
            target = dst / f"{key}.webp"
            Image.open(desk / f"{key}.png").save(target, "WEBP", quality=92, method=4)
            total += target.stat().st_size
        manifest["desk"] = {"size": info["size"], "regions": info["regions"]}
        print("desk", ", ".join(info["regions"]))
    else:
        print("ohne Pult (baer3d.py --desk fehlt) – der Bot zeichnet das einfache")
    stage = a.clips / "stage"                     # Buehne: baer3d.py --stage
    if (stage / "stage.json").exists():
        info = json.loads((stage / "stage.json").read_text())
        dst = a.out / "stage"
        dst.mkdir(exist_ok=True)
        target = dst / "bg.webp"
        Image.open(stage / "bg.png").convert("RGB").save(target, "WEBP", quality=90, method=4)
        total += target.stat().st_size
        manifest["stage"] = {k: info[k] for k in ("size", "oy", "regions", "spots")}
        print("stage", len(info["spots"]), "Scheinwerfer")
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"MB {total / 1e6:.1f}")


if __name__ == "__main__":
    main()
