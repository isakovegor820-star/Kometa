#!/usr/bin/env bash
# =============================================================================
#  Kometa — инбаунд VLESS + XHTTP (mode=stream-up) в 3x-ui
#  TLS терминирует СВОЙ nginx; сценарий «за CDN» — флаг --behind-cdn
# =============================================================================
#  Почему XHTTP и почему жёстко stream-up:
#    * XHTTP (он же SplitHTTP) — единственный транспорт Xray, который
#      мультиплексирует запросы в одно H2/H3-соединение (XMUX). Это лечит
#      эвристику «>3 параллельных TLS к одному SNI за 60 секунд» (июнь 2026),
#      от которой страдают Vision и любой RAW-TCP;
#    * packet-up дробит аплинк на отдельные POST-запросы. Замер августа 2026:
#      605 запросов/мин и ~870 тыс./сутки на четырёх клиентов, обфускация имён
#      полей дала снижение запросов ровно ноль, один CDN забанил ресурс навсегда.
#      Поэтому mode здесь не «настройка по вкусу», а константа stream-up;
#    * stream-up — длинный поток в обе стороны, поэтому его нельзя отдавать
#      Apache/шаред-хостингу (буферизуют тело целиком). Только свой nginx
#      с proxy_buffering off и proxy_request_buffering off.
#  Источники: docs/LTE-ВАРИАНТЫ-2026-10.md §2.1 и §5.5, docs/БЕЛЫЕ-СПИСКИ-ВНЕДРЕНИЕ.md,
#             .research/entry-points-ru.md, XTLS discussion #4113.
#
#  Схема в обоих режимах (Xray слушает только loopback, TLS — на nginx):
#      клиент --TLS/h2--> nginx:443 --HTTP/1.1--> Xray 127.0.0.1:<--port>
#      --behind-cdn: клиент -> CDN (TLS) -> origin nginx:443 -> Xray (то же самое)
#
#  Что делает скрипт:
#    1. печатает ТОЧНЫЙ JSON-payload инбаунда, nginx-локацию и чек-лист CDN;
#       по умолчанию в панель не ходит вообще (dry-run, сетевых запросов нет);
#    2. с --apply: проверяет API, ищет инбаунд по remark (идемпотентность),
#       создаёт инбаунд и одного клиента, печатает vless://, sing-box, Clash;
#    3. в конце — блок «Как проверить» (что смотреть руками на ноде).
#
#  Идемпотентность: повторный запуск не создаёт дубликаты — инбаунд ищется по
#  remark (Kometa-XHTTP, за CDN — Kometa-XHTTP-CDN) через GET /panel/api/inbounds/list.
#  Remark НЕ содержит порт: смена --port не должна плодить второй инбаунд.
#
#  Примеры:
#     # 1. Посмотреть, что уйдёт в панель (в панель не ходит):
#     PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
#       ./scripts/add_xhttp_inbound.sh --domain vpn.example.com
#
#     # 2. Создать (порт инбаунда 2096, nginx проксирует на него):
#     PANEL_URL=... PANEL_TOKEN=... ./scripts/add_xhttp_inbound.sh \
#       --domain vpn.example.com --port 2096 --path /api/v1/updates --apply
#
#     # 3. За CDN: самоподписанный сертификат на origin, клиенты идут в CDN:
#     PANEL_URL=... PANEL_TOKEN=... ./scripts/add_xhttp_inbound.sh \
#       --domain vpn.example.com --behind-cdn --origin-selfsigned --apply
#
#  Зависимости: bash, jq (всегда), curl (только с --apply), openssl (UUID).
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------- параметры -------
PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"
DOMAIN="${DOMAIN:-}"
SNI="${SNI:-}"
CDN_HOST="${CDN_HOST:-}"
PORT="${PORT:-2096}"
LISTEN_ADDR="${LISTEN_ADDR:-127.0.0.1}"
XHTTP_PATH="${XHTTP_PATH:-/api/v1/updates}"
REMARK="${REMARK:-}"
CERT_FILE="${CERT_FILE:-}"
KEY_FILE="${KEY_FILE:-}"
CLIENT_EMAIL="${CLIENT_EMAIL:-}"
CLIENT_UUID="${CLIENT_UUID:-}"
BEHIND_CDN="${BEHIND_CDN:-0}"
ORIGIN_SELFSIGNED="${ORIGIN_SELFSIGNED:-0}"
CREATE_CLIENT="${CREATE_CLIENT:-1}"

