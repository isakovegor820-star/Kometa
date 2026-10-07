#!/usr/bin/env bash
# =============================================================================
#  Kometa — MTProto-инбаунд (Telegram) в 3x-ui: «минимальный интернет»
# =============================================================================
#  Зачем: в режиме «белых списков»/БС у абонента часто не работает ничего,
#  кроме мессенджеров. MTProto-прокси даёт Telegram напрямую, без VPN-туннеля,
#  и не требует ни домена, ни сертификата, ни UDP: клиент видит обычный TLS
#  (FakeTLS) к случайному домену. Это АВАРИЙНЫЙ канал, а не основной:
#  покрывает только Telegram, а с мая 2026 MTProto под волной блокировок
#  (docs/LTE-ВАРИАНТЫ-2026-10.md §2.10, .research/lte-transports.md §11.2).
#
#  Проверено по исходникам 3x-ui (main, октябрь 2026):
#    * протокол инбаунда — "mtproto", обслуживает sidecar mtg-multi (не Xray),
#      поэтому streamSettings у него нет — "{}"
#      (frontend/src/schemas/protocols/inbound/mtproto.ts);
#    * settings: {fakeTlsDomain, clients:[{secret,email,enable,tgId,subId,...}], ...},
#      fakeTlsDomain по умолчанию www.cloudflare.com — это домен, под который
#      маскируется FakeTLS;
#    * клиентский secret — ee-префиксный FakeTLS-секрет, хвост которого
#      панель пересобирает из fakeTlsDomain инбаунда. Если secret не передан,
#      панель генерирует его сама при clients/add — поэтому скрипт НЕ выдумывает
#      секрет за панель (см. ветку «секрет не задан»);
#    * минимальная версия панели — 3x-ui 3.5.0 (в 3.9.0 MTProto научились
#      раскладывать на ноды; ноду нужно обновлять ПЕРВОЙ).
#
#  Что делает скрипт:
#    1. печатает ТОЧНЫЙ JSON-payload и понятную подсказку про секрет; по
#       умолчанию в панель не ходит вообще (dry-run, сетевых запросов нет);
#    2. ЯВНО проверяет версию панели: < 3.5.0 — понятная подсказка; с --apply
#       это ошибка (выход 1), без --apply — предупреждение;
#    3. с --apply: ищет инбаунд по remark (идемпотентность), создаёт инбаунд и
#       клиента, печатает ссылку tg://proxy (и https://t.me/proxy);
#    4. в конце — блок «Как проверить» (что смотреть руками на ноде).
#
#  Идемпотентность: повторный запуск не создаёт дубликаты — инбаунд ищется по
#  remark (Kometa-MTProto) через GET /panel/api/inbounds/list. Remark НЕ содержит
#  порт: смена --port не должна плодить второй инбаунд.
#
#  Примеры:
#     # 1. Посмотреть payload и проверку версии (в панель не ходит):
#     PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
#       ./scripts/add_mtproto_inbound.sh --port 443
#
#     # 2. Заодно прочитать версию панели (read-only GET /server/status):
#     ... ./scripts/add_mtproto_inbound.sh --port 443 --check-version
#
#     # 3. Создать инбаунд и клиента, маскируясь под свой домен:
#     ... ./scripts/add_mtproto_inbound.sh --port 443 --domain www.cloudflare.com --apply
#
#     # 4. Со своим секретом (32 hex-символа):
#     ... ./scripts/add_mtproto_inbound.sh --port 443 \
#           --secret "$(openssl rand -hex 16)" --apply
#
#  Зависимости: bash, jq (всегда), curl (только с --apply/--check-version),
#  openssl (только чтобы сгенерировать UUID локально).
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------- параметры -------
PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"
FAKE_TLS_DOMAIN="${FAKE_TLS_DOMAIN:-${DOMAIN:-www.cloudflare.com}}"
PORT="${PORT:-443}"
REMARK="${REMARK:-}"
SECRET="${SECRET:-}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
CLIENT_EMAIL="${CLIENT_EMAIL:-}"
CREATE_CLIENT="${CREATE_CLIENT:-1}"

