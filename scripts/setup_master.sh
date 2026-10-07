#!/usr/bin/env bash
# =============================================================================
#  Kometa — мастер-панель и первая локация за один запуск
# =============================================================================
#  Что делает по шагам:
#    1) ставит 3x-ui (scripts/install_panel.sh), если панели ещё нет;
#    2) берёт из базы панели фактический порт и web base path;
#    3) выпускает API-токен со скоупом admin (или переиспользует рабочий из .env);
#    4) создаёт инбаунды VLESS+Reality и AmneziaWG (scripts/install_node.sh);
#    5) читает ID этих инбаундов прямо из API панели;
#    6) аккуратно вписывает в .env: PANEL_TYPE, PANEL_URL, PANEL_TOKEN,
#       PANEL_INBOUND_IDS, PANEL_SUB_BASE, PUBLIC_BASE_URL
#       (через scripts/configure_env.py — с резервной копией .env);
#    7) печатает, что осталось сделать.
#
#  Запуск:   sudo bash scripts/setup_master.sh
#  Примеры:  sudo bash scripts/setup_master.sh --dry-run          # показать план
#            sudo bash scripts/setup_master.sh --public-ip 1.2.3.4 --web-port 8090
#
#  Идемпотентность: повторный запуск безопасен — панель не переустанавливается,
#  инбаунды ищутся по remark, ID инбаундов добавляются к уже перечисленным,
#  .env получает резервную копию перед правкой.
# =============================================================================

set -euo pipefail

XUI_DIR="/usr/local/x-ui"
XUI_BIN="${XUI_DIR}/x-ui"
XUI_DB="/etc/x-ui/x-ui.db"
XUI_INSTALL_RESULT="/etc/x-ui/install-result.env"
TOKEN_NAME="${TOKEN_NAME:-kometa-bot}"
REALITY_REMARK="${REALITY_REMARK:-Kometa-Reality-443}"
AWG_REMARK="${AWG_REMARK:-Kometa-AWG-51820}"

PUBLIC_IP=""
PANEL_PORT="${PANEL_PORT:-54321}"
WEB_PORT=""
DRY_RUN=0
START=0
# Режим ноды: панель ставится для новой страны, .env не трогаем — вместо этого
# печатаем блок для админки бота (/admin/nodes).
NODE_MODE=0
NODE_CODE=""
NODE_TITLE=""
CURL_OPTS=()

# ------------------------------------------------------------------- вывод ----
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_OFF=""
fi
log()  { printf '%s[•]%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()   { printf '%s[✓]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '%s[✗]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

box() {
    printf '\n%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
}

usage() {
    cat <<'EOF'
Использование: sudo bash scripts/setup_master.sh [флаги]

  --public-ip IP    публичный адрес сервера (по умолчанию определится сам)
  --panel-port N    порт панели 3x-ui при первой установке (по умолчанию 54321)
  --web-port N      порт веб-слоя бота для ссылки-подписки (по умолчанию WEB_PORT из .env)
  --node            режим ноды: поставить панель для новой страны и напечатать
                    данные для админки бота (файл .env не изменяется)
  --code КОД        код страны для ноды, например fi (с --node)
  --title ИМЯ       название локации, например «🇫🇮 Финляндия» (с --node)
  --dry-run         показать план и ничего не менять
  --start           после настройки запустить контейнеры и preflight
  -h, --help        эта справка
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
CONFIGURE_ENV="${SCRIPT_DIR}/configure_env.py"

have() { command -v "$1" >/dev/null 2>&1; }

run() { # run <команда...> — выполнить, а в dry-run только показать
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '   %s[dry-run]%s %s\n' "$C_YELLOW" "$C_OFF" "$*"
        return 0
    fi
    "$@"
}

require_root() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        [[ "${EUID:-$(id -u)}" -eq 0 ]] || warn "dry-run без root: показываю только план, менять ничего не буду."
        return 0
    fi
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Запусти от root:  sudo bash scripts/setup_master.sh"
}

check_deps() {
    local missing=()
    for tool in curl jq openssl sqlite3; do
        have "$tool" || missing+=("$tool")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            warn "Для боевого запуска не хватает: ${missing[*]} (на сервере поставит install_panel.sh)"
        else
            die "Не хватает инструментов: ${missing[*]}. Поставь: apt-get install -y ${missing[*]}"
        fi
    fi
    [[ -f "$CONFIGURE_ENV" ]] || die "Не найден ${CONFIGURE_ENV} — без него .env не правим."
}

env_get() { # env_get <ИМЯ> — значение из .env без выполнения файла
    local key="$1" line
    [[ -f "$ENV_FILE" ]] || return 1
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

# --------------------------------------------------------------- параметры ----
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --public-ip)  PUBLIC_IP="${2:?--public-ip требует значение}"; shift 2 ;;
            --panel-port) PANEL_PORT="${2:?--panel-port требует значение}"; shift 2 ;;
            --web-port)   WEB_PORT="${2:?--web-port требует значение}"; shift 2 ;;
            --dry-run)    DRY_RUN=1; shift ;;
            --node)       NODE_MODE=1; shift ;;
            --code)       NODE_CODE="${2:?--code требует значение}"; shift 2 ;;
            --title)      NODE_TITLE="${2:?--title требует значение}"; shift 2 ;;
            --start)      START=1; shift ;;
            -h | --help)  usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done
}