# Режим XHTTP. stream-up — не «один из вариантов», а требование безопасности:
# packet-up = отдельный HTTP-запрос на каждый пакет (605 запросов/мин в замере)
# и гарантированный бан ресурса на CDN. Менять только осознанно и вручную.
XHTTP_MODE="stream-up"
FP="firefox"                 # chrome/safari/ios — чёрный список волны июня 2026

APPLY=0
REMARK_EXPLICIT=0
CERT_EXPLICIT=0
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
Инбаунд VLESS + XHTTP (mode=stream-up) в 3x-ui. TLS терминирует свой nginx.

По умолчанию — DRY-RUN: скрипт печатает точный JSON-payload, nginx-локацию и
чек-лист CDN, но в панель НЕ ходит (ни одного сетевого запроса).
Создание — только с явным --apply.

Использование:
  PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
    ./scripts/add_xhttp_inbound.sh --domain vpn.example.com [флаги]

Обязательно:
  PANEL_URL          адрес панели (env или --panel-url)
  PANEL_TOKEN        API-токен панели, скоуп admin (env или --panel-token)
  --domain DOMAIN    домен, на который выпущен сертификат nginx (SNI/Host)

Флаги:
  --port N           порт инбаунда Xray на loopback (по умолчанию 2096);
                     nginx проксирует на него, наружу порт не открываем
  --listen ADDR      адрес прослушивания инбаунда (по умолчанию 127.0.0.1;
                     0.0.0.0 — только если nginx стоит на другой машине)
  --path PATH        HTTP-путь XHTTP (по умолчанию /api/v1/updates; бери
                     нейтральный, не пересекающийся с реальным сайтом)
  --sni DOMAIN       SNI/Host для клиента (по умолчанию = --domain)
  --remark NAME      имя инбаунда (по умолчанию Kometa-XHTTP,
                     в режиме --behind-cdn — Kometa-XHTTP-CDN)
  --client-email E   email клиента в панели (по умолчанию xhttp-<uuid8>)
  --client-uuid U    UUID клиента (по умолчанию — случайный)
  --no-client        не создавать клиента (клиентов создаёт бот)
  --behind-cdn       сценарий «за CDN»: клиенты ходят в CDN, origin — этот
                     сервер; печатает чек-лист настроек CDN
  --cdn-host HOST    хост, который резолвится в CDN (если отличается от --domain)
  --origin-selfsigned  на origin самоподписанный сертификат (CDN должен ходить
                     в режиме Full, НЕ Full (strict)); только с --behind-cdn
  --cert FILE        сертификат для nginx (по умолчанию Let's Encrypt по --domain)
  --key FILE         ключ сертификата для nginx
  --panel-url URL    адрес панели (альтернатива PANEL_URL)
  --panel-token T    токен панели (альтернатива PANEL_TOKEN)
  --insecure         не проверять TLS-сертификат панели (самоподписанный)
  --apply            реально создать инбаунд (без него — только показать)
  --dry-run          явный dry-run (то же поведение, что и по умолчанию)
  -h, --help         эта справка

Переменные окружения: PANEL_URL, PANEL_TOKEN, DOMAIN, SNI, CDN_HOST, PORT,
LISTEN_ADDR, XHTTP_PATH, REMARK, CERT_FILE, KEY_FILE, CLIENT_EMAIL, CLIENT_UUID,
BEHIND_CDN=1, ORIGIN_SELFSIGNED=1.
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

# Идемпотентность: точное совпадение по remark, затем «семейство» Kometa-XHTTP-*.
find_inbound_by_remark() { # <json-список> <remark> → id или пусто
    printf '%s' "$1" | jq -r --arg r "$2" '[.obj[]? | select(.remark == $r) | .id] | first // empty'
}

find_inbound_by_prefix() { # <json-список> <префикс> → "id<TAB>remark<TAB>port" или пусто
    printf '%s' "$1" | jq -r --arg p "$2" \
        '[.obj[]? | select((.remark // "") | startswith($p)) | "\(.id)\t\(.remark)\t\(.port)"] | first // empty'
}

inbound_field() { # <json-список> <remark> <jq-путь внутри инбаунда>
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
        warn "Если это не сам инбаунд — nginx будет проксировать в занятый порт: возьми свободный (--port)."
    else
        ok "Порт ${port}/tcp на этой машине свободен."
    fi
}

# ------------------------------------------------------------ payload ---------
# xhttpSettings — поля ровно те, что читает серверный слушатель Xray/3x-ui:
#   * mode: stream-up (см. шапку);
#   * xPaddingBytes 100-1000 — дефолт ядра, не трогаем (padding лечит размеры);
#   * scMinPostsIntervalMs оставляем пустым: 3x-ui #5141 — ТСПУ цепляется за
#     зашитый в конфиг scMinPostsIntervalMs=30, а пустое значение = дефолт ядра;
#   * scStreamUpServerSecs 20-80 — сервер шлёт padding в «молчащий» stream-up
#     (CDN рвёт HTTP-поток без данных примерно через 100 с);
#   * xmux: maxConcurrency и maxConnections взаимоисключающие — здесь задан
#     только maxConcurrency, maxConnections=0;
#   * security: none — TLS терминирует nginx, Xray слушает loopback.
build_payload() {
    local stream settings sniffing
    stream="$(jq -nc \
        --arg path "$XHTTP_PATH" \
        --arg host "$DOMAIN" \
        --arg mode "$XHTTP_MODE" \
        '{network:"xhttp",security:"none",externalProxy:[],
          xhttpSettings:{
            path:$path,host:$host,mode:$mode,
            xPaddingBytes:"100-1000",
            scMaxEachPostBytes:"",
            scMinPostsIntervalMs:"",
            scMaxBufferedPosts:30,
            scStreamUpServerSecs:"20-80",
            noSSEHeader:false,
            headers:{},
            xmux:{maxConcurrency:"8-16",maxConnections:0,cMaxReuseTimes:0,
                  hMaxRequestTimes:"400-600",hMaxReusableSecs:"900-1500",
                  hKeepAlivePeriod:0}},
          tlsSettings:{}}')"

    settings='{"clients":[],"decryption":"none","fallbacks":[]}'
    sniffing='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":false}'

    PAYLOAD="$(jq -nc \
        --arg remark "$REMARK" --argjson port "$PORT" --arg listen "$LISTEN_ADDR" \
        --arg settings "$settings" --arg stream "$stream" --arg sniffing "$sniffing" \
        '{up:0,down:0,total:0,remark:$remark,enable:true,expiryTime:0,listen:$listen,
          port:$port,protocol:"vless",settings:$settings,streamSettings:$stream,sniffing:$sniffing}')"
}