# MTProto появился в 3x-ui 3.5.0; ниже — панель просто не знает такого протокола.
# Для нод ограничение строже: деплой на нод старше релиза протокола отклоняется.
MIN_PANEL_VERSION="3.5.0"

APPLY=0
CHECK_VERSION=0
REMARK_EXPLICIT=0
CURL_OPTS=()
XUI_BIN="/usr/local/x-ui/x-ui"   # локальный бинарь панели (если скрипт на ноде)

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

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
MTProto-инбаунд (Telegram, FakeTLS) в 3x-ui — «минимальный интернет» в БС.

По умолчанию — DRY-RUN: скрипт печатает точный JSON-payload, подсказку про
секрет и требования к версии панели, но в панель НЕ ходит (ни одного сетевого
запроса; исключение — явный --check-version: один read-only GET).
Создание — только с явным --apply.

Использование:
  PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
    ./scripts/add_mtproto_inbound.sh [флаги]

Обязательно:
  PANEL_URL          адрес панели (env или --panel-url)
  PANEL_TOKEN        API-токен панели, скоуп admin (env или --panel-token)

Флаги:
  --port N           TCP-порт MTProto (по умолчанию 443 — лучшая маскировка;
                     если 443 занят nginx/Reality — дай отдельный порт)
  --domain DOMAIN    домен, под который маскируется FakeTLS (fakeTlsDomain;
                     по умолчанию www.cloudflare.com). Домен и сертификат
                     на сервере НЕ нужны — это только «лицо» рукопожатия
  --sni DOMAIN       алиас --domain (MTProto-аналог SNI)
  --remark NAME      имя инбаунда (по умолчанию Kometa-MTProto)
  --secret HEX       секрет клиента: 32 hex-символа (openssl rand -hex 16)
                     либо полный ee-секрет FakeTLS (панель пересоберёт хвост
                     из fakeTlsDomain). Если НЕ задан — скрипт секрет сам
                     не придумывает: панель сгенерирует его при clients/add
  --host HOST        публичный адрес ноды для ссылки tg://proxy (по умолчанию —
                     хост из PANEL_URL; сертификат и домен тут не нужны)
  --client-email E   email клиента в панели (по умолчанию mtproto-<host8>)
  --no-client        не создавать клиента
  --panel-url URL    адрес панели (альтернатива PANEL_URL)
  --panel-token T    токен панели (альтернатива PANEL_TOKEN)
  --insecure         не проверять TLS-сертификат панели (самоподписанный)
  --check-version    прочитать версию панели (GET /panel/api/server/status)
                     даже в dry-run — иначе без --apply версия не проверяется
  --apply            реально создать инбаунд (без него — только показать)
  --dry-run          явный dry-run (то же поведение, что и по умолчанию)
  -h, --help         эта справка

Переменные окружения: PANEL_URL, PANEL_TOKEN, FAKE_TLS_DOMAIN (или DOMAIN),
PORT, REMARK, SECRET, PUBLIC_HOST, CLIENT_EMAIL.
EOF
}

# --------------------------------------------------------------- утилиты ------
rand_uuid() { cat /proc/sys/kernel/random/uuid 2>/dev/null || openssl rand -hex 16; }

require_panel() {
    [[ -n "$PANEL_URL" ]] || die "Не задан PANEL_URL. Укажи --panel-url или переменную окружения PANEL_URL (например PANEL_URL=https://127.0.0.1:2053)."
    [[ -n "$PANEL_TOKEN" ]] || die "Не задан PANEL_TOKEN. Укажи --panel-token или переменную окружения PANEL_TOKEN (панель → Настройки → Безопасность → API-токен)."
    PANEL_URL="${PANEL_URL%/}"
}

api_get() {
    curl -sS --max-time 25 "${CURL_OPTS[@]+"${CURL_OPTS[@]}"}" \
        -H "Authorization: Bearer ${PANEL_TOKEN}" \
        "${PANEL_URL}$1"
}

api_post() {
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
        die "Панель ответила отказом: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"'). Проверь PANEL_TOKEN (скоуп admin)."
    fi
    ok "API панели доступен, токен принят."
}

