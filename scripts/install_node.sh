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
#  по remark (Kometa-Reality-443 / Kometa-AWG; старые установки — Kometa-AWG-51820).
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------- настройки ---
REALITY_PORT="${REALITY_PORT:-443}"
# Порт AmneziaWG. 51820 (дефолт WireGuard) — плохой выбор для LTE: полевой замер
# (NTC 22319, 18.02.2026, Дом.ру Сибирь) показал, что при детекте WG-рукопожатия
# оператор блокирует ВСЕ UDP-порты выше 1000 на 10 минут — сутки, а порты ниже
# 1000 продолжают работать. Рабочие кейсы сообщества: 990/udp и <600/udp.
# Компромисс: 443/udp маскируется под QUIC, но попадает в зону блокировки.
# Совместить нельзя — см. docs/LTE-ВАРИАНТЫ-2026-10.md, §3.1 и §8.3.
AWG_PORT="${AWG_PORT:-990}"
# uTLS-фингерпринт Reality. Дефолт Xray — chrome, и в июне 2026 именно chrome
# попал под эвристику «IP → фингерпринт → >3 параллельных TLS». Рабочие
# значения: firefox, edge, android (OkHttp), randomized.
# См. docs/LTE-ВАРИАНТЫ-2026-10.md, §6.1.
REALITY_FP="${REALITY_FP:-firefox}"
REALITY_REMARK="${REALITY_REMARK:-Kometa-Reality-443}"
# Запасные домены маскировки (ротация SNI): список через запятую или пробел.
# Попадают в realitySettings.serverNames вместе с основным --sni, поэтому
# смена маскировки у клиентов не требует пересоздавать инбаунд.
REALITY_SNI_EXTRA="${REALITY_SNI_EXTRA:-}"
# Remark AWG не содержит порта: порт — это параметр инбаунда, а не его имя.
# Иначе смена --awg-port на уже настроенной ноде создала бы второй инбаунд.
AWG_REMARK="${AWG_REMARK:-Kometa-AWG}"
# Имена, под которыми инбаунд мог быть создан раньше (до 07.10.2026) —
# ищем и их, чтобы повторный запуск оставался идемпотентным.
AWG_REMARK_LEGACY="Kometa-AWG-51820"
PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"
SNI="${SNI:-}"
MODE="inbounds"        # inbounds | token-only
DO_REALITY=1
DO_AWG=1
DRY_RUN=0
TEST_CLIENT=0
BRIDGE_CLIENT=1        # клиент для моста: без него мосту нечем выйти на эту ноду
BRIDGE_CLIENT_NAME="bridge@kometa"
PUBLIC_IP_OVERRIDE=""  # --public-ip: что печатать как адрес этой ноды
FORCE_REMOTE=0
CURL_OPTS=()

# Заполняются по ходу работы (нужны для итогового отчёта; заранее пустые,
# чтобы `set -u` не ронял скрипт при повторном запуске, когда инбаунды уже есть).
REALITY_ID=""
AWG_ID=""
REALITY_PUBKEY=""
REALITY_SHORTID=""

XUI_DIR="/usr/local/x-ui"
XUI_BIN="${XUI_DIR}/x-ui"
XUI_INSTALL_RESULT="/etc/x-ui/install-result.env"
NODE_TOKEN_NAME="${NODE_TOKEN_NAME:-kometa-node}"

# Домены-кандидаты для маскировки Reality. Требования: TLS 1.3 + HTTP/2,
# крупный сайт «не по теме VPN», не заблокирован в РФ, не на нашей же IP.
# Порядок = приоритет: скрипт берёт первый, который прошёл проверку.
SNI_CANDIDATES="${SNI_CANDIDATES:-www.microsoft.com dl.google.com www.samsung.com www.lovelive-anime.jp gateway.icloud.com www.bing.com yandex.ru www.ozon.ru}"

