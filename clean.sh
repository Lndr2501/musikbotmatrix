#!/usr/bin/env bash
# Entfernt den Musikbot-Stack RESTLOS: Container, Netzwerk, Volume, Image.
# Danach liegt auf der Platte nichts mehr vom Bot (nur dieser Projektordner,
# den du selbst loeschst).
#
#   ./clean.sh          Container + Volume + Image loeschen
#   ./clean.sh --keep-image   nur Container + Volume (Image behalten)
set -euo pipefail
cd "$(dirname "$0")"

KEEP_IMAGE=0
if [ "${1:-}" = "--keep-image" ]; then
  KEEP_IMAGE=1
fi

echo "1) Stack stoppen, Container + Netzwerk + Volume loeschen ..."
# -v ist der wichtige Teil: ohne bleibt das Docker-Volume 'musikbot_data' stehen.
docker compose down -v --remove-orphans

if [ "$KEEP_IMAGE" -eq 0 ]; then
  echo "2) Image loeschen ..."
  docker rmi musikbotmatrix:latest 2>/dev/null || true
fi

echo "3) Nachpruefen, ob nichts geblieben ist:"
echo "   Container:"
docker ps -a --filter "name=musikbot" --format '     {{.Names}}' || true
echo "   Volumes:"
docker volume ls --filter "name=musikbot" --format '     {{.Name}}' || true

echo
echo "Fertig. Der Projektordner mit .env (Zugangsdaten) bleibt bewusst stehen."
echo "Vollstaendig weg:"
echo "     cd .. && rm -rf '$(basename "$PWD")'"
