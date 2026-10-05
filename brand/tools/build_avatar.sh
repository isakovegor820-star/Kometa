#!/usr/bin/env bash
# Полный цикл сборки аватара Kometa: исходники → рендер Open Design → готовые PNG.
#
#   bash brand/tools/build_avatar.sh
#
# Требуется запущенный Open Design (демон) и Node. Тяжёлую работу (растеризацию
# в Chromium) делает Open Design; скрипт только синхронизирует исходники в его
# проект и режет результат.
set -euo pipefail

BRAND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="$(cd "$BRAND_DIR/.." && pwd)"
RAW_DIR="${RAW_DIR:-/tmp/od-avatar-raw}"

OD_APP="${OD_APP:-/Applications/Open Design.app/Contents/Resources/app}"
OD_CLI="$OD_APP/prebundled/daemon/daemon-cli.mjs"
OD_DATA="${OD_DATA:-$HOME/Library/Application Support/Open Design/namespaces/release-stable/data}"
PROJECT_ID="${OD_PROJECT_ID:-kometa-brand}"
PROJECT_DIR="$OD_DATA/projects/$PROJECT_ID"
DAEMON_URL="${OD_DAEMON_URL:-http://127.0.0.1:65445}"
NODE="${OD_NODE_BIN:-$(command -v node)}"
PYTHON="${PYTHON:-python3}"

[ -f "$OD_CLI" ] || { echo "не найден Open Design CLI: $OD_CLI" >&2; exit 1; }
[ -n "$NODE" ] || { echo "не найден node" >&2; exit 1; }

echo "== 1/4 подготовка исходников"
"$PYTHON" "$BRAND_DIR/tools/make_assets.py" prepare --brand "$BRAND_DIR"

echo "== 2/4 проект Open Design: $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" "$RAW_DIR"
cp "$BRAND_DIR"/source/*.html "$PROJECT_DIR"/

echo "== 3/4 рендер в Chromium Open Design"
render() {
  local name="$1"
  "$NODE" "$OD_CLI" export "$name.html" --project "$PROJECT_ID" --format image \
    --image-format png --out "$RAW_DIR/$name.raw.png" --daemon-url "$DAEMON_URL" --json >/dev/null
  echo "   $name → $RAW_DIR/$name.raw.png"
}
for name in avatar-a-comet-arrow avatar-b-growth-bars avatar-c-monogram-k; do
  render "$name" &
done
wait
render preview-sheet

echo "== 4/4 нарезка ассетов"
"$PYTHON" "$BRAND_DIR/tools/make_assets.py" finalize --brand "$BRAND_DIR" --raw "$RAW_DIR"

echo
echo "готово:"
find "$BRAND_DIR/avatar" -name '*.png' | sed "s|$WORKSPACE/||" | sort
