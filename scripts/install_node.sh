#!/usr/bin/env bash
# =============================================================================
#  Kometa — подготовка VPN-ноды: инбаунды VLESS+Reality и AmneziaWG в 3x-ui
# =============================================================================
#  Скрипт создаёт на панели 3x-ui два инбаунда:
#    1) VLESS + Reality (TCP, по умолчанию 443) — ключи x25519 генерирует xray,
#       shortId — openssl, домен маскировки (SNI) подбирается и ПРОВЕРЯЕТСЯ
#       на поддержку TLS 1.3 + HTTP/2;
#    2) AmneziaWG (UDP, по умолчанию 51820) — штатный протокол 3x-ui начиная
#       с версии 3.7.0 (встроенный amneziawg-go, без kernel-модуля и awg-quick).
#
#  Проверено по исходникам 3x-ui v3.9.0 (октябрь 2026):
#    * протокол инбаунда: "amneziawg" (internal/database/model/model.go, Protocol);
#    * пустые settings у AmneziaWG панель сама заполняет случайной обфускацией
#      и свежей парой ключей (web/service/inbound_amneziawg.go, defaultAmneziaWGServer);
#    * ключи Reality: GET /panel/api/server/getNewX25519Cert;
#    * авторизация API: заголовок "Authorization: Bearer <токен>".
#  Поэтому отдельная установка amneziawg-go/awg-quick НЕ нужна и даже вредна:
#  старый путь (DKMS + awg-quick) в 3x-ui удалён.
#
#  Запуск:   sudo bash scripts/install_node.sh
#  Примеры:  sudo bash scripts/install_node.sh --sni www.microsoft.com
#            sudo bash scripts/install_node.sh --token-only      # нода для мастер-панели
#            sudo bash scripts/install_node.sh --dry-run
#
#  Идемпотентность: повторный запуск не создаёт дубликаты — инбаунды ищутся
#  по remark (Kometa-Reality-443 / Kometa-AWG-51820).
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------- настройки ---
REALITY_PORT="${REALITY_PORT:-443}"
AWG_PORT="${AWG_PORT:-51820}"
REALITY_REMARK="${REALITY_REMARK:-Kometa-Reality-443}"
AWG_REMARK="${AWG_REMARK:-Kometa-AWG-51820}"
PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"
SNI="${SNI:-}"
MODE="inbounds"        # inbounds | token-only
DO_REALITY=1
DO_AWG=1
DRY_RUN=0
TEST_CLIENT=0
FORCE_REMOTE=0
CURL_OPTS=()

XUI_DIR="/usr/local/x-ui"
XUI_BIN="${XUI_DIR}/x-ui"
XUI_INSTALL_RESULT="/etc/x-ui/install-result.env"
NODE_TOKEN_NAME="${NODE_TOKEN_NAME:-kometa-node}"

