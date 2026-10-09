#!/usr/bin/env bash
# Рендер макетов бота в PNG через Open Design.
#
#   bash design/bot/tools/screens/render.sh                    # все 9 экранов + борд
#   bash design/bot/tools/screens/render.sh 01-start-new       # только один
#
# Требует: запущенный Open Design (демон 127.0.0.1:65445) и собранные HTML
# в design/bot/screens/ (см. build.py). PNG — артефакт, руками не правится.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SCREENS_DIR="${SCREENS_DIR:-$ROOT/design/bot/screens}"
RENDERS_DIR="${RENDERS_DIR:-$ROOT/design/bot/renders}"
OD_DATA="${OD_DATA:-$HOME/Library/Application Support/Open Design/namespaces/release-stable/data/projects/kometa-admin}"
OD_CLI="${OD_CLI:-/Applications/Open Design.app/Contents/Resources/app/prebundled/daemon/daemon-cli.mjs}"
DAEMON_URL="${DAEMON_URL:-http://127.0.0.1:65445}"
PROJECT="${OD_PROJECT:-kometa-admin}"

ALL=(00-board 01-start-new 02-start-active 03-start-expired 04-menu-active \
     05-plans 06-pay 07-connect 08-support 09-error-node)

NAMES=("$@")
if [ ${#NAMES[@]} -eq 0 ]; then NAMES=("${ALL[@]}"); fi

[ -d "$OD_DATA" ] || { echo "нет проекта Open Design: $OD_DATA" >&2; exit 1; }
[ -f "$OD_CLI" ] || { echo "нет CLI Open Design: $OD_CLI" >&2; exit 1; }
mkdir -p "$RENDERS_DIR"

for name in "${NAMES[@]}"; do
  src="$SCREENS_DIR/$name.html"
  [ -f "$src" ] || { echo "· $name — нет $src, сначала соберите build.py"; continue; }
  cp "$src" "$OD_DATA/"
  result="$(node "$OD_CLI" export "$name.html" --project "$PROJECT" \
      --format image --image-format png \
      --out "$RENDERS_DIR/$name.png" --daemon-url "$DAEMON_URL" --json)"
  bytes="$(printf '%s' "$result" | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("bytes") or "ОШИБКА: "+str(d.get("error")))')"
  echo "· $name.png — $bytes байт"
done