# ------------------------------------------------------------ версия ----------
# Тот же способ, что в install_node.sh: панель отдаёт panelVersion в
# GET /panel/api/server/status; если панель недоступна — пробуем локальный
# бинарь `x-ui -v` (на ноде он есть, в dry-run сети не касается).
panel_version_api() {
    api_get /panel/api/server/status | jq -r '.obj.panelVersion // empty' || true
}

panel_version_local() {
    local v=""
    if [[ -x "$XUI_BIN" ]]; then
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

print_version_hint() {
    cat <<EOF
MTProto есть в 3x-ui начиная с ${MIN_PANEL_VERSION} (движок mtg-multi, FakeTLS).
Что делать:
  1. обнови панель:   sudo bash scripts/install_panel.sh
     (или в панели: «Настройки» → «Обновить панель» / x-ui → update);
  2. проверь версию:  curl -s -H "Authorization: Bearer \$PANEL_TOKEN" \\
                        ${PANEL_URL}/panel/api/server/status | jq -r .obj.panelVersion
  3. в схеме «мастер-панель + нода» ноды обновляются ПЕРВЫМИ: деплой инбаунда
     на нод старше релиза протокола панель отклоняет (MTProto — ${MIN_PANEL_VERSION},
     AmneziaWG — 3.7.0, TUIC — 3.8.0).
EOF
}

check_version_gate() { # <режим: apply|dry-run> → 0 ок, 1 — панель старее, 2 — версия неизвестна
    local mode="$1" version=""
    if [[ "$APPLY" -eq 1 || "$CHECK_VERSION" -eq 1 ]]; then
        version="$(panel_version_api)"
    else
        version="$(panel_version_local)"
    fi

    if [[ -z "$version" ]]; then
        if [[ "$APPLY" -eq 1 || "$CHECK_VERSION" -eq 1 ]]; then
            warn "Версию панели определить не удалось: /panel/api/server/status не ответил panelVersion."
        else
            warn "DRY-RUN: версию панели не проверяю (в панель не хожу, локального x-ui тоже нет)."
            warn "MTProto требует 3x-ui ≥ ${MIN_PANEL_VERSION}; с --apply скрипт проверит версию сам."
            warn "Проверить сейчас, без создания: --check-version (один read-only GET /server/status)."
            return 2
        fi
        return 2
    fi

    log "Версия панели: ${version} (нужно ≥ ${MIN_PANEL_VERSION})"
    if version_ge "$version" "$MIN_PANEL_VERSION"; then
        ok "Версия панели подходит для MTProto."
        return 0
    fi
    if [[ "$mode" == "apply" ]]; then
        err "Панель ${version} не умеет MTProto: нужно 3x-ui ≥ ${MIN_PANEL_VERSION}."
        print_version_hint
        return 1
    fi
    warn "Панель ${version} не умеет MTProto (нужно ≥ ${MIN_PANEL_VERSION}) — это предупреждение,"
    warn "потому что запуск без --apply. С --apply скрипт остановится с ошибкой."
    print_version_hint
    return 1
}

# ------------------------------------------------------------ payload ---------
# settings MTProto (см. frontend/src/schemas/protocols/inbound/mtproto.ts):
#   * fakeTlsDomain — домен, под который маскируется FakeTLS (клиент видит
#     обычный TLS к нему), по умолчанию www.cloudflare.com;
#   * clients — пустой список: клиентов создаём отдельным вызовом clients/add,
#     чтобы панель сама сгенерировала FakeTLS-секрет (ee-префикс + хвост домена);
#   * streamSettings у MTProto нет вообще: инбаунд обслуживает sidecar mtg-multi,
#     а не Xray.
build_payload() {
    local settings
    settings="$(jq -nc --arg dom "$FAKE_TLS_DOMAIN" '{fakeTlsDomain:$dom,clients:[]}')"
    PAYLOAD="$(jq -nc \
        --arg remark "$REMARK" --argjson port "$PORT" --arg settings "$settings" \
        '{up:0,down:0,total:0,remark:$remark,enable:true,expiryTime:0,listen:"",
          port:$port,protocol:"mtproto",settings:$settings,
          streamSettings:"{}",sniffing:"{}"}')"
}