# ------------------------------------------------------- конфиги клиенту ------
print_client_configs() { # <uuid>
    local uuid="$1" link sb_json clash
    link="vless://${uuid}@${CONNECT_HOST}:443?type=xhttp&security=tls&path=$(printf '%s' "$XHTTP_PATH" | jq -sRr @uri)&host=${DOMAIN}&sni=${DOMAIN}&fp=${FP}&mode=${XHTTP_MODE}#${REMARK}"

    sb_json="$(jq -nc --arg uuid "$uuid" --arg host "$CONNECT_HOST" --arg sni "$DOMAIN" \
        --arg path "$XHTTP_PATH" --arg mode "$XHTTP_MODE" --arg fp "$FP" --arg tag "$REMARK" \
        '{type:"vless",tag:$tag,server:$host,server_port:443,uuid:$uuid,
          tls:{enabled:true,server_name:$sni,utls:{enabled:true,fingerprint:$fp},alpn:["h2"]},
          transport:{type:"xhttp",path:$path,host:$sni,mode:$mode}}')"

    clash="$(cat <<YAML
- name: ${REMARK}
  type: vless
  server: ${CONNECT_HOST}
  port: 443
  uuid: ${uuid}
  udp: true
  tls: true
  servername: ${DOMAIN}
  client-fingerprint: ${FP}
  network: xhttp
  xhttp-opts:
    path: "${XHTTP_PATH}"
    mode: ${XHTTP_MODE}
    headers:
      Host: ${DOMAIN}
