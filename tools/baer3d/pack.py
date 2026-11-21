"""PNG-Clips aus baer3d.py in ein Baer-Paket fuer den Bot packen (WebP, ~16 MB statt ~200 MB).

  python tools/baer3d/pack.py CLIPS_DIR PACK_DIR [--quality 88]

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
    if "units" not in manifest:
        raise SystemExit("manifest.json ohne 'units' – mit aktuellem baer3d.py neu rendern")
    a.out.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, clip in manifest["clips"].items():
        for variant in [name] + ([f"{name}_open"] if clip.get("talk") else []):
            src, dst = a.clips / variant, a.out / variant
            if dst.exists():
                shutil.rmtree(dst)
            dst.mkdir()
            for png in sorted(src.glob("*.png")):
                target = dst / (png.stem + ".webp")
                Image.open(png).save(target, "WEBP", quality=a.quality, method=4)
                total += target.stat().st_size
            print(variant, len(list(dst.iterdir())), "Bilder")
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"MB {total / 1e6:.1f}")


if __name__ == "__main__":
    main()
