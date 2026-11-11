#!/bin/sh
# Rendert die PNG-Bilder aus den Logo-SVGs (Dashboard-Icons und Bot-Profilbild).
# Braucht rsvg-convert; ohne lokale Installation laeuft es in einem Wegwerf-Container:
#   docker run --rm -v "$PWD":/w -w /w alpine:3.20 sh -c "apk add -q rsvg-convert && sh branding/export.sh"
set -eu
cd "$(dirname "$0")/.."
python3 branding/build.py 2>/dev/null || true   # im Container ohne Python: SVGs liegen schon im Repo
src=musikbot/static/icon.svg
rsvg-convert -w 180 -h 180 "$src" -o musikbot/static/apple-touch-icon.png
rsvg-convert -w 32 -h 32 "$src" -o musikbot/static/favicon-32.png
rsvg-convert -w 512 -h 512 branding/logo/musikbot-pink-square.svg -o musikbot/static/avatar.png
echo "PNG-Bilder in musikbot/static/ (apple-touch-icon, favicon-32, avatar)"