# Домены-кандидаты для маскировки Reality. Требования: TLS 1.3 + HTTP/2,
# крупный сайт «не по теме VPN», не заблокирован в РФ, не на нашей же IP.
# Порядок = приоритет: скрипт берёт первый, который прошёл проверку.
SNI_CANDIDATES="${SNI_CANDIDATES:-www.microsoft.com dl.google.com www.samsung.com www.lovelive-anime.jp gateway.icloud.com www.bing.com yandex.ru www.ozon.ru}"

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
err()  { printf '%s[✗]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
die()  { err "$*"; exit 1; }

box() {
    printf '\n%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
}

usage() {
    cat <<'EOF'
Подготовка ноды Kometa: инбаунды VLESS+Reality и AmneziaWG в панели 3x-ui.

Использование:
  sudo bash scripts/install_node.sh [флаги]

Флаги:
  --reality-port N   порт VLESS+Reality (по умолчанию 443)
  --awg-port N       порт AmneziaWG, UDP (по умолчанию 51820)
  --sni DOMAIN       домен маскировки Reality (по умолчанию — автоподбор)
  --no-reality       не создавать VLESS+Reality
  --no-awg           не создавать AmneziaWG
  --panel-url URL    адрес панели (по умолчанию PANEL_URL из .env)
  --panel-token T    API-токен панели (по умолчанию PANEL_TOKEN из .env)
  --env FILE         путь к .env (по умолчанию <проект>/.env)
  --token-only       ничего не создавать: выпустить node-sync токен этой ноды
                     и показать, что вписать в мастер-панели (режим «нода+мастер»)
  --test-client      дополнительно создать тестового клиента и напечатать
                     готовую ссылку vless:// для проверки на телефоне
  --force-remote     разрешить создавать инбаунды на удалённой панели
  --insecure         не проверять TLS-сертификат панели (самоподписанный)
  --dry-run          показать план, ничего не менять
  -h, --help         эта справка
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------- .env проекта ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

# env_get <ИМЯ> — значение переменной из .env БЕЗ выполнения файла
# (в .env бывают значения с пробелами, `source` их сломает).
env_get() {
    local key="$1" line
    [[ -f "$ENV_FILE" ]] || return 1
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

load_env() {
    [[ -f "$ENV_FILE" ]] || { warn "Файл ${ENV_FILE} не найден — беру параметры только из флагов."; return 0; }
    if [[ -z "$PANEL_URL" ]]; then PANEL_URL="$(env_get PANEL_URL || true)"; fi
    if [[ -z "$PANEL_TOKEN" ]]; then PANEL_TOKEN="$(env_get PANEL_TOKEN || true)"; fi
    ok "Прочитал .env: ${ENV_FILE}"
}

# ------------------------------------------------------------------ утилиты ---
require_root() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Запусти от root:  sudo bash scripts/install_node.sh"
}

check_deps() {
    local missing=()
    have curl || missing+=(curl)
    have jq || missing+=(jq)
    have openssl || missing+=(openssl)
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Не хватает программ: ${missing[*]}. Установи: apt-get update && apt-get install -y ${missing[*]}"
    fi
    ok "Зависимости на месте (curl, jq, openssl)."
}

local_ip() {
    { hostname -I 2>/dev/null | awk '{print $1}'; } || true
}

is_local_panel() {
    local host
    host="$(printf '%s' "$PANEL_URL" | sed -E 's#^https?://##' | cut -d/ -f1 | cut -d: -f1)"
    case "$host" in
        localhost | 127.0.0.1 | ::1) return 0 ;;
    esac
    local ip; ip="$(local_ip)"
    [[ -n "$ip" && "$host" == "$ip" ]] && return 0
    return 1
}

find_xray() { # путь к бинарю xray из панели (или системному)
    local p
    for p in "${XUI_DIR}"/bin/xray-linux-* "${XUI_DIR}"/bin/xray /usr/local/bin/xray /usr/bin/xray; do
        [[ -x "$p" ]] && { printf '%s' "$p"; return 0; }
    done
    if have xray; then command -v xray; return 0; fi
    return 1
}

gen_x25519_local() { # → "PRIVATE PUBLIC" через локальный `xray x25519`
    local bin out priv pub
    bin="$(find_xray)" || return 1
    out="$("$bin" x25519 2>/dev/null)" || return 1
    # Формат xray: "PrivateKey: ..." / "Password (PublicKey): ..." (в старых —
    # "Private key:" / "Public key:"). Берём значение после ':' из первых двух строк.
    priv="$(printf '%s\n' "$out" | sed -n '1s/^[^:]*:[[:space:]]*//p')"
    pub="$(printf '%s\n' "$out" | sed -n '2s/^[^:]*:[[:space:]]*//p')"
    [[ -n "$priv" && -n "$pub" ]] || return 1
    printf '%s %s' "$priv" "$pub"
}

gen_x25519_api() { # → "PRIVATE PUBLIC" через API панели (когда xray только на панели)
    local resp
    resp="$(api_get /panel/api/server/getNewX25519Cert)" || return 1
    printf '%s %s' \
        "$(printf '%s' "$resp" | jq -r '.obj.privateKey // empty')" \
        "$(printf '%s' "$resp" | jq -r '.obj.publicKey // empty')"
}

rand_shortid() { openssl rand -hex 8 2>/dev/null || true; }
rand_uuid()    { cat /proc/sys/kernel/random/uuid 2>/dev/null || openssl rand -hex 16; }

# -------------------------------------------------------------------- сеть ----
api_get() { # api_get <путь>
    curl -sS --max-time 25 "${CURL_OPTS[@]+"${CURL_OPTS[@]}"}" \
        -H "Authorization: Bearer ${PANEL_TOKEN}" \
        "${PANEL_URL}$1"
}

api_post() { # api_post <путь> <json>
    curl -sS --max-time 30 "${CURL_OPTS[@]+"${CURL_OPTS[@]}"}" \
        -X POST \
        -H "Authorization: Bearer ${PANEL_TOKEN}" \
        -H 'Content-Type: application/json' \
        -d "$2" \
        "${PANEL_URL}$1"
}

api_check() {
    local resp
    log "Проверяю доступ к API панели: ${PANEL_URL}/panel/api/inbounds/list"
    resp="$(api_get /panel/api/inbounds/list)" \
        || die "Панель не отвечает по адресу ${PANEL_URL}. Проверь PANEL_URL, firewall и что служба x-ui запущена."
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        die "Панель ответила отказом: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"'). Проверь PANEL_TOKEN (скоуп admin или node-sync)."
    fi
    ok "API панели доступен, токен принят."
}

panel_version() {
    local v
    v="$(api_get /panel/api/server/status | jq -r '.obj.panelVersion // empty' || true)"
    if [[ -z "$v" ]] && [[ -x "$XUI_BIN" ]]; then
        v="$("$XUI_BIN" -v 2>/dev/null || true)"
    fi
    printf '%s' "$v"
}

version_ge() { # version_ge <A> <B> → 0 если A >= B
    local a="${1#v}" b="${2#v}"
    local a1 a2 b1 b2
    a1="$(printf '%s' "$a" | cut -d. -f1)"; a2="$(printf '%s' "$a" | cut -d. -f2)"
    b1="$(printf '%s' "$b" | cut -d. -f1)"; b2="$(printf '%s' "$b" | cut -d. -f2)"
    a1="${a1:-0}"; a2="${a2:-0}"; b1="${b1:-0}"; b2="${b2:-0}"
    [[ "$a1" -gt "$b1" ]] && return 0
    [[ "$a1" -lt "$b1" ]] && return 1
    [[ "$a2" -ge "$b2" ]]
}

check_sni() { # check_sni <домен> → 0 если TLS 1.3 + h2 + валидный сертификат
    local d="$1" out
    [[ -n "$d" ]] || return 1
    out="$(timeout 12 openssl s_client -connect "${d}:443" -servername "$d" \
        -tls1_3 -alpn h2 </dev/null 2>/dev/null || true)"
    [[ -n "$out" ]] || return 1
    printf '%s' "$out" | grep -q 'ALPN protocol: h2' || return 1
    printf '%s' "$out" | grep -Eq 'Protocol *: *TLSv1\.3' || return 1
    printf '%s' "$out" | grep -q 'Verify return code: 0' || return 1
    # домен не должен указывать на этот же сервер (иначе маскировка бессмысленна)
    local ip; ip="$(local_ip)"
    local sni_ip
    sni_ip="$(getent hosts "$d" 2>/dev/null | awk '{print $1}' | head -n 1 || true)"
    if [[ -n "$ip" && -n "$sni_ip" && "$sni_ip" == "$ip" ]]; then
        warn "Домен ${d} указывает на этот же сервер — пропускаю."
        return 1
    fi
    return 0
}

resolve_sni() {
    if [[ -n "$SNI" ]]; then
        log "Проверяю указанный домен маскировки: ${SNI}"
        check_sni "$SNI" || die "Домен ${SNI} не подходит: нужен рабочий сайт с TLS 1.3 + HTTP/2 и валидным сертификатом. Проверь вручную: openssl s_client -connect ${SNI}:443 -servername ${SNI} -tls1_3 -alpn h2 </dev/null"
        ok "Домен ${SNI} подходит (TLS 1.3 + h2)."
        return 0
    fi
    log "Подбираю домен маскировки (проверяю TLS 1.3 + HTTP/2)..."
    local d
    for d in $SNI_CANDIDATES; do
        if check_sni "$d"; then
            SNI="$d"
            ok "Выбран домен: ${d}"
            return 0
        fi
        warn "Не подошёл: ${d}"
    done
    die "Ни один домен из списка не подошёл. Задай свой: --sni <домен> (например --sni www.microsoft.com)."
}

# --------------------------------------------------------------- инбаунды -----
list_inbounds() { api_get /panel/api/inbounds/list; }

find_inbound_by_remark() { # find_inbound_by_remark <remark> → id или пусто
    printf '%s' "$1" | jq -r --arg r "$2" '[.obj[]? | select(.remark == $r) | .id] | first // empty'
}

create_reality_inbound() {
    local remark="$1" port="$2"
    local keys priv pub sid settings stream sniffing payload resp new_id
    local version

    version="$(panel_version || true)"
    [[ -n "$version" ]] && log "Версия панели: ${version}"

    if keys="$(gen_x25519_local)"; then
        ok "Ключи x25519 сгенерированы локальным xray ($(find_xray))."
    elif keys="$(gen_x25519_api)"; then
        ok "Ключи x25519 сгенерированы панелью (/panel/api/server/getNewX25519Cert)."
    else
        die "Не удалось сгенерировать ключи x25519: нет ни локального xray, ни доступа к API панели."
    fi
    priv="${keys%% *}"
    pub="${keys##* }"
    [[ -n "$priv" && -n "$pub" && "$priv" != "$pub" ]] || die "xray вернул неожиданный формат ключей."

    sid="$(rand_shortid)"
    [[ -n "$sid" ]] || die "Не удалось сгенерировать shortId (openssl rand)."

    # settings/streamSettings — строки с JSON внутри JSON (так устроено API 3x-ui).
    settings='{"clients":[],"decryption":"none","fallbacks":[]}'
    stream="$(jq -nc \
        --arg sni "$SNI" --arg priv "$priv" --arg pub "$pub" --arg sid "$sid" \
        '{network:"tcp",security:"reality",externalProxy:[],
          realitySettings:{show:false,xver:0,target:($sni+":443"),serverNames:[$sni],
            privateKey:$priv,minClientVer:"",maxClientVer:"",maxTimediff:0,
            shortIds:[$sid],
            settings:{publicKey:$pub,fingerprint:"chrome",serverName:"",spiderX:"/"}}}')"
    sniffing='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":false}'

    payload="$(jq -nc --arg remark "$remark" --argjson port "$port" \
        --arg settings "$settings" --arg stream "$stream" --arg sniffing "$sniffing" \
        '{up:0,down:0,total:0,remark:$remark,enable:true,expiryTime:0,listen:"",
          port:$port,protocol:"vless",settings:$settings,streamSettings:$stream,sniffing:$sniffing}')"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "DRY-RUN: VLESS+Reality НЕ создаётся. Параметры:"
        printf '    порт=%s  SNI=%s  shortId=%s\n    publicKey=%s\n' "$port" "$SNI" "$sid" "$pub"
        return 0
    fi

    log "Создаю инбаунд VLESS+Reality (порт ${port}, SNI ${SNI})..."
    resp="$(api_post /panel/api/inbounds/add "$payload")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        die "Панель не создала инбаунд: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')"
    fi
    new_id="$(printf '%s' "$resp" | jq -r '.obj.id // empty')"
    ok "Инбаунд VLESS+Reality создан (id=${new_id:-?}, порт ${port})."
    REALITY_ID="$new_id"
    REALITY_PUBKEY="$pub"
    REALITY_SHORTID="$sid"
}