YAML
)"

    box "КОНФИГИ КЛИЕНТУ"
    printf 'vless:// ссылка (v2rayNG / Happ / sing-box; Streisand XHTTP НЕ умеет):\n  %s\n\n' "$link"
    printf 'sing-box (вставить в outbounds; mux.cool с XHTTP включать НЕЛЬЗЯ —\n'
    printf 'в клиенте multiplex.enabled=false, иначе сервер отклонит соединение):\n'
    printf '%s\n' "$sb_json" | jq . 2>/dev/null || printf '%s\n' "$sb_json"
    printf '\nClash / mihomo (proxies):\n%s\n' "$clash"
}

# --------------------------------------------------------- nginx и CDN --------
print_nginx_hint() {
    box "NGINX: ЛОКАЦИЯ ДЛЯ XHTTP"
    cat <<NGINX
# /etc/nginx/sites-enabled/kometa-xhttp.conf  (или location в существующий server{})
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;                       # nginx < 1.25: listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate     ${CERT_FILE};
    ssl_certificate_key ${KEY_FILE};
    ssl_protocols       TLSv1.2 TLSv1.3;

    location ${XHTTP_PATH} {
        proxy_pass http://${LISTEN_ADDR}:${PORT};   # порт инбаунда, БЕЗ хвостового /
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # stream-up — ДОЛГИЙ поток в обе стороны. Буферизация и короткие
        # таймауты убивают его: nginx накопит тело и порвёт соединение.
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
        proxy_connect_timeout 15s;
        chunked_transfer_encoding on;
    }
}
# Проверка синтаксиса: nginx -t && systemctl reload nginx
NGINX

    if [[ "$ORIGIN_SELFSIGNED" -eq 1 ]]; then
        echo
        warn "Origin c самоподписанным сертификатом (${CERT_FILE}). Если его нет:"
        warn "  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 3650 -nodes \\"
        warn "    -keyout ${KEY_FILE} -out ${CERT_FILE} -subj \"/CN=${DOMAIN}\""
        warn "CDN к такому origin ходит только в режиме Full (НЕ Full (strict))."
    fi
    echo
    log "PATH нейтральный? «${XHTTP_PATH}» не должен совпадать с реальным API сайта:"
    log "запросы к нему пойдут в Xray, и живой сайт на этом пути сломается."
}

print_cdn_hint() {
    box "ЧТО ВКЛЮЧИТЬ В CDN (--behind-cdn)"
    cat <<EOF
  1. Проксирование (Cloudflare «оранжевое облако», а не DNS-only): клиент
     должен видеть IP CDN, а не IP ноды.
  2. WebSockets — ON, HTTP/2 — ON, HTTP/3 — ON (если CDN умеет). stream-up
     живёт именно на H2/H3-стримах; без них останется packet-up-поведение.
  3. ORIGIN только по https (SSL/TLS mode: Full). Если origin с самоподписанным
     сертификатом — ровно Full, «Full (strict)» отвергнет сертификат.
  4. Кэширование пути ${XHTTP_PATH} — bypass (Cache Rule → Bypass). Ответы XHTTP
     уникальны, кэш даёт нулевой hit rate и лишний отпечаток.
  5. Минификацию HTML/Rocket Loader/Auto Minify — OFF: они переписывают поток.
  6. Порты, которые пропускает Cloudflare: 443, 2053, 2083, 2087, 2096, 8443.
     Нестандартный порт CDN режет WAF-правилом — держи 443 на стороне CDN.
  7. Таймаут без данных у Cloudflare ~100 с: поэтому scStreamUpServerSecs
     оставлен 20-80 — сервер сам шлёт padding в молчащий поток.
  8. ToS: Cloudflare запрещает проксирование VPN/прокси — риск бана аккаунта
     (docs/LTE-ВАРИАНТЫ-2026-10.md §5.4). Разрешение проксировать VPN не
     подтверждено ни у одного CDN с точками в РФ.
EOF
}

print_packet_up_warning() {
    warn "Только stream-up. В packet-up каждый пакет аплинка — ОТДЕЛЬНЫЙ HTTP-запрос:"
    warn "605 запросов/мин и ~870 тыс./сутки на 4 клиентов, обфускация имён полей дала"
    warn "ноль снижения, один CDN забанил ресурс навсегда (.research/entry-points-ru.md)."
    warn "Проходит только stream-up + свой nginx; Apache/шаред-хостинг буферизуют тело."
}