detect_ip() {
    [[ -n "$PUBLIC_IP" ]] && return 0
    PUBLIC_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    [[ -n "$PUBLIC_IP" ]] || PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -n "$PUBLIC_IP" ]] || die "Не смог определить публичный IP — передай --public-ip."
}

resolve_web_port() {
    if [[ -z "$WEB_PORT" ]]; then
        WEB_PORT="$(env_get WEB_PORT || true)"
    fi
    WEB_PORT="${WEB_PORT:-8090}"
}

# ------------------------------------------------------------------ панель ----
panel_installed() { [[ -x "$XUI_BIN" ]]; }

read_panel_settings() { # фактический порт и base path из базы панели
    local port path
    port="$(sqlite3 "$XUI_DB" "select value from settings where key='webPort' limit 1;" 2>/dev/null || true)"
    path="$(sqlite3 "$XUI_DB" "select value from settings where key='webBasePath' limit 1;" 2>/dev/null || true)"
    printf '%s\n%s\n' "${port:-}" "${path:-}"
}

step_panel() {
    box "ШАГ 1/5 • ПАНЕЛЬ 3x-ui"
    if panel_installed; then
        ok "Панель уже установлена — установку пропускаю."
        return 0
    fi
    local base_path
    base_path="$(openssl rand -hex 10)"
    log "Ставлю панель: порт ${PANEL_PORT}, web base path ${base_path}"
    run bash "${SCRIPT_DIR}/install_panel.sh" --port "$PANEL_PORT" --web-base-path "$base_path" \
        || die "install_panel.sh завершился с ошибкой — смотри его вывод выше."
}