create_awg_inbound() {
    local remark="$1" port="$2"
    local payload resp new_id version

    version="$(panel_version || true)"
    if [[ -n "$version" ]] && ! version_ge "$version" "3.7.0"; then
        warn "Панель ${version} не умеет AmneziaWG (нужно 3.7.0+)."
        print_awg_unsupported_hint
        return 1
    fi

    # settings: "{}" — панель сама сгенерирует случайную обфускацию (jc/jmin/jmax/
    # s1-s4/h1-h4/i1) и пару ключей сервера: web/service/inbound_amneziawg.go.
    payload="$(jq -nc --arg remark "$remark" --argjson port "$port" \
        '{up:0,down:0,total:0,remark:$remark,enable:true,expiryTime:0,listen:"",
          port:$port,protocol:"amneziawg",settings:"{}",streamSettings:"{}",sniffing:"{}"}')"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "DRY-RUN: AmneziaWG НЕ создаётся (порт UDP ${port})."
        return 0
    fi

    log "Создаю инбаунд AmneziaWG (UDP ${port})..."
    resp="$(api_post /panel/api/inbounds/add "$payload")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        warn "Панель не создала AmneziaWG: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')"
        print_awg_unsupported_hint
        return 1
    fi
    new_id="$(printf '%s' "$resp" | jq -r '.obj.id // empty')"
    ok "Инбаунд AmneziaWG создан (id=${new_id:-?}, UDP ${port})."
    AWG_ID="$new_id"
    return 0
}

