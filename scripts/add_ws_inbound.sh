#!/usr/bin/env bash
# VLESS + WebSocket + TLS: инбаунд для 3x-ui + готовые конфиги клиенту.
#
# Зачем именно этот транспорт:
#   * WS — единственный TCP-транспорт, который корректно живёт за CDN:
#     это ОДИН длинный Upgrade-коннект, а не сотни уникальных POST-URL
#     (на URL-ах и палится XHTTP packet-up — см. docs/LTE-ВАРИАНТЫ-2026-10.md, §5.5);
#   * обычный TLS с настоящим сертификатом + опционально ECH;
#   * поддерживается всеми клиентами, включая Streisand (в нём нет XHTTP).
#
# Что делает скрипт:
#   1. создаёт в панели инбаунд VLESS+WS+TLS (идемпотентно, по remark);
#   2. создаёт клиента и печатает готовые конфиги: vless:// ссылку,
#      outbound для sing-box (JSON) и proxy для Clash (YAML).
#
# Ничего не меняет без --dry-run-подтверждения: сначала печатает payload.
#
# Примеры:
#   PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
#   ./scripts/add_ws_inbound.sh --domain vpn.example.com \
#       --cert /etc/letsencrypt/live/vpn.example.com/fullchain.pem \
#       --key  /etc/letsencrypt/live/vpn.example.com/privkey.pem \
#       --path /ws --port 2053 --client-email ws-user1 --dry-run
#
#   # За Cloudflare (проксирование включено): порт держим 443, Host = домен
#   ./scripts/add_ws_inbound.sh --domain vpn.example.com --behind-cdn \
#       --cert ... --key ... --path /ws
#
#   # Прямое подключение к своему серверу (без CDN), порт нестандартный:
#   ./scripts/add_ws_inbound.sh --domain vpn.example.com --port 8443 \
#       --cert ... --key ... --path /ws
#
# Требования на ноде: bash, curl, jq. Сертификат — свой (Let's Encrypt на домен
# или IP-сертификат, см. §5.2 документа); при --behind-cdn сертификат нужен
# на стороне CDN, а на сервере можно оставить самоподписанный (CDN ходит
# к origin по IP — см. --origin-selfsigned).

set -euo pipefail

PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"

DOMAIN=""
CERT_FILE=""
KEY_FILE=""
WS_PATH="/ws"
PORT="8443"
REMARK="VLESS-WS-TLS"
CLIENT_EMAIL=""
CLIENT_UUID=""
BEHIND_CDN=0
ORIGIN_SELFSIGNED=0
DRY_RUN=0

log()  { printf '[ws-tls] %s\n' "$*"; }
ok()   { printf '[ws-tls] ✅ %s\n' "$*"; }
warn() { printf '[ws-tls] ⚠️  %s\n' "$*" >&2; }
die()  { printf '[ws-tls] ❌ %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,40p' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)            DOMAIN="${2:?}"; shift 2 ;;
        --cert)              CERT_FILE="${2:?}"; shift 2 ;;
        --key)               KEY_FILE="${2:?}"; shift 2 ;;
        --path)              WS_PATH="${2:?}"; shift 2 ;;
        --port)              PORT="${2:?}"; shift 2 ;;
        --remark)            REMARK="${2:?}"; shift 2 ;;
        --client-email)      CLIENT_EMAIL="${2:?}"; shift 2 ;;
        --client-uuid)       CLIENT_UUID="${2:?}"; shift 2 ;;
        --behind-cdn)        BEHIND_CDN=1; shift ;;
        --origin-selfsigned) ORIGIN_SELFSIGNED=1; shift ;;
        --dry-run)           DRY_RUN=1; shift ;;
        -h|--help)           usage ;;
        *) die "Неизвестный аргумент: $1" ;;
    esac
done

command -v jq >/dev/null 2>&1 || die "Нужен jq."
[[ -n "$DOMAIN" ]] || die "Обязателен --domain (SNI/Host; для CDN — домен, который проксируется)."

rand_uuid() { cat /proc/sys/kernel/random/uuid 2>/dev/null || openssl rand -hex 16; }
CLIENT_UUID="${CLIENT_UUID:-$(rand_uuid)}"
CLIENT_EMAIL="${CLIENT_EMAIL:-ws-$(printf '%s' "$CLIENT_UUID" | cut -c1-8)}"