# ------------------------------------------------------------ применение ------
apply_inbound() {
    local existing id prefix_hit existing_port resp client_payload new_id
    existing="$(api_get /panel/api/inbounds/list)"
    [[ "$(printf '%s' "$existing" | jq -r '.success // false')" == "true" ]] \
        || die "Не удалось получить список инбаундов: $(printf '%s' "$existing" | jq -r '.msg // "нет сообщения"')"

    id="$(find_inbound_by_remark "$existing" "$REMARK")"
    if [[ -n "$id" ]]; then
        existing_port="$(inbound_field "$existing" "$REMARK" "port")"
        ok "Инбаунд «${REMARK}» уже есть (id=${id}, порт ${existing_port}) — не дублирую (идемпотентность)."
        if [[ -n "$existing_port" && "$existing_port" != "$PORT" ]]; then
            warn "Он слушает порт ${existing_port}, а запрошен ${PORT}: порт я НЕ меняю."
            warn "Смена порта — вручную: панель → инбаунды → ${REMARK} → порт, затем перезапуск xray."
        fi
        INBOUND_ID="$id"
        EXISTING_JSON="$existing"
        MATCHED_REMARK="$REMARK"
        return 0
    fi

    # Точного имени нет, но есть инбаунд того же семейства (например, созданный
    # раньше с другим именем). Без явного --remark не плодим второй профиль.
    prefix_hit="$(find_inbound_by_prefix "$existing" "Kometa-XHTTP")"
    if [[ -n "$prefix_hit" && "$REMARK_EXPLICIT" -eq 0 ]]; then
        ok "Найден инбаунд семейства Kometa-XHTTP (id/remark/port: $(printf '%s' "$prefix_hit" | tr '\t' ' ')) — не дублирую."
        log "Нужен именно второй профиль? Запусти с явным --remark <другое имя>."
        INBOUND_ID="$(printf '%s' "$prefix_hit" | cut -f1)"
        EXISTING_JSON="$existing"
        MATCHED_REMARK="$(printf '%s' "$prefix_hit" | cut -f2)"
        return 0
    fi

    log "Создаю инбаунд «${REMARK}» (XHTTP/${XHTTP_MODE}, ${LISTEN_ADDR}:${PORT})..."
    resp="$(api_post /panel/api/inbounds/add "$PAYLOAD")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        die "Панель не создала инбаунд: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"'). Обнови панель: XHTTP требует 3x-ui с поддержкой xhttpSettings."
    fi
    new_id="$(printf '%s' "$resp" | jq -r '.obj.id // empty')"
    ok "Инбаунд создан (id=${new_id:-?})."
    INBOUND_ID="${new_id:-0}"
    EXISTING_JSON=""
    MATCHED_REMARK="$REMARK"

    if [[ "$CREATE_CLIENT" -ne 1 ]]; then
        log "Клиента не создаю (--no-client): их создаёт бот."
        return 0
    fi

    client_payload="$(jq -nc --arg id "$CLIENT_UUID" --arg email "$CLIENT_EMAIL" \
        --argjson iid "${INBOUND_ID:-0}" \
        '{client:{id:$id,email:$email,flow:"",limitIp:0,totalGB:0,expiryTime:0,
                  enable:true,subId:"",tgId:0},
          inboundIds:[$iid]}')"
    resp="$(api_post /panel/api/clients/add "$client_payload")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        warn "Клиента создать не удалось: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"'). Создай вручную в панели."
    else
        ok "Клиент ${CLIENT_EMAIL} создан."
    fi
}