step_panel_url() {
    box "ШАГ 2/5 • АДРЕС ПАНЕЛИ"
    local port path
    { read -r port; read -r path; } < <(read_panel_settings)
    port="${port:-$PANEL_PORT}"
    if [[ -z "$path" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            path="<web_base_path>"
            warn "Базы панели нет — в dry-run показываю заготовку адреса."
        else
            die "Не вижу webBasePath в ${XUI_DB}. Проверь вручную: ${XUI_BIN} setting -show"
        fi
    fi
    path="/${path#/}"      # ровно один ведущий слэш
    path="${path%/}"       # без хвостового слэша
    PANEL_URL="http://${PUBLIC_IP}:${port}${path}"
    ok "PANEL_URL = ${PANEL_URL}"
}

step_token() {
    box "ШАГ 3/5 • API-ТОКЕН (скоуп admin)"
    local existing
    existing="$(env_get PANEL_TOKEN || true)"
    if [[ -n "$existing" ]] && api_ok "$existing"; then
        ok "Токен из .env рабочий — переиспользую (новый не выпускаю)."
        PANEL_TOKEN="$existing"
        return 0
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "[dry-run] выпустил бы новый токен: ${XUI_BIN} setting -getApiToken -tokenName ${TOKEN_NAME} -tokenScope admin"
        PANEL_TOKEN="DRY_RUN_TOKEN"
        return 0
    fi
    local out token
    out="$("$XUI_BIN" setting -getApiToken -tokenName "$TOKEN_NAME" -tokenScope admin)"
    token="$(printf '%s' "$out" | grep -Eo 'apiToken: .+' | awk '{print $2}' || true)"
    [[ -n "$token" ]] || die "Не удалось выпустить токен. Вывод команды: ${out}"
    PANEL_TOKEN="$token"
    ok "Токен выпущен (показывается один раз, но уже сохранён в .env на шаге 5)."
}

api_ok() { # api_ok <токен> — отвечает ли панель на этот токен
    local token="$1" resp
    [[ "$DRY_RUN" -eq 1 ]] && return 1
    resp="$(curl -fsS "${CURL_OPTS[@]}" -m 10 -H "Authorization: Bearer ${token}" \
        "${PANEL_URL}/panel/api/inbounds/list" 2>/dev/null || true)"
    [[ "$(printf '%s' "$resp" | jq -r '.success // false' 2>/dev/null)" == "true" ]]
}

list_inbounds() { # JSON со списком инбаундов панели
    curl -fsS "${CURL_OPTS[@]}" -m 20 -H "Authorization: Bearer ${PANEL_TOKEN}" \
        "${PANEL_URL}/panel/api/inbounds/list"
}

inbound_id_by_remark() { # inbound_id_by_remark <json> <remark>
    printf '%s' "$1" | jq -r --arg r "$2" '[.obj[]? | select(.remark == $r) | .id] | first // empty'
}

step_inbounds() {
    box "ШАГ 4/5 • ИНБАУНДЫ ЛОКАЦИИ (Reality + AmneziaWG)"
    run bash "${SCRIPT_DIR}/install_node.sh" --panel-url "$PANEL_URL" --panel-token "$PANEL_TOKEN" \
        || die "install_node.sh завершился с ошибкой — смотри его вывод выше."
    if [[ "$DRY_RUN" -eq 1 ]]; then
        REALITY_ID="1"; AWG_ID="2"
        log "[dry-run] ID инбаундов прочитал бы из API панели"
        return 0
    fi
    local list
    list="$(list_inbounds)" || die "Панель не отдала список инбаундов."
    REALITY_ID="$(inbound_id_by_remark "$list" "$REALITY_REMARK")"
    AWG_ID="$(inbound_id_by_remark "$list" "$AWG_REMARK")"
    [[ -n "$REALITY_ID" ]] || die "Не нашёл инбаунд ${REALITY_REMARK} в панели."
    [[ -n "$AWG_ID" ]] || warn "Инбаунд ${AWG_REMARK} не найден — резервная локация не настроена."
}

merge_ids() { # merge_ids <существующий список> <новые id...>
    local existing="$1"; shift
    local result="" item
    for item in ${existing//,/ } "$@"; do
        [[ -n "$item" ]] || continue
        case ",${result}," in
            *",${item},"*) ;;
            *) result="${result:+${result},}${item}" ;;
        esac
    done
    printf '%s' "$result"
}

step_env() {
    box "ШАГ 5/5 • ЗАПИСЬ В .env"
    local ids previous
    previous="$(env_get PANEL_INBOUND_IDS || true)"
    ids="$(merge_ids "$previous" "$REALITY_ID" "${AWG_ID:-}")"
    [[ -n "$ids" ]] || die "Не получилось собрать список ID инбаундов."

    local args=(
        --env "$ENV_FILE"
        --set "PANEL_TYPE=xui"
        --set "PANEL_URL=${PANEL_URL}"
        --set "PANEL_TOKEN=${PANEL_TOKEN}"
        --set "PANEL_INBOUND_IDS=${ids}"
        --set "PANEL_SUB_BASE=http://${PUBLIC_IP}:2096/sub/"
        --set "PUBLIC_BASE_URL=http://${PUBLIC_IP}:${WEB_PORT}"
        --set "PANEL_USERNAME="
        --set "PANEL_PASSWORD="
    )
    [[ "$DRY_RUN" -eq 1 ]] && args+=(--dry-run)

    if have python3; then
        python3 "$CONFIGURE_ENV" "${args[@]}"
    else
        die "Нет python3 — .env придётся поправить руками (см. docs/МУЛЬТИГЕО-ЗАПУСК.md)."
    fi
}

print_next() {
    box "ГОТОВО • ЧТО ОСТАЛОСЬ"
    cat <<EOF
  Панель            : ${PANEL_URL}
  Логин и пароль    : в ${XUI_INSTALL_RESULT} (права 600, только root)
  Порты             : 443/tcp (Reality), 51820/udp (AmneziaWG), 2096/tcp (подписки панели)
  Инбаунды в .env   : ${REALITY_ID:-1}${AWG_ID:+,${AWG_ID}}
  Ссылка-подписка   : http://${PUBLIC_IP}:${WEB_PORT}/sub/<токен>

  Дальше:
    cd ${ROOT_DIR}
    docker compose up -d
    bash scripts/preflight.sh          # ждём «блокеров 0»
    curl -s http://127.0.0.1:${WEB_PORT}/health

  Затем в Telegram: /start → «Попробовать бесплатно» → подключись с телефона.
  Только после успешного подключения включай продажи: SALES_ENABLED=true.
EOF
}

print_node_details() {
    box "ГОТОВО • ДАННЫЕ ДЛЯ АДМИНКИ БОТА"
    local ids="${REALITY_ID:-1}${AWG_ID:+,${AWG_ID}}"
    cat <<EOF
  Открой админ-панель бота → «Ноды и панель» → «Добавить страну» и впиши:

    код ................ ${NODE_CODE:-<код: fi, nl, jp, us>}
    название ........... ${NODE_TITLE:-<🇫🇮 Финляндия>}
    IP ................. ${PUBLIC_IP}
    адрес панели ....... ${PANEL_URL}
    API-токен .......... ${PANEL_TOKEN}
    ID инбаундов ....... ${ids}

  Перед сохранением открой панель для бота (только с IP основного сервера):
    ufw allow from ${MASTER_IP:-<IP основного сервера>} to any port ${PANEL_PORT} proto tcp
    ufw allow 443/tcp && ufw allow 51820/udp

  Логин и пароль панели: ${XUI_INSTALL_RESULT} (права 600, только root)
  После сохранения выполни в боте команду /sync — клиенты получат новую страну.
EOF
}

# ------------------------------------------------------------------- main -----
main() {
    parse_args "$@"
    if [[ "$NODE_MODE" -eq 1 ]]; then
        box "KOMETA • НОВАЯ СТРАНА (НОДА)"
    else
        box "KOMETA • МАСТЕР-ПАНЕЛЬ + ПЕРВАЯ ЛОКАЦИЯ"
    fi
    require_root
    check_deps
    detect_ip
    resolve_web_port
    log "Сервер: ${PUBLIC_IP} • веб-слой: ${WEB_PORT} • .env: ${ENV_FILE}"
    [[ "$DRY_RUN" -eq 1 ]] && warn "Режим dry-run: ничего не меняется, показываю только план."

    step_panel
    step_panel_url
    step_token
    step_inbounds
    if [[ "$NODE_MODE" -eq 1 ]]; then
        print_node_details
        return 0
    fi
    step_env
    print_next

    if [[ "$START" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
        box "ЗАПУСКАЮ КОНТЕЙНЕРЫ"
        ( cd "$ROOT_DIR" && docker compose up -d )
        ( cd "$ROOT_DIR" && bash scripts/preflight.sh ) || true
    fi
}

# Точка входа. Гвардия нужна, чтобы скрипт можно было подключать в тестах
# (`source scripts/setup_master.sh`) и проверять отдельные функции.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