print_awg_unsupported_hint() {
    cat <<EOF

  ── Что делать с AmneziaWG ─────────────────────────────────────────────
  В 3x-ui AmneziaWG встроен начиная с версии 3.7.0 (внутри amneziawg-go,
  kernel-модуль и awg-quick больше НЕ используются — этот путь удалён).
  Правильное решение — обновить панель, а не ставить awg-quick рядом:

      # обновление 3x-ui (сохраняет базу и настройки):
      bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)

  После обновления запусти этот скрипт ещё раз — он создаст AmneziaWG-инбаунд.
  ВАЖНО: обновляй и мастер-панель, и все ноды: инбаунд AmneziaWG нельзя
  выкатить на ноду старее 3.7.0.
EOF
}

create_test_client() { # тестовый клиент на Reality-инбаунде + готовая ссылка
    local inbound_id="$1" email="test@kometa" uuid payload resp link
    [[ -n "$inbound_id" ]] || { warn "Нет id Reality-инбаунда — тестовый клиент не создаю."; return 0; }
    uuid="$(rand_uuid)"
    payload="$(jq -nc --arg id "$uuid" --arg email "$email" --argjson iid "$inbound_id" \
        '{client:{id:$id,email:$email,flow:"xtls-rprx-vision",limitIp:0,totalGB:0,
                  expiryTime:0,enable:true,subId:"",tgId:0},inboundIds:[$iid]}')"
    log "Создаю тестового клиента ${email}..."
    resp="$(api_post /panel/api/clients/add "$payload")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        warn "Тестового клиента создать не удалось: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')"
        return 0
    fi
    resp="$(api_get "/panel/api/clients/links/${email}")"
    link="$(printf '%s' "$resp" | jq -r '[.obj | .. | strings | select(startswith("vless://"))] | first // empty')"
    if [[ -n "$link" ]]; then
        box "ССЫЛКА ДЛЯ ПРОВЕРКИ НА ТЕЛЕФОНЕ (v2rayNG / Hiddify / Happ)"
        printf '  %s\n\n' "$link"
    fi
    printf '  Удалить тестового клиента после проверки:\n'
    printf '    curl -X POST -H "Authorization: Bearer <PANEL_TOKEN>" %s/panel/api/clients/del/%s\n\n' "$PANEL_URL" "$email"
}