print_secret_hint() {
    box "СЕКРЕТ КЛИЕНТА"
    if [[ -n "$SECRET" ]]; then
        printf '  задан через --secret: %s\n\n' "$SECRET"
        cat <<EOF
  Это значение уйдёт в settings клиента как есть. Панель хранит ee-секрет
  FakeTLS и пересобирает его хвост из fakeTlsDomain инбаунда (${FAKE_TLS_DOMAIN}),
  поэтому 32 hex-символа из \`openssl rand -hex 16\` — нормальный ввод.
EOF
    else
        cat <<EOF
  Секрет НЕ задан (--secret) — и я его сам не выдумываю, чтобы не разойтись
  с панелью. Два штатных пути:

    a) панель сгенерирует секрет сама при создании клиента
       (POST /panel/api/clients/add): FakeTLS-секрет выводится из fakeTlsDomain
       инбаунда — у нас это ${FAKE_TLS_DOMAIN}; если домен не задан вовсе,
       панель берёт www.cloudflare.com.
       Так делает и наш запуск с --apply: секрет читается обратно из ответа.

    b) свой секрет, если нужен свой:
         openssl rand -hex 16        # → 32 hex-символа
         ./scripts/add_mtproto_inbound.sh --secret <эти 32 символа> --apply
       Раздать его абоненту можно вручную: tg://proxy?server=<host>&port=<port>&secret=<secret>
EOF
    fi
}

# ------------------------------------------------------- конфиги клиенту ------
print_client_link() { # <secret>
    local secret="$1" link tg_link
    if [[ -n "$secret" ]]; then
        link="tg://proxy?server=${PUBLIC_HOST}&port=${PORT}&secret=${secret}"
        tg_link="https://t.me/proxy?server=${PUBLIC_HOST}&port=${PORT}&secret=${secret}"
    else
        link="tg://proxy?server=${PUBLIC_HOST}&port=${PORT}&secret=<СЕКРЕТ_ИЗ_ПАНЕЛИ>"
        tg_link="https://t.me/proxy?server=${PUBLIC_HOST}&port=${PORT}&secret=<СЕКРЕТ_ИЗ_ПАНЕЛИ>"
    fi
    box "ССЫЛКА ДЛЯ TELEGRAM"
    printf '  %s\n  %s\n\n' "$link" "$tg_link"
    cat <<EOF
  Куда вставлять:
    * Telegram Desktop: Настройки → Продвинутые настройки → Тип соединения →
      Прокси → «Добавить прокси» → вставить ссылку;
    * Android: Настройки → Данные и память → Прокси → «Добавить прокси»;
    * iOS: тот же путь, но с 01.04.2026 FakeTLS-детект по TLS-фингерпринту
      на iOS не восстановлен — проверяй отдельно (§2.10).

  В подписке абонента MTProto попадает ТОЛЬКО в raw links (ни JSON-, ни
  Clash-профиль его не содержат) — это ограничение самого 3x-ui.
EOF
}

# ------------------------------------------------------------ применение ------
find_inbound_by_remark() { # <json-список> <remark> → id или пусто
    printf '%s' "$1" | jq -r --arg r "$2" '[.obj[]? | select(.remark == $r) | .id] | first // empty'
}

find_inbound_by_prefix() { # <json-список> <префикс> → "id<TAB>remark<TAB>port"
    printf '%s' "$1" | jq -r --arg p "$2" \
        '[.obj[]? | select((.remark // "") | startswith($p)) | "\(.id)\t\(.remark)\t\(.port)"] | first // empty'
}

inbound_field() { # <json-список> <remark> <jq-путь>
    printf '%s' "$1" | jq -r --arg r "$2" --arg q "$3" \
        '[.obj[]? | select(.remark == $r) | getpath($q | split("."))] | first // empty'
}

port_busy_hint() { # подсказка о занятости порта на ЭТОЙ машине (не блокирует)
    local port="$1" who=""
    if have ss; then
        who="$(ss -tlnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" || true)"
    elif have lsof; then
        who="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | tail -n +2 || true)"
    else
        return 0
    fi
    if [[ -n "$who" ]]; then
        warn "Порт ${port}/tcp уже слушается на этой машине:"
        printf '%s\n' "$who" | sed 's/^/    /' >&2
        warn "MTProto на занятый порт не встанет: возьми другой порт (--port) или"
        warn "разведи службы SNI-роутингом (nginx stream + ssl_preread)."
    else
        ok "Порт ${port}/tcp на этой машине свободен."
    fi
}

apply_inbound() {
    local existing id prefix_hit existing_port resp client_payload new_id
    local secret_out

    existing="$(api_get /panel/api/inbounds/list)"
    [[ "$(printf '%s' "$existing" | jq -r '.success // false')" == "true" ]] \
        || die "Не удалось получить список инбаундов: $(printf '%s' "$existing" | jq -r '.msg // "нет сообщения"')"

    id="$(find_inbound_by_remark "$existing" "$REMARK")"
    if [[ -n "$id" ]]; then
        existing_port="$(inbound_field "$existing" "$REMARK" "port")"
        ok "Инбаунд «${REMARK}» уже есть (id=${id}, порт ${existing_port}) — не дублирую (идемпотентность)."
        if [[ -n "$existing_port" && "$existing_port" != "$PORT" ]]; then
            warn "Он слушает порт ${existing_port}, а запрошен ${PORT}: порт я НЕ меняю."
            warn "Смена порта — вручную: панель → инбаунды → ${REMARK} → порт."
        fi
        INBOUND_ID="$id"
        EXISTING_JSON="$existing"
        MATCHED_REMARK="$REMARK"
        return 0
    fi

    prefix_hit="$(find_inbound_by_prefix "$existing" "Kometa-MTProto")"
    if [[ -n "$prefix_hit" && "$REMARK_EXPLICIT" -eq 0 ]]; then
        ok "Найден инбаунд семейства Kometa-MTProto (id/remark/port: $(printf '%s' "$prefix_hit" | tr '\t' ' ')) — не дублирую."
        log "Нужен именно второй профиль? Запусти с явным --remark <другое имя>."
        INBOUND_ID="$(printf '%s' "$prefix_hit" | cut -f1)"
        EXISTING_JSON="$existing"
        MATCHED_REMARK="$(printf '%s' "$prefix_hit" | cut -f2)"
        return 0
    fi

    log "Создаю инбаунд «${REMARK}» (MTProto, порт ${PORT}, FakeTLS=${FAKE_TLS_DOMAIN})..."
    resp="$(api_post /panel/api/inbounds/add "$PAYLOAD")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        err "Панель не создала инбаунд: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')."
        print_version_hint
        die "Частая причина — старая панель без MTProto."
    fi
    new_id="$(printf '%s' "$resp" | jq -r '.obj.id // empty')"
    ok "Инбаунд создан (id=${new_id:-?})."
    INBOUND_ID="${new_id:-0}"
    EXISTING_JSON=""
    MATCHED_REMARK="$REMARK"

    if [[ "$CREATE_CLIENT" -ne 1 ]]; then
        log "Клиента не создаю (--no-client)."
        return 0
    fi

    # secret не отправляем вовсе, если он не задан: панель сгенерирует его сама
    # (docs: «per-protocol secrets are generated server-side when omitted»).
    if [[ -n "$SECRET" ]]; then
        client_payload="$(jq -nc --arg email "$CLIENT_EMAIL" --arg secret "$SECRET" \
            --argjson iid "${INBOUND_ID:-0}" \
            '{client:{email:$email,secret:$secret,enable:true,limitIp:0,totalGB:0,
                      expiryTime:0,subId:"",tgId:0},
              inboundIds:[$iid]}')"
    else
        client_payload="$(jq -nc --arg email "$CLIENT_EMAIL" --argjson iid "${INBOUND_ID:-0}" \
            '{client:{email:$email,enable:true,limitIp:0,totalGB:0,
                      expiryTime:0,subId:"",tgId:0},
              inboundIds:[$iid]}')"
    fi

    resp="$(api_post /panel/api/clients/add "$client_payload")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        warn "Клиента создать не удалось: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')."
        warn "Создай вручную в панели — она сгенерирует FakeTLS-секрет сама."
        return 0
    fi
    ok "Клиент ${CLIENT_EMAIL} создан."

    # Секрет читаем обратно: add может вернуть его не во всех сборках.
    secret_out="$(printf '%s' "$resp" | jq -r '.obj.secret // empty')"
    if [[ -z "$secret_out" ]]; then
        secret_out="$(api_get "/panel/api/clients/get/${CLIENT_EMAIL}" | jq -r '.obj.secret // empty' || true)"
    fi
    if [[ -n "$secret_out" ]]; then
        SECRET_OUT="$secret_out"
        ok "FakeTLS-секрет клиента получен из панели."
    else
        warn "Секрет в ответе API не пришёл — скопируй его из панели:"
        warn "  инбаунды → ${REMARK} → клиент ${CLIENT_EMAIL} → поле secret / QR."
    fi
}

# ---------------------------------------------------------------- main --------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port)         PORT="${2:?}"; shift 2 ;;
            --domain)       FAKE_TLS_DOMAIN="${2:?}"; shift 2 ;;
            --sni)          FAKE_TLS_DOMAIN="${2:?}"; shift 2 ;;
            --remark)       REMARK="${2:?}"; REMARK_EXPLICIT=1; shift 2 ;;
            --secret)       SECRET="${2:?}"; shift 2 ;;
            --host)         PUBLIC_HOST="${2:?}"; shift 2 ;;
            --client-email) CLIENT_EMAIL="${2:?}"; shift 2 ;;
            --no-client)    CREATE_CLIENT=0; shift ;;
            --panel-url)    PANEL_URL="${2:?}"; shift 2 ;;
            --panel-token)  PANEL_TOKEN="${2:?}"; shift 2 ;;
            --insecure)     CURL_OPTS+=(--insecure); shift ;;
            --check-version) CHECK_VERSION=1; shift ;;
            --apply)        APPLY=1; shift ;;
            --dry-run)      APPLY=0; shift ;;
            -h | --help)    usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    have jq || die "Нужен jq (apt install jq / brew install jq)."
    require_panel

    [[ "$PORT" =~ ^[0-9]+$ ]] || die "--port должен быть числом, получено: ${PORT}"
    [[ "$PORT" -ge 1 && "$PORT" -le 65535 ]] || die "--port вне диапазона 1..65535: ${PORT}"
    [[ -n "$FAKE_TLS_DOMAIN" ]] || die "--domain (fakeTlsDomain) не может быть пустым."

    [[ -n "$REMARK" ]] || REMARK="Kometa-MTProto"
    # Публичный адрес для ссылки: --host, иначе хост из PANEL_URL.
    if [[ -z "$PUBLIC_HOST" ]]; then
        PUBLIC_HOST="$(printf '%s' "$PANEL_URL" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#^\[?([^]/:@]+).*#\1#')"
    fi
    [[ -n "$PUBLIC_HOST" ]] || PUBLIC_HOST="<IP_СЕРВЕРА>"
    CLIENT_EMAIL="${CLIENT_EMAIL:-mtproto-$(printf '%s' "$PUBLIC_HOST" | tr -c 'a-zA-Z0-9' '-' | cut -c1-12)}"

    build_payload
    INBOUND_ID=""
    EXISTING_JSON=""
    MATCHED_REMARK=""
    SECRET_OUT=""

    box "KOMETA • MTPROTO-ИНБАУНД (Telegram, FakeTLS)"
    printf '  панель        : %s\n' "$PANEL_URL"
    printf '  порт          : %s/tcp\n' "$PORT"
    printf '  fakeTlsDomain : %s\n' "$FAKE_TLS_DOMAIN"
    printf '  remark        : %s\n' "$REMARK"
    printf '  адрес для ссылки: %s\n' "$PUBLIC_HOST"
    printf '  режим         : %s\n' "$( [[ "$APPLY" -eq 1 ]] && printf 'APPLY (создаю)' || printf 'DRY-RUN (ничего не отправляю)' )"
    printf '  клиент        : %s\n' "$( [[ "$CREATE_CLIENT" -eq 1 ]] && printf '%s' "$CLIENT_EMAIL" || printf 'не создавать (--no-client)' )"

    # --- явная проверка версии панели -------------------------------------
    version_rc=0
    check_version_gate "$( [[ "$APPLY" -eq 1 ]] && printf 'apply' || printf 'dry-run' )" || version_rc=$?
    if [[ "$version_rc" -eq 1 && "$APPLY" -eq 1 ]]; then
        die "MTProto на панели ${MIN_PANEL_VERSION}+ — обнови панель и повтори."
    fi

    print_secret_hint
    port_busy_hint "$PORT"

    warn "MTProto — только Telegram и только как аварийный канал: с мая 2026 он под"
    warn "волной блокировок, а на iOS после 01.04.2026 FakeTLS-детект не восстановлен."

    if [[ "$APPLY" -eq 1 ]]; then
        have curl || die "Нужен curl для --apply."
        box "PAYLOAD, КОТОРЫЙ УЙДЁТ В ПАНЕЛЬ (POST /panel/api/inbounds/add)"
        printf '%s\n' "$PAYLOAD" | jq .
        api_check
        apply_inbound
        if [[ -n "${EXISTING_JSON:-}" ]]; then
            SECRET_OUT="$(inbound_field "$EXISTING_JSON" "${MATCHED_REMARK:-$REMARK}" "settings")"
            SECRET_OUT="$(printf '%s' "$SECRET_OUT" \
                | jq -r 'if type == "string" then (fromjson? // {}) else . end | .clients[0].secret // empty' 2>/dev/null || true)"
        fi
    else
        box "DRY-RUN: PAYLOAD ДЛЯ POST /panel/api/inbounds/add"
        printf '%s\n' "$PAYLOAD" | jq .
        echo
        log "Сетевых запросов нет: ни GET /panel/api/inbounds/list, ни POST /panel/api/inbounds/add."
        log "Ничего не создано. Для реального создания добавь --apply."
        if [[ -n "$SECRET" ]]; then
            SECRET_OUT="$SECRET"
        fi
    fi

    print_client_link "${SECRET_OUT:-}"

    box "КАК ПРОВЕРИТЬ (руками на ноде)"
    cat <<EOF
  1. Порт слушается (sidecar mtg-multi поднимает сама панель, ~5 с):
       ss -tlnp | grep ':${PORT}'
     Если порт занят nginx/Reality — MTProto на него не встанет: дай отдельный
     порт (--port 8443) или разведи SNI-роутингом (nginx stream + ssl_preread).
  2. Firewall открыт:
       ufw allow ${PORT}/tcp && ufw status numbered
  3. Проверка «снаружи» (TCP-порт должен отвечать):
       nc -vz ${PUBLIC_HOST} ${PORT}
     Рукопожатие выглядит как обычный TLS к ${FAKE_TLS_DOMAIN}:
       openssl s_client -connect ${PUBLIC_HOST}:${PORT} -servername ${FAKE_TLS_DOMAIN} </dev/null | head -5
  4. Ссылка клиенту: панель → инбаунды → ${REMARK} → клиент → QR/ссылка
     (tg://proxy?server=…&port=…&secret=ee…). Импортируй в Telegram и отправь
     себе сообщение — это и есть проверка «минимального интернета».
  5. Android проверяем обязательно, iOS — отдельно: с 01.04.2026 детект
     TLS-фингерпринта сломал FakeTLS на iOS (Android-релиз починили).
  6. Логи sidecar'а:
       journalctl -u x-ui -n 50
     и панель → инбаунд → «Логи».
  7. Если Telegram не подключается, а порт открыт — проверь, что
     fakeTlsDomain (${FAKE_TLS_DOMAIN}) не заблокирован у оператора: FakeTLS
     маскируется под TLS именно к нему.
  8. MTProto не заменяет VPN: в подписке он идёт только raw-ссылкой, общий
     интернет через него не ходит. Держи его третьим каналом рядом с
     Reality+Vision и XHTTP (§2.10).
EOF
}

main "$@"