# ---------------------------------------------------------------- main --------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --domain)            DOMAIN="${2:?}"; shift 2 ;;
            --sni)               SNI="${2:?}"; shift 2 ;;
            --cdn-host)          CDN_HOST="${2:?}"; shift 2 ;;
            --port)              PORT="${2:?}"; shift 2 ;;
            --listen)            LISTEN_ADDR="${2:?}"; shift 2 ;;
            --path)              XHTTP_PATH="${2:?}"; shift 2 ;;
            --remark)            REMARK="${2:?}"; REMARK_EXPLICIT=1; shift 2 ;;
            --cert)              CERT_FILE="${2:?}"; CERT_EXPLICIT=1; shift 2 ;;
            --key)               KEY_FILE="${2:?}"; CERT_EXPLICIT=1; shift 2 ;;
            --client-email)      CLIENT_EMAIL="${2:?}"; shift 2 ;;
            --client-uuid)       CLIENT_UUID="${2:?}"; shift 2 ;;
            --no-client)         CREATE_CLIENT=0; shift ;;
            --behind-cdn)        BEHIND_CDN=1; shift ;;
            --origin-selfsigned) ORIGIN_SELFSIGNED=1; shift ;;
            --panel-url)         PANEL_URL="${2:?}"; shift 2 ;;
            --panel-token)       PANEL_TOKEN="${2:?}"; shift 2 ;;
            --insecure)          CURL_OPTS+=(--insecure); shift ;;
            --apply)             APPLY=1; shift ;;
            --dry-run)           APPLY=0; shift ;;
            -h | --help)         usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    have jq || die "Нужен jq (apt install jq / brew install jq)."
    require_panel

    [[ -n "$DOMAIN" ]] || die "Обязателен --domain: домен, на который выпущен сертификат nginx (он же SNI/Host)."
    [[ "$PORT" =~ ^[0-9]+$ ]] || die "--port должен быть числом, получено: ${PORT}"
    [[ "$PORT" -ge 1 && "$PORT" -le 65535 ]] || die "--port вне диапазона 1..65535: ${PORT}"
    case "$XHTTP_PATH" in
        /*) : ;;
        *) die "--path должен начинаться с «/»: ${XHTTP_PATH}" ;;
    esac

    # Значения по умолчанию, зависящие от режима.
    SNI="${SNI:-$DOMAIN}"
    if [[ -z "$REMARK" ]]; then
        if [[ "$BEHIND_CDN" -eq 1 ]]; then REMARK="Kometa-XHTTP-CDN"; else REMARK="Kometa-XHTTP"; fi
    fi
    CONNECT_HOST="${CDN_HOST:-$DOMAIN}"

    if [[ "$ORIGIN_SELFSIGNED" -eq 1 && "$CERT_EXPLICIT" -eq 0 ]]; then
        CERT_FILE="/etc/ssl/kometa/origin.crt"
        KEY_FILE="/etc/ssl/kometa/origin.key"
    fi
    [[ -n "$CERT_FILE" ]] || CERT_FILE="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
    [[ -n "$KEY_FILE" ]]  || KEY_FILE="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"

    if [[ "$ORIGIN_SELFSIGNED" -eq 1 && "$BEHIND_CDN" -ne 1 ]]; then
        warn "--origin-selfsigned без --behind-cdn: клиенты увидят самоподписанный"
        warn "сертификат и откажутся подключаться. Годится только для отладки."
    fi

    CLIENT_UUID="${CLIENT_UUID:-$(rand_uuid)}"
    CLIENT_EMAIL="${CLIENT_EMAIL:-xhttp-$(printf '%s' "$CLIENT_UUID" | cut -c1-8)}"

    build_payload
    INBOUND_ID=""
    EXISTING_JSON=""
    MATCHED_REMARK=""
    UUID_KNOWN=1   # 0 — UUID взят из панели не был, в ссылке будет плейсхолдер

    box "KOMETA • XHTTP-ИНБАУНД (VLESS + XHTTP, mode=${XHTTP_MODE})"
    printf '  панель      : %s\n' "$PANEL_URL"
    printf '  домен/SNI   : %s (клиент подключается к %s:443)\n' "$DOMAIN" "$CONNECT_HOST"
    printf '  инбаунд     : %s:%s/tcp (наружу НЕ открываем, только nginx)\n' "$LISTEN_ADDR" "$PORT"
    printf '  путь        : %s\n' "$XHTTP_PATH"
    printf '  remark      : %s\n' "$REMARK"
    printf '  режим       : %s%s\n' "$( [[ "$APPLY" -eq 1 ]] && printf 'APPLY (создаю)' || printf 'DRY-RUN (ничего не отправляю)' )" \
        "$( [[ "$BEHIND_CDN" -eq 1 ]] && printf ' + behind-cdn' || printf '' )"
    printf '  nginx cert  : %s\n' "$CERT_FILE"

    print_packet_up_warning
    port_busy_hint "$PORT"

    if [[ "$APPLY" -eq 1 ]]; then
        have curl || die "Нужен curl для --apply."
        box "PAYLOAD, КОТОРЫЙ УЙДЁТ В ПАНЕЛЬ (POST /panel/api/inbounds/add)"
        printf '%s\n' "$PAYLOAD" | jq .
        api_check
        apply_inbound
        # Если инбаунд уже был — попробуем достать UUID существующего клиента,
        # чтобы напечатать рабочие конфиги, а не выдуманные.
        if [[ -n "${EXISTING_JSON:-}" ]]; then
            local existing_uuid
            # В API 3x-ui settings — вложенный объект; в старых сборках — строка
            # с JSON внутри, отсюда fromjson?.
            existing_uuid="$(inbound_field "$EXISTING_JSON" "${MATCHED_REMARK:-$REMARK}" "settings")"
            existing_uuid="$(printf '%s' "$existing_uuid" \
                | jq -r 'if type == "string" then (fromjson? // {}) else . end | .clients[0].id // empty' 2>/dev/null || true)"
            if [[ -n "$existing_uuid" ]]; then
                CLIENT_UUID="$existing_uuid"
            else
                # Инбаунд есть, а клиента в нём нет (или панель не отдала settings):
                # не печатаем ссылку с выдуманным UUID.
                CLIENT_UUID="<UUID_КЛИЕНТА_ИЗ_ПАНЕЛИ>"
                UUID_KNOWN=0
            fi
        fi
    else
        box "DRY-RUN: PAYLOAD ДЛЯ POST /panel/api/inbounds/add"
        printf '%s\n' "$PAYLOAD" | jq .
        echo
        log "Сетевых запросов нет: ни GET /panel/api/inbounds/list, ни POST /panel/api/inbounds/add."
        log "Ничего не создано. Для реального создания добавь --apply."
    fi

    print_nginx_hint
    [[ "$BEHIND_CDN" -eq 1 ]] && print_cdn_hint
    print_client_configs "$CLIENT_UUID"
    if [[ "$UUID_KNOWN" -eq 0 ]]; then
        warn "В инбаунде «${MATCHED_REMARK:-$REMARK}» не видно ни одного клиента — в ссылке стоит плейсхолдер вместо UUID."
        warn "Возьми UUID клиента в панели (инбаунды → инбаунд → клиент) или создай нового."
    fi

    box "КАК ПРОВЕРИТЬ (руками на ноде)"
    cat <<EOF
  1. Инбаунд слушает loopback (панель поднимает Xray сама, ~5 с):
       ss -tlnp | grep ':${PORT}'
  2. nginx жив и знает локацию:
       nginx -t && systemctl reload nginx
  3. Запрос снаружи доходит до Xray (502 = Xray не слушает, 400/404 = дошёл):
       curl -sS -o /dev/null -w '%{http_code}\n' https://${CONNECT_HOST}${XHTTP_PATH}
     TLS и h2 на месте:
       openssl s_client -connect ${CONNECT_HOST}:443 -servername ${DOMAIN} -alpn h2 </dev/null | grep -E 'Protocol|ALPN'
  4. Клиент: импортируй vless://-ссылку в v2rayNG / Happ / sing-box
     (Streisand XHTTP не умеет). В sing-box multiplex для XHTTP — выключен.
  5. Xray-логи:
       journalctl -u x-ui -n 50
  6. За CDN: сравни скорость с LTE напрямую и через CDN — у части операторов
     трафик к CDN режется до ~16 КБ на соединение.
  7. Если «сайты висят, а пинг идёт» — это PMTUD-блэкхол LTE, а не XHTTP:
     проверь MSS-clamp (install_node.sh, MTU 1280 → 1240/1220).
  8. Инбаунд молча уходит в fallback на dest? Это баг 3x-ui 3.5.0 + Xray 26.7.11
     (3x-ui#5922): обнови панель и ядро, проверь живым клиентом.
  9. Режим инбаунда в панели должен остаться stream-up. Если кто-то переключит
     на packet-up — CDN забанит ресурс (605 запросов/мин), см. шапку скрипта.
EOF
}

main "$@"