api_get() {
    curl -sS --max-time 25 -H "Authorization: Bearer ${PANEL_TOKEN}" "${PANEL_URL}$1"
}
api_post() {
    curl -sS --max-time 30 -X POST \
        -H "Authorization: Bearer ${PANEL_TOKEN}" \
        -H 'Content-Type: application/json' -d "$2" "${PANEL_URL}$1"
}

# --- 1. streamSettings ------------------------------------------------------
# WS поверх TLS: ALPN http/1.1 (иначе WebSocket не согласуется), min TLS 1.2.
# allowInsecure в payload НЕ кладём — в Xray-core 26.9.30 он удалён.
if [[ "$ORIGIN_SELFSIGNED" -eq 1 ]]; then
    TLS_BLOCK='{"serverName":"'"$DOMAIN"'","minVersion":"1.2","maxVersion":"1.3","alpn":["http/1.1"],"certificates":[{"certificateFile":"'"$CERT_FILE"'","keyFile":"'"$KEY_FILE"'"}]}'
else
    [[ -n "$CERT_FILE" && -n "$KEY_FILE" ]] || die "Нужны --cert и --key (или --origin-selfsigned с самоподписанным сертификатом)."
    if [[ "$DRY_RUN" -eq 0 ]]; then
        [[ -r "$CERT_FILE" ]] || die "Не читается сертификат: $CERT_FILE"
        [[ -r "$KEY_FILE" ]]  || die "Не читается ключ: $KEY_FILE"
    fi
    TLS_BLOCK='{"serverName":"'"$DOMAIN"'","minVersion":"1.2","maxVersion":"1.3","alpn":["http/1.1"],"certificates":[{"certificateFile":"'"$CERT_FILE"'","keyFile":"'"$KEY_FILE"'"}]}'
fi

STREAM="$(jq -nc --arg path "$WS_PATH" --argjson tls "$TLS_BLOCK" \
    '{network:"ws",security:"tls",externalProxy:[],
      wsSettings:{path:$path,host:"",heartbeatPeriod:0},
      tlsSettings:$tls}')"

SETTINGS='{"clients":[],"decryption":"none","fallbacks":[]}'
SNIFFING='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":false}'

PAYLOAD="$(jq -nc --arg remark "$REMARK" --argjson port "$PORT" \
    --arg settings "$SETTINGS" --arg stream "$STREAM" --arg sniffing "$SNIFFING" \
    '{up:0,down:0,total:0,remark:$remark,enable:true,expiryTime:0,listen:"",
      port:$port,protocol:"vless",settings:$settings,streamSettings:$stream,sniffing:$sniffing}')"

# --- 2. конфиги клиента -----------------------------------------------------
FP="firefox"          # Chrome/Safari/iOS — в чёрном списке волны июня 2026
if [[ "$BEHIND_CDN" -eq 1 ]]; then
    CONNECT_HOST="$DOMAIN"
else
    CONNECT_HOST="${DOMAIN}"
fi

LINK="vless://${CLIENT_UUID}@${CONNECT_HOST}:${PORT}?type=ws&security=tls&path=$(printf '%s' "$WS_PATH" | jq -sRr @uri)&host=${DOMAIN}&sni=${DOMAIN}&fp=${FP}&alpn=http%2F1.1#${REMARK}"

SB_JSON="$(jq -nc --arg uuid "$CLIENT_UUID" --arg host "$CONNECT_HOST" --arg sni "$DOMAIN" \
    --arg path "$WS_PATH" --argjson port "$PORT" --arg fp "$FP" --arg tag "$REMARK" \
    '{type:"vless",tag:$tag,server:$host,server_port:$port,uuid:$uuid,
      tls:{enabled:true,server_name:$sni,utls:{enabled:true,fingerprint:$fp},
           alpn:["http/1.1"]},
      transport:{type:"ws",path:$path,headers:{Host:$sni}}}')"

CLASH_YAML="$(cat <<YAML
- name: ${REMARK}
  type: vless
  server: ${CONNECT_HOST}
  port: ${PORT}
  uuid: ${CLIENT_UUID}
  tls: true
  servername: ${DOMAIN}
  client-fingerprint: ${FP}
  network: ws
  ws-opts:
    path: "${WS_PATH}"
    headers:
      Host: ${DOMAIN}