# Режим «белых списков»: оператор пропускает только разрешённые домены, поэтому
# маскироваться под зарубежный сайт бесполезно — берём домены из разрешённого
# набора. Список у каждого оператора и региона свой: проверяй вручную.
WHITELIST_SNI_CANDIDATES="${WHITELIST_SNI_CANDIDATES:-yandex.ru www.ozon.ru vk.com gosuslugi.ru www.sberbank.ru}"

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
  --reality-fp FP    uTLS-фингерпринт Reality: firefox|edge|android|randomized
                     (по умолчанию firefox — chrome под эвристикой июня 2026)
  --awg-port N       порт AmneziaWG, UDP (по умолчанию 990 — ниже 1000, чтобы
                     не попадать под блокировку UDP-портов >1000 на LTE)
  --sni DOMAIN       домен маскировки Reality (по умолчанию — автоподбор)
  --sni-extra LIST   запасные домены маскировки через запятую: попадут в
                     serverNames вместе с основным. Нужны для ротации SNI:
                     если домен сожгут, клиенты переключаются без пересоздания
                     инбаунда (например --sni-extra www.bing.com,yandex.ru)
  --whitelist        режим «белых списков»: маскироваться под разрешённый
                     российский домен (yandex.ru, ozon.ru, vk.com, …)
  --no-reality       не создавать VLESS+Reality
  --no-awg           не создавать AmneziaWG
  --no-mss           не настраивать MSS-clamp (по умолчанию настраивается:
                     MTU 1280 → MSS 1240/IPv4 и 1220/IPv6 в mangle/FORWARD)
  --lte-mtu N        MTU для расчёта MSS (по умолчанию 1280; домашним сетям 1420)
  --panel-url URL    адрес панели (по умолчанию PANEL_URL из .env)
  --panel-token T    API-токен панели (по умолчанию PANEL_TOKEN из .env)
  --env FILE         путь к .env (по умолчанию <проект>/.env)
  --token-only       ничего не создавать: выпустить node-sync токен этой ноды
                     и показать, что вписать в мастер-панели (режим «нода+мастер»)
  --test-client      дополнительно создать тестового клиента и напечатать
                     готовую ссылку vless:// для проверки на телефоне
  --no-bridge-client не создавать клиента для моста (по умолчанию создаётся:
                     без него у моста нет --exit-uuid)
  --bridge-client-name NAME  имя клиента моста (по умолчанию bridge@kometa)
  --public-ip IP     что печатать адресом этой ноды в параметрах моста
                     (по умолчанию определяется по маршруту, без внешних сервисов)
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

local_ips() { # все адреса этого сервера (включая публичный), по одному в строке
    { hostname -I 2>/dev/null || true; } | tr ' ' '\n' | grep -v '^$' || true
    local pub
    pub="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
    [[ -n "$pub" ]] && printf '%s\n' "$pub"
    return 0
}

# Панель на этом же сервере? Проверяем localhost, адреса из hostname -I и DNS-имена
# (важно для случая PANEL_URL=https://panel.example.com/... — это тоже «своя» панель).
is_local_panel() {
    local host
    host="$(printf '%s' "$PANEL_URL" | sed -E 's#^https?://##' | cut -d/ -f1 | cut -d: -f1)"
    case "$host" in
        localhost | 127.0.0.1 | ::1) return 0 ;;
    esac
    local ips ip resolved r
    ips="$(local_ips | sort -u)"
    for ip in $ips; do
        [[ -n "$ip" && "$host" == "$ip" ]] && return 0
    done
    resolved="$(getent hosts "$host" 2>/dev/null | awk '{print $1}' || true)"
    for r in $resolved; do
        for ip in $ips; do
            [[ -n "$ip" && "$r" == "$ip" ]] && return 0
        done
    done
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
# MSS-clamping под LTE. На мобильных сетях MTU меньше (туннели оператора,
# GTP-U), и без подрезки MSS крупные сегменты молча дропаются — это классический
# PMTUD-блэкхол: «сервер пингуется, SSH работает, а сайты висят».
# Правило живёт на СЕРВЕРЕ, в таблице mangle цепочки FORWARD, и работает только
# для транзитного трафика VPN (сам сервер не затрагивается).
#   MTU 1280 (RFC 8200, минимум для IPv6, проходит через любую сеть)
#   → MSS 1240 для IPv4 (1280 − 20 IP − 20 TCP)
#   → MSS 1220 для IPv6 (1280 − 40 IP − 20 TCP)
# См. docs/LTE-ВАРИАНТЫ-2026-10.md, §7.7 и .research/lte-clients.md, §5.4.
LTE_MTU="${LTE_MTU:-1280}"
LTE_MSS_V4="${LTE_MSS_V4:-1240}"
LTE_MSS_V6="${LTE_MSS_V6:-1220}"
DO_MSS=1

mss_rule() { # mss_rule <iptables|ip6tables> <MSS> → одна строка правила
    printf '%s -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss %s' "$1" "$2"
}