# --------------------------------------------------- режим «нода для мастера» --
install_result_value() { # значение из /etc/x-ui/install-result.env (пишет сам установщик 3x-ui)
    local key="$1" line
    [[ -r "$XUI_INSTALL_RESULT" ]] || return 1
    line="$(grep -E "^${key}=" "$XUI_INSTALL_RESULT" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    printf '%s' "${line#*=}" | tr -d '"' | tr -d "'"
}

token_only() {
    box "РЕЖИМ НОДЫ ДЛЯ МАСТЕР-ПАНЕЛИ (node-sync)"
    [[ -x "$XUI_BIN" ]] || die "На этой машине нет панели 3x-ui. Сначала: sudo bash scripts/install_panel.sh"
    log "Выпускаю node-sync токен '${NODE_TOKEN_NAME}'..."
    local out token ip port base url
    out="$("$XUI_BIN" setting -getApiToken -tokenName "$NODE_TOKEN_NAME" -tokenScope node-sync)"
    token="$(printf '%s' "$out" | grep -Eo 'apiToken: .+' | awk '{print $2}' || true)"
    [[ -n "$token" ]] || die "Не удалось получить токен. Вывод команды: ${out}"

    ip="$(local_ip)"
    port="$(install_result_value XUI_PANEL_PORT || true)"
    port="${port:-54321}"
    base="$(install_result_value XUI_WEB_BASE_PATH || true)"
    url="http://${ip}:${port}/${base}"

    box "ЧТО ВПИСАТЬ В МАСТЕР-ПАНЕЛИ"
    cat <<EOF
  Мастер-панель → Nodes → «+» (Add Node):
    Адрес ноды : ${url}
    API-токен  : ${token}

  Токен показывается ОДИН РАЗ (в базе панели хранится только его хеш) — сохрани его.
  ВАЖНО: у ноды должен быть открыт порт панели, а версия 3x-ui — не старее 3.7.0,
  иначе мастер не сможет выкатить на неё AmneziaWG-инбаунд.

  После добавления ноды инбаунды создаются в мастере с указанием этой ноды —
  отдельно запускать install_node.sh на ноде НЕ нужно.
  Проверка, что нода отвечает (запускать с мастера):
    curl -s -H "Authorization: Bearer ${token}" ${url}/panel/api/server/status | jq -r '.obj.panelVersion'
EOF
}

# ------------------------------------------------------------------ проверки --
wait_for_port() { # wait_for_port <порт> <tcp|udp> [секунд]
    local port="$1" proto="$2" tries="${3:-10}" i=0
    have ss || return 0
    while [[ "$i" -lt "$tries" ]]; do
        if [[ "$proto" == "tcp" ]]; then
            ss -tln 2>/dev/null | grep -Eq "[:.]${port}[[:space:]]" && return 0
        else
            ss -uln 2>/dev/null | grep -Eq "[:.]${port}[[:space:]]" && return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    return 1
}

print_port_checks() {
    box "ПРОВЕРКА, ЧТО ПОРТЫ СЛУШАЮТСЯ (запусти эти команды)"
    cat <<EOF
  # оба порта сразу:
    ss -tulpn | grep -E ':(${REALITY_PORT}|${AWG_PORT})\b'

  # только VLESS+Reality (TCP):
    ss -tlnp | grep ':${REALITY_PORT}'

  # только AmneziaWG (UDP):
    ss -ulpn | grep ':${AWG_PORT}'

  # снаружи, с другого компьютера (TCP-порт Reality должен отвечать):
    nc -vz <IP_СЕРВЕРА> ${REALITY_PORT}

  # журнал панели, если что-то не поднялось:
    journalctl -u x-ui -n 50 --no-pager

  # логи AmneziaWG внутри панели:
    curl -s -X POST -H "Authorization: Bearer <PANEL_TOKEN>" ${PANEL_URL}/panel/api/server/amneziawglogs/50
EOF
}

print_summary() {
    local ids=""
    [[ -n "${REALITY_ID:-}" ]] && ids="${REALITY_ID}"
    if [[ -n "${AWG_ID:-}" ]]; then
        [[ -n "$ids" ]] && ids="${ids},${AWG_ID}" || ids="${AWG_ID}"
    fi

    box "ИТОГИ: ЧТО ВПИСАТЬ В .env"
    cat <<EOF
  PANEL_TYPE=xui
  PANEL_URL=${PANEL_URL}
  PANEL_TOKEN=<токен, который уже лежит в .env — не печатаю его в целях безопасности>
  PANEL_INBOUND_IDS=${ids:-<id инбаундов>}
  PUBLIC_BASE_URL=http://<IP_СЕРВЕРА>:8080
EOF

    if [[ -n "${REALITY_ID:-}" ]]; then
        box "VLESS + REALITY"
        cat <<EOF
  inbound id : ${REALITY_ID}
  порт       : ${REALITY_PORT}/tcp
  SNI (dest) : ${SNI}:443
  shortId    : ${REALITY_SHORTID}
  publicKey  : ${REALITY_PUBKEY}
  fingerprint: chrome, spiderX: /

  Клиентов создаёт бот (по одному на подписку) — вручную добавлять не нужно.
  Если понадобится вручную: панель → Inbounds → ${REALITY_REMARK} → «+» у клиента.
EOF
    fi

    if [[ -n "${AWG_ID:-}" ]]; then
        box "AMNEZIAWG"
        cat <<EOF
  inbound id : ${AWG_ID}
  порт       : ${AWG_PORT}/udp
  Обфускация (jc/jmin/jmax/s1-s4/h1-h4/i1) и ключи сервера сгенерированы панелью
  автоматически и хранятся в самом инбаунде. Клиентские .conf-файлы панель
  собирает сама — бот отдаёт их через нашу ссылку-подписку /sub/<token>.
EOF
    fi

    print_port_checks

    box "ЧТО ПРОВЕРИТЬ РУКАМИ"
    cat <<EOF
  1. Панель → Inbounds: оба инбаунда в статусе «enabled», Xray перезапущен
     (панель делает это сама в течение ~5 секунд после создания).
  2. Домен маскировки (проверка TLS 1.3 + h2 вручную):
       openssl s_client -connect ${SNI}:443 -servername ${SNI} -tls1_3 -alpn h2 </dev/null | grep -E 'Protocol|ALPN'
     Ожидаем: Protocol: TLSv1.3 и ALPN protocol: h2
  3. Подключение с телефона: запусти скрипт с флагом --test-client и импортируй
     полученную ссылку vless:// в v2rayNG или Hiddify.
  4. Если провайдер режет TCP/443 — создай второй Reality-инбаунд на резервном
     порту (например 8443) и добавь его id в PANEL_INBOUND_IDS.
EOF
}

# ------------------------------------------------------------------- main -----
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reality-port) REALITY_PORT="${2:?}"; shift 2 ;;
            --awg-port)     AWG_PORT="${2:?}"; shift 2 ;;
            --sni)          SNI="${2:?}"; shift 2 ;;
            --no-reality)   DO_REALITY=0; shift ;;
            --no-awg)       DO_AWG=0; shift ;;
            --panel-url)    PANEL_URL="${2:?}"; shift 2 ;;
            --panel-token)  PANEL_TOKEN="${2:?}"; shift 2 ;;
            --env)          ENV_FILE="${2:?}"; shift 2 ;;
            --token-only)   MODE="token-only"; shift ;;
            --test-client)  TEST_CLIENT=1; shift ;;
            --force-remote) FORCE_REMOTE=1; shift ;;
            --insecure)     CURL_OPTS+=(--insecure); shift ;;
            --dry-run)      DRY_RUN=1; shift ;;
            -h | --help)    usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    box "KOMETA • ПОДГОТОВКА VPN-НОДЫ"
    require_root
    check_deps

    if [[ "$MODE" == "token-only" ]]; then
        token_only
        exit 0
    fi

    load_env
    [[ -n "$PANEL_URL" ]] || die "Не знаю адрес панели. Задай --panel-url или заполни PANEL_URL в .env (${ENV_FILE})."
    [[ -n "$PANEL_TOKEN" ]] || die "Не знаю API-токен. Задай --panel-token или заполни PANEL_TOKEN в .env (${ENV_FILE})."
    PANEL_URL="${PANEL_URL%/}"

    api_check

    if ! is_local_panel && [[ "$FORCE_REMOTE" -eq 0 ]]; then
        warn "Панель ${PANEL_URL} — не эта машина. Создавать инбаунды отсюда можно, но для"
        warn "схемы «мастер-панель + нода» правильный путь другой: на ноде выпускается"
        warn "node-sync токен, а инбаунды раскатываются из мастера."
        die  "Запусти на ноде: sudo bash scripts/install_node.sh --token-only (или добавь --force-remote)."
    fi

    local existing reality_id awg_id
    existing="$(list_inbounds)"

    # ---- VLESS + Reality ----
    if [[ "$DO_REALITY" -eq 1 ]]; then
        reality_id="$(find_inbound_by_remark "$existing" "$REALITY_REMARK")"
        if [[ -n "$reality_id" ]]; then
            ok "VLESS+Reality уже есть (id=${reality_id}) — пропускаю (идемпотентность)."
            REALITY_ID="$reality_id"
        else
            resolve_sni
            create_reality_inbound "$REALITY_REMARK" "$REALITY_PORT"
        fi
    fi

    # ---- AmneziaWG ----
    if [[ "$DO_AWG" -eq 1 ]]; then
        awg_id="$(find_inbound_by_remark "$existing" "$AWG_REMARK")"
        if [[ -n "$awg_id" ]]; then
            ok "AmneziaWG уже есть (id=${awg_id}) — пропускаю (идемпотентность)."
            AWG_ID="$awg_id"
        else
            create_awg_inbound "$AWG_REMARK" "$AWG_PORT" || true
        fi
    fi

    if [[ "$DRY_RUN" -eq 0 ]]; then
        log "Жду, пока панель поднимет порты..."
        if [[ -n "${REALITY_ID:-}" ]]; then
            wait_for_port "$REALITY_PORT" tcp 15 \
                && ok "Порт ${REALITY_PORT}/tcp слушается." \
                || warn "Порт ${REALITY_PORT}/tcp пока не слушается — смотри journalctl -u x-ui -n 50"
        fi
        if [[ -n "${AWG_ID:-}" ]]; then
            wait_for_port "$AWG_PORT" udp 15 \
                && ok "Порт ${AWG_PORT}/udp слушается." \
                || warn "Порт ${AWG_PORT}/udp пока не слушается — AmneziaWG поднимается при первом клиенте, это нормально."
        fi
        if [[ "$TEST_CLIENT" -eq 1 ]]; then
            create_test_client "${REALITY_ID:-}"
        fi
    fi

    print_summary
}

main "$@"