YAML
)"

echo
log "Инбаунд: VLESS + WebSocket + TLS"
printf '    порт       : %s\n    путь       : %s\n    SNI/Host   : %s\n    сертификат : %s\n' \
    "$PORT" "$WS_PATH" "$DOMAIN" "${CERT_FILE:-self-signed}"

if [[ "$BEHIND_CDN" -eq 1 ]]; then
    echo
    warn "Режим CDN: на стороне CDN должно быть включено проксирование (оранжевое облако) и WebSockets."
    warn "Порты, которые пропускает Cloudflare: 443, 2053, 2083, 2087, 2096, 8443 (HTTPS)."
    warn "Нестандартные порты CF режет WAF-правилом — держи 443 или 2053/2083/2087/2096/8443."
    warn "ToS Cloudflare запрещает прокси/VPN — риск бана аккаунта (см. §5.4 документа)."
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo
    log "DRY-RUN — в панель ничего не отправляю. Payload:"
    printf '%s\n' "$PAYLOAD" | jq .
else
    [[ -n "$PANEL_URL" && -n "$PANEL_TOKEN" ]] || die "Нужны PANEL_URL и PANEL_TOKEN (или запусти с --dry-run)."

    existing="$(api_get /panel/api/inbounds/list | jq -r --arg r "$REMARK" '[.obj[]? | select(.remark == $r) | .id] | first // empty')"
    if [[ -n "$existing" ]]; then
        ok "Инбаунд «${REMARK}» уже есть (id=${existing}) — не дублирую."
        INBOUND_ID="$existing"
    else
        resp="$(api_post /panel/api/inbounds/add "$PAYLOAD")"
        [[ "$(printf '%s' "$resp" | jq -r '.success // false')" == "true" ]] \
            || die "Панель не создала инбаунд: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"')"
        INBOUND_ID="$(printf '%s' "$resp" | jq -r '.obj.id // empty')"
        ok "Инбаунд создан (id=${INBOUND_ID:-?})."
    fi

    client_payload="$(jq -nc --arg id "$CLIENT_UUID" --arg email "$CLIENT_EMAIL" \
        --argjson iid "${INBOUND_ID:-0}" \
        '{client:{id:$id,email:$email,flow:"",limitIp:0,totalGB:0,expiryTime:0,enable:true,subId:"",tgId:0},
          inboundIds:[$iid]}')"
    resp="$(api_post /panel/api/clients/add "$client_payload")"
    if [[ "$(printf '%s' "$resp" | jq -r '.success // false')" != "true" ]]; then
        warn "Клиента создать не удалось: $(printf '%s' "$resp" | jq -r '.msg // "нет сообщения"'). Создай вручную в панели."
    else
        ok "Клиент ${CLIENT_EMAIL} создан."
    fi
fi

echo
echo "====================== КОНФИГИ КЛИЕНТУ ======================"
echo
printf 'vless:// ссылка (v2rayNG / Happ / Streisand):\n  %s\n' "$LINK"
echo
printf 'sing-box (вставить в outbounds):\n%s\n' "$SB_JSON" | jq . 2>/dev/null || printf '%s\n' "$SB_JSON"
echo
printf 'Clash / mihomo (proxies):\n%s\n' "$CLASH_YAML"
echo "============================================================="
echo
cat <<EOF
Проверка на сервере:
  ss -tlnp | grep ':${PORT}'
  curl -sS -o /dev/null -w '%{http_code}\n' --resolve ${DOMAIN}:${PORT}:127.0.0.1 https://${DOMAIN}:${PORT}${WS_PATH}
  # рукопожатие и ALPN должны быть http/1.1:
  openssl s_client -connect ${DOMAIN}:${PORT} -servername ${DOMAIN} -alpn http/1.1 </dev/null | grep -E 'Protocol|ALPN'

Что дальше (см. docs/LTE-ВАРИАНТЫ-2026-10.md):
  * этот инбаунд — второй профиль в подписке, рядом с Reality (§2.2). Не заменяем, а добавляем;
  * за CDN замерить реальную скорость с LTE: у части операторов трафик к CDN режется (~16 КБ/соединение);
  * если CDN отвалится — тот же инбаунд работает напрямую: в ссылке заменить host на IP сервера,
    SNI оставить домен, сертификат уже стоит на сервере.
EOF