apply_mss_clamp() {
    local table bin mss rule removed=0
    for table in "iptables ${LTE_MSS_V4}" "ip6tables ${LTE_MSS_V6}"; do
        set -- $table
        bin="$1"; mss="$2"
        have "$bin" || continue
        rule="$(mss_rule "$bin" "$mss")"

        # Сначала убрать прежние правила скрипта (идемпотентность и чистка дублей):
        # при повторном запуске иначе копились бы одинаковые строки.
        while $rule -D >/dev/null 2>&1; do removed=$((removed + 1)); done

        if $rule -I >/dev/null 2>&1; then
            if [[ "$removed" -gt 0 ]]; then
                ok "MSS-clamp ${bin}: переставлен (снято дублей: ${removed}, MSS ${mss})."
            else
                ok "MSS-clamp ${bin}: MSS ${mss} для транзитного TCP (FORWARD)."
            fi
        else
            warn "MSS-clamp ${bin}: не удалось применить (нет прав или другой backend)."
            warn "Проверь вручную на ноде и добавь в автозагрузку:"
            warn "  ${rule} -I"
        fi
    done

    log "Проверка на ноде: iptables -t mangle -S FORWARD | grep TCPMSS"
}

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
    # openssl с -alpn h2 печатает версию в строке «New, TLSv1.3, Cipher is …»,
    # а не в «Protocol  : TLSv1.3» — принимаем оба формата.
    printf '%s' "$out" | grep -Eq 'Protocol *: *TLSv1\.3|New, TLSv1\.3' || return 1
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
    local names extra_sorted
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
    # serverNames: первый — текущий домен маскировки, дальше запасные (--sni-extra).
    # Запасные нужны для ротации: если домен сожгут как признак обхода, клиенты
    # переключаются на другой SNI без пересоздания инбаунда.
    extra_sorted="${REALITY_SNI_EXTRA//,/ }"
    names="$(printf '%s\n' "$SNI" $extra_sorted | awk 'NF && !seen[$0]++' | jq -R . | jq -sc .)"
    stream="$(jq -nc \
        --arg sni "$SNI" --arg priv "$priv" --arg pub "$pub" --arg sid "$sid" \
        --arg fp "$REALITY_FP" --argjson names "$names" \
        '{network:"tcp",security:"reality",externalProxy:[],
          realitySettings:{show:false,xver:0,target:($sni+":443"),serverNames:$names,
            privateKey:$priv,minClientVer:"",maxClientVer:"",maxTimeDiff:0,
            shortIds:[$sid],
            settings:{publicKey:$pub,fingerprint:$fp,serverName:"",spiderX:"/"}}}')"
    # routeOnly: домен из sniffing нужен только для маршрутизации — он не
    # подменяет адрес назначения и не оседает в логах. Это часть обещания
    # «не храним историю посещений» (docs/ЛОГИ-И-ПРИВАТНОСТЬ.md).
    sniffing='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":true}'

    payload="$(jq -nc --arg remark "$remark" --argjson port "$port" \
        --arg settings "$settings" --arg stream "$stream" --arg sniffing "$sniffing" \
        '{up:0,down:0,total:0,remark:$remark,enable:true,expiryTime:0,listen:"",
          port:$port,protocol:"vless",settings:$settings,streamSettings:$stream,sniffing:$sniffing}')"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "DRY-RUN: VLESS+Reality НЕ создаётся. Параметры:"
        printf '    порт=%s  SNI=%s  shortId=%s\n    publicKey=%s\n' "$port" "$SNI" "$sid" "$pub"
        return 0
    fi

    log "Создаю инбаунд VLESS+Reality (порт ${port}, SNI ${SNI}, fp ${REALITY_FP})..."
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

detect_public_ip() { # внешний адрес ноды без внешних сервисов; пусто, если не вышел
    local a=""
    if have ip; then
        a="$(ip -4 route get 1.1.1.1 2>/dev/null \
             | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
    fi
    [[ -n "$a" ]] || a="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    printf '%s' "$a"
}

# Клиент моста на Reality-инбаунде. Без него мост собрать нельзя: ему нужен
# --exit-uuid, а панель до этого создавала клиентов только боту (settings.clients
# пустой). Повторный запуск не плодит второго клиента — берём существующий UUID.
create_bridge_client() { # create_bridge_client <inbound_id>
    local inbound_id="$1" uuid payload resp addr existing
    [[ -n "$inbound_id" ]] || { warn "Нет id Reality-инбаунда — клиента моста не создаю."; return 0; }

    resp="$(api_get "/panel/api/inbounds/get/${inbound_id}")"
    existing="$(printf '%s' "$resp" | jq -r --arg e "$BRIDGE_CLIENT_NAME" \
        '[.obj.settings | fromjson | .clients[]? | select(.email == $e)][0].id // empty' 2>/dev/null || true)"
    if [[ -n "$existing" ]]; then
        uuid="$existing"
        note "Клиент моста ${BRIDGE_CLIENT_NAME} уже есть — беру его UUID (новый не создаю)."
    else
        uuid="$(rand_uuid)"
        payload="$(jq -nc --arg id "$uuid" --arg email "$BRIDGE_CLIENT_NAME" --argjson iid "$inbound_id" \
            '{client:{id:$id,email:$email,flow:"xtls-rprx-vision",limitIp:0,totalGB:0,
                      expiryTime:0,enable:true,subId:"",tgId:0},inboundIds:[$iid]}')"
        log "Создаю клиента моста ${BRIDGE_CLIENT_NAME}..."
        resp="$(api_post /panel/api/clients/add "$payload")"
        if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
            warn "Клиента моста создать не удалось: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')"
            warn "Создай клиента в панели вручную и возьми UUID оттуда: без --exit-uuid мост не собрать."
            return 0
        fi
    fi

    addr="${PUBLIC_IP_OVERRIDE:-$(detect_public_ip)}"
    [[ -n "$addr" ]] || addr="<IP_СЕРВЕРА>"
    box "ПАРАМЕТРЫ ДЛЯ МОСТА (ops/30-deploy-bridge.sh)"
    cat <<EOF
  --exit-address ${addr} \\
  --exit-port ${REALITY_PORT} \\
  --exit-uuid ${uuid} \\
  --exit-pubkey ${REALITY_PUBKEY:-<publicKey из панели>} \\
  --exit-sni ${SNI:-<домен маскировки>} \\
  --exit-shortid ${REALITY_SHORTID:-<shortId из панели>}

  Порт ${REALITY_PORT}/tcp на этой ноде должен пускать только мост:
    ufw allow from <IP_МОСТА> to any port ${REALITY_PORT} proto tcp
    ufw deny ${REALITY_PORT}/tcp

  Клиента ${BRIDGE_CLIENT_NAME} не удаляй — это учётная запись моста.
  Отключить его создание можно флагом --no-bridge-client.
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

  # Порт Reality должен пускать ТОЛЬКО мост, иначе нода открыта всему интернету:
    ufw allow from <IP_МОСТА> to any port ${REALITY_PORT} proto tcp
    ufw deny ${REALITY_PORT}/tcp
    ufw allow ${AWG_PORT}/udp
    ufw status numbered

  # (без ограничения по IP было бы просто `ufw allow ${REALITY_PORT}/tcp` —
  #  так делать не надо: любой, кто узнает UUID, сможет ходить через ноду)

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
  SNI (dest) : ${SNI:-<не определён>}:443
  shortId    : ${REALITY_SHORTID:-<см. панель>}
  publicKey  : ${REALITY_PUBKEY:-<см. панель: Inbounds → инбаунд → клиент>}
  fingerprint: ${REALITY_FP}, spiderX: /

  Клиентов бота создаёт бот (по одному на подписку) — вручную добавлять не нужно.
  Отдельно создаётся клиент моста ${BRIDGE_CLIENT_NAME}: он нужен, чтобы мост мог
  выйти на эту ноду (--exit-uuid). Не удаляй его.
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
       openssl s_client -connect ${SNI:-<домен>}:443 -servername ${SNI:-<домен>} -tls1_3 -alpn h2 </dev/null | grep -E 'Protocol|ALPN'
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
            --reality-fp)   REALITY_FP="${2:?}"; shift 2 ;;
            --awg-port)     AWG_PORT="${2:?}"; shift 2 ;;
            --sni)          SNI="${2:?}"; shift 2 ;;
            --sni-extra)    REALITY_SNI_EXTRA="${2:?}"; shift 2 ;;
            --whitelist)    SNI_CANDIDATES="$WHITELIST_SNI_CANDIDATES"; shift ;;
            --no-reality)   DO_REALITY=0; shift ;;
            --no-awg)       DO_AWG=0; shift ;;
            --no-mss)       DO_MSS=0; shift ;;
            --lte-mtu)      LTE_MTU="${2:?}"; LTE_MSS_V4=$((LTE_MTU - 40)); LTE_MSS_V6=$((LTE_MTU - 60)); shift 2 ;;
            --panel-url)    PANEL_URL="${2:?}"; shift 2 ;;
            --panel-token)  PANEL_TOKEN="${2:?}"; shift 2 ;;
            --env)          ENV_FILE="${2:?}"; shift 2 ;;
            --token-only)   MODE="token-only"; shift ;;
            --test-client)  TEST_CLIENT=1; shift ;;
            --no-bridge-client) BRIDGE_CLIENT=0; shift ;;
            --bridge-client-name) BRIDGE_CLIENT_NAME="${2:?}"; shift 2 ;;
            --public-ip)    PUBLIC_IP_OVERRIDE="${2:?}"; shift 2 ;;
            --force-remote) FORCE_REMOTE=1; shift ;;
            --insecure)     CURL_OPTS+=(--insecure); shift ;;
            --dry-run)      DRY_RUN=1; shift ;;
            -h | --help)    usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    # Фингерпринт: chrome/safari/ios попадают под эвристику ТСПУ (июнь 2026).
    # Не запрещаем — иногда нужен для отладки, — но предупреждаем громко.
    case "$REALITY_FP" in
        chrome | safari | ios | ios14 | ios15)
            warn "Фингерпринт '${REALITY_FP}' входит в чёрный список эвристики июня 2026."
            warn "Рекомендуется firefox, edge, android или randomized." ;;
    esac

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
            # Достаём параметры существующего инбаунда, чтобы напечатать их снова
            # (streamSettings в API — это строка с JSON внутри, отсюда fromjson?).
            REALITY_PUBKEY="$(printf '%s' "$existing" | jq -r --arg r "$REALITY_REMARK" \
                '[.obj[]? | select(.remark == $r) | (.streamSettings | fromjson? | .realitySettings.settings.publicKey) // empty] | first // empty')"
            REALITY_SHORTID="$(printf '%s' "$existing" | jq -r --arg r "$REALITY_REMARK" \
                '[.obj[]? | select(.remark == $r) | (.streamSettings | fromjson? | .realitySettings.shortIds[0]) // empty] | first // empty')"
            SNI="$(printf '%s' "$existing" | jq -r --arg r "$REALITY_REMARK" \
                '[.obj[]? | select(.remark == $r) | (.streamSettings | fromjson? | .realitySettings.serverNames[0]) // empty] | first // empty')"
        else
            resolve_sni
            create_reality_inbound "$REALITY_REMARK" "$REALITY_PORT"
        fi
    fi

    # ---- AmneziaWG ----
    if [[ "$DO_AWG" -eq 1 ]]; then
        awg_id="$(find_inbound_by_remark "$existing" "$AWG_REMARK")"
        if [[ -z "$awg_id" && -n "${AWG_REMARK_LEGACY:-}" ]]; then
            # Нода, настроенная до 07.10.2026: инбаунд назывался Kometa-AWG-51820.
            awg_id="$(find_inbound_by_remark "$existing" "$AWG_REMARK_LEGACY")"
            if [[ -n "$awg_id" ]]; then
                warn "Найден старый инбаунд «${AWG_REMARK_LEGACY}» (id=${awg_id})."
                warn "Он слушает прежний порт. Для LTE перенеси его на порт ${AWG_PORT}/udp:"
                warn "  панель → инбаунды → «${AWG_REMARK_LEGACY}» → порт ${AWG_PORT}, затем перезапуск xray."
                warn "Либо запусти скрипт с AWG_REMARK=${AWG_REMARK_LEGACY}, чтобы не создавать дубль."
            fi
        fi
        if [[ -n "$awg_id" ]]; then
            ok "AmneziaWG уже есть (id=${awg_id}) — пропускаю (идемпотентность)."
            AWG_ID="$awg_id"
        else
            create_awg_inbound "$AWG_REMARK" "$AWG_PORT" || true
        fi
    fi

    if [[ "$DRY_RUN" -eq 0 ]]; then
        if [[ "$DO_MSS" -eq 1 ]]; then
            log "Настраиваю MSS-clamp под LTE (MTU ${LTE_MTU})..."
            apply_mss_clamp
        else
            warn "MSS-clamp пропущен (--no-mss). На LTE это частая причина «сайты висят»."
        fi

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
        if [[ "$BRIDGE_CLIENT" -eq 1 ]]; then
            create_bridge_client "${REALITY_ID:-}"
        fi
        if [[ "$TEST_CLIENT" -eq 1 ]]; then
            create_test_client "${REALITY_ID:-}"
        fi
    fi

    print_summary
}

main "$@"
