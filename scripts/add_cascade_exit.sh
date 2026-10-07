#!/usr/bin/env bash
# =============================================================================
#  Kometa — каскад «вход в РФ → зарубежный выход»: релей на входном узле (3x-ui)
# =============================================================================
#  Зачем. В режиме «белых списков» клиент не может подключиться к зарубежному
#  IP напрямую (отрезан на L3), поэтому схема A — вход в РФ из проверенной
#  /24, который релеит трафик на обычный зарубежный выход:
#
#      телефон (LTE, БС)
#         │  TCP 443, SNI = разрешённый домен
#         ▼
#      [ВХОД] RU VPS в «белой» /24   ← Reality-инбаунд уже стоит (install_node.sh --whitelist)
#         │  outbound «Kometa-Exit» (VLESS + Reality до выхода)
#         ▼
#      [ВЫХОД] зарубежная нода (ЕС/Азия) → интернет
#
#  Что делает ЭТОТ скрипт. Вход — не «ещё одна нода для клиентов», а релей
#  (docs/БЕЛЫЕ-СПИСКИ-ВНЕДРЕНИЕ.md §2.1). Технически это правка глобального
#  шаблона Xray панели (setting `xrayTemplateConfig`):
#    1. outbound с тегом Kometa-Exit — VLESS + Reality до зарубежного выхода;
#    2. правило маршрутизации ПЕРВЫМ — direct для разрешённых РФ-сервисов
#       (geosite:category-ru, geoip:ru, банки/Госуслуги/маркетплейсы);
#    3. правило ПОСЛЕДНИМ — всё остальное на Kometa-Exit.
#
#  Почему direct-правило обязательно и почему оно первое. Если клиентский
#  трафик к разрешённым РФ-сервисам гнать через вход, подсеть «выгорает»
#  именно на этом (docs/ОБХОД-БЕЛЫХ-СПИСКОВ-ПЛАН.md §5, docs/БЕЛЫЕ-СПИСКИ-ВНЕДРЕНИЕ.md §2.1).
#  Плюс РФ-сервисы (банки, Госуслуги) с зарубежного IP ломаются или требуют
#  дополнительных подтверждений. Поэтому: РФ-трафик — напрямую со входа,
#  всё прочее — на выход.
#
#  Почему служебное правило панели остаётся ВЫШЕ нашего direct. Штатный шаблон
#  3x-ui содержит правило {"inboundTag":["api"],"outboundTag":"api"}, которое
#  уводит служебный туннель панели (статистика, API) в отдельный outbound.
#  Наш direct-список содержит geoip:private, а API-инбаунд слушает 127.0.0.1 —
#  если поставить direct выше служебного правила, статистика панели уедет в
#  direct. Скрипт такие правила «пришпиливает» наверх, наш direct идёт сразу
#  за ними.
#
#  Идемпотентность. Повторный запуск не плодит копии:
#    * outbound ищется по тегу Kometa-Exit (--exit-remark) — найден → обновляем;
#    * наши правила помечены ruleTag Kometa-Direct / Kometa-Exit-Rule
#      (ruleTag — штатное поле правила Xray, см. infra/conf/router.go) —
#      найденные снимаются и ставятся заново в правильном порядке.
#
#  Про API панели. По ТЗ чтение — GET /panel/setting/all, запись —
#  POST /panel/setting/update, поле obj.xrayTemplateConfig (JSON-СТРОКА).
#  В живом 3x-ui эндпоинт /panel/setting/all объявлен как POST (internal/web/
#  controller/setting.go), поэтому если GET не отвечает успехом — скрипт
#  повторяет чтение через POST и говорит об этом в логе.
#  Запись всегда идёт на POST /panel/setting/update и отправляет ПОЛНЫЙ объект
#  настроек (прочитанный + подменённый xrayTemplateConfig): частичный объект
#  панель трактует как «остальные поля пустые» и может обнулить чужие настройки.
#
#  Что скрипт НЕ делает: не создаёт инбаунд входа (это install_node.sh
#  --whitelist), не трогает выходную ноду, не ходит в сеть без --apply.
#
#  Примеры:
#     # 1. Посмотреть, что уйдёт в панель (в панель не ходит вообще):
#     PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
#       ./scripts/add_cascade_exit.sh \
#         --exit-host exit.example.com --exit-uuid <UUID> \
#         --exit-pbk <PUBLIC_KEY> --exit-sni www.microsoft.com --exit-sid <SHORT_ID>
#
#     # 2. Свой список «напрямую» (по строке: geosite:/geoip:/domain:/CIDR):
#     ... ./scripts/add_cascade_exit.sh --exit-host ... --direct-file ru-direct.txt
#
#     # 3. Реально записать (сначала снимет бэкап, потом POST):
#     ... ./scripts/add_cascade_exit.sh --exit-host ... --apply
#
#     # 4. Отдать каскад только клиентскому инбаунду (служебный трафик — мимо):
#     ... ./scripts/add_cascade_exit.sh --exit-host ... --inbound-tag Kometa-Reality-443 --apply
#
#  Где взять параметры Reality выхода: их печатает install_node.sh выходной
#  ноды (блок «VLESS + REALITY»: SNI, shortId, publicKey) либо панель выхода:
#  инбаунды → инбаунд → клиент (UUID, flow, publicKey, shortId, serverName).
#
#  Зависимости: bash, jq (всегда), curl (только с --apply).
#  Опционально diff (красивый unified diff; без него печатается разбор по
#  секциям). openssl НЕ нужен: скрипт ничего не генерирует — все параметры
#  Reality берутся с выходной ноды.
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------- параметры -------
PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"
EXIT_HOST="${EXIT_HOST:-}"
EXIT_PORT="${EXIT_PORT:-443}"
EXIT_UUID="${EXIT_UUID:-}"
EXIT_PBK="${EXIT_PBK:-}"
EXIT_SNI="${EXIT_SNI:-}"
EXIT_SID="${EXIT_SID:-}"
EXIT_FLOW="${EXIT_FLOW:-xtls-rprx-vision}"
EXIT_FP="${EXIT_FP:-firefox}"
EXIT_REMARK="${EXIT_REMARK:-Kometa-Exit}"
DIRECT_FILE="${DIRECT_FILE:-}"
BACKUP_FILE="${BACKUP_FILE:-}"
DOMAIN_STRATEGY="${DOMAIN_STRATEGY:-IPIfNonMatch}"

# Метки «наших» объектов в шаблоне — по ним же работает идемпотентность.
DIRECT_RULE_TAG="Kometa-Direct"
EXIT_RULE_TAG="Kometa-Exit-Rule"
# Штатный тег freedom-outbound в шаблоне 3x-ui (config.json: protocol freedom, tag direct).
DIRECT_OUTBOUND_TAG="direct"

APPLY=0
CURL_OPTS=()
INBOUND_TAGS=()
XUI_BIN="/usr/local/x-ui/x-ui"   # локальный бинарь панели (если скрипт на ноде)
UUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

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

# Временный файл без обязательной зависимости от mktemp (есть везде, но путь
# всё равно нужен предсказуемый и убираемый).
tmp_file() {
    mktemp 2>/dev/null || printf '%s/kometa-cascade.%s.%s' "${TMPDIR:-/tmp}" "$$" "${RANDOM:-0}"
}

usage() {
    cat <<'EOF'
Каскад «вход в РФ → зарубежный выход»: outbound Kometa-Exit + правила
маршрутизации в глобальном шаблоне Xray панели 3x-ui (xrayTemplateConfig).

По умолчанию — DRY-RUN: скрипт печатает outbound, правила и diff, но в панель
НЕ ходит (ни одного сетевого запроса) и ничего не меняет.
Запись — только с явным --apply, и только после обязательного бэкапа.

Использование:
  PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
    ./scripts/add_cascade_exit.sh --exit-host <выход> --exit-uuid <UUID> \
      --exit-pbk <PUBLIC_KEY> --exit-sni <SNI> [флаги]

Обязательно:
  PANEL_URL          адрес панели ВХОДНОГО узла (env или --panel-url)
  PANEL_TOKEN        API-токен панели, скоуп admin (env или --panel-token)
  --exit-host HOST   адрес зарубежного выхода (IP или домен)
  --exit-uuid UUID   UUID клиента на выходе (VLESS id)
  --exit-pbk KEY     publicKey Reality выходной ноды (x25519, из install_node.sh)
  --exit-sni DOMAIN  serverName Reality выходной ноды (домен маскировки)

Параметры выхода:
  --exit-port N      порт выхода (по умолчанию 443)
  --exit-sid SID     shortId Reality выхода (16 hex-символов). Пусто — поле не
                     пишется, и это сработает только если в инбаунде выхода
                     shortIds содержит пустую строку: по умолчанию его задаёт
                     install_node.sh, поэтому shortId лучше указать
  --exit-flow FLOW   flow клиента (по умолчанию xtls-rprx-vision; пусто — без flow)
  --exit-fp FP       uTLS-фингерпринт (по умолчанию firefox; chrome/safari/ios —
                     чёрный список эвристики июня 2026)
  --exit-remark NAME тег outbound в шаблоне (по умолчанию Kometa-Exit)

Маршрутизация:
  --direct-file FILE файл со списком «идёт напрямую со входа»: по строке,
                     `#` — комментарий. Понимает geosite:/geoip:/domain:/full:/
                     keyword:/regexp:/CIDR/голый домен. По умолчанию — встроенный
                     список: geosite:category-ru, geoip:ru, geoip:private,
                     domain:gosuslugi.ru, domain:vk.com, domain:yandex.ru,
                     domain:ozon.ru, domain:wildberries.ru, domain:sberbank.ru,
                     domain:tbank.ru
  --inbound-tag TAG  ограничить правило «всё остальное → Kometa-Exit» этим
                     inboundTag (можно повторять). По умолчанию правило
                     ловит все инбаунды. Нужно, если на входе есть служебные
                     инбаунды, чей трафик не должен уходить в каскад
  --domain-strategy S domainStrategy роутера: IPIfNonMatch (по умолчанию; нужен,
                     чтобы geoip:ru работал для доменов), IPOnDemand, AsIs

Запись:
  --backup-file FILE путь бэкапа текущего xrayTemplateConfig
                     (по умолчанию ./backup-xray-<дата-время>.json)
  --panel-url URL    адрес панели (альтернатива PANEL_URL)
  --panel-token T    токен панели (альтернатива PANEL_TOKEN)
  --insecure         не проверять TLS-сертификат панели (самоподписанный)
  --apply            реально записать шаблон (без него — только показать)
  --dry-run          явный dry-run (то же поведение, что и по умолчанию)
  -h, --help         эта справка

Переменные окружения: PANEL_URL, PANEL_TOKEN, EXIT_HOST, EXIT_PORT, EXIT_UUID,
EXIT_PBK, EXIT_SNI, EXIT_SID, EXIT_FLOW, EXIT_FP, EXIT_REMARK, DIRECT_FILE,
BACKUP_FILE, DOMAIN_STRATEGY.
EOF
}

# --------------------------------------------------------------- утилиты ------
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

json_ok() { # <ответ панели> → 0 если .success == true
    local v
    v="$(printf '%s' "$1" | jq -r '.success // false' 2>/dev/null || printf 'false')"
    [[ "$v" == "true" ]]
}

json_msg() { # <ответ панели> → .msg или заглушка
    printf '%s' "$1" | jq -r '.msg // "нет сообщения"' 2>/dev/null || printf 'не JSON'
}

# ------------------------------------------------------------ параметры -------
validate_params() {
    [[ -n "$EXIT_HOST" ]] || die "Обязателен --exit-host: адрес зарубежного выхода (IP или домен)."
    [[ -n "$EXIT_UUID" ]] || die "Обязателен --exit-uuid: UUID клиента на выходе (панель выхода → инбаунд → клиент)."
    [[ -n "$EXIT_PBK" ]]  || die "Обязателен --exit-pbk: publicKey Reality выходной ноды (install_node.sh печатает его в блоке «VLESS + REALITY»)."
    [[ -n "$EXIT_SNI" ]]  || die "Обязателен --exit-sni: serverName Reality выходной ноды (домен маскировки, он же SNI в инбаунде выхода)."

    [[ "$EXIT_PORT" =~ ^[0-9]+$ ]] || die "--exit-port должен быть числом, получено: ${EXIT_PORT}"
    [[ "$EXIT_PORT" -ge 1 && "$EXIT_PORT" -le 65535 ]] || die "--exit-port вне диапазона 1..65535: ${EXIT_PORT}"

    [[ "$EXIT_UUID" =~ $UUID_RE ]] \
        || die "--exit-uuid не похож на UUID (ожидается 8-4-4-4-12 hex): ${EXIT_UUID}. VLESS не примет другое значение."

    case "$EXIT_FLOW" in
        "" | xtls-rprx-vision | xtls-rprx-vision-udp443) : ;;
        *) die "--exit-flow «${EXIT_FLOW}» не поддерживается этим скриптом (ожидается xtls-rprx-vision, xtls-rprx-vision-udp443 или пусто)." ;;
    esac

    case "$EXIT_FP" in
        chrome | safari | ios | ios14 | ios15)
            warn "Фингерпринт '${EXIT_FP}' входит в чёрный список эвристики июня 2026."
            warn "Рекомендуется firefox, edge, android или randomized (как в install_node.sh)." ;;
    esac

    [[ -n "$EXIT_SID" ]] || warn "shortId (--exit-sid) не задан: поле в outbound не попадёт. Это сработает только если в инбаунде выхода shortIds содержит пустую строку; install_node.sh задаёт непустой shortId — тогда соединение не поднимется."

    case "$EXIT_PBK" in
        *[!A-Za-z0-9_=-]* | "") warn "publicKey «${EXIT_PBK}» выглядит необычно (обычно 43 символа base64url из install_node.sh) — проверь, что скопирован именно publicKey выхода." ;;
    esac
}

validate_domain_strategy() {
    # Сравнение без учёта регистра и дефиса: панель/Xray пишут «AsIs», человек —
    # «as-is»; значение всё равно нормализуется к форме Xray.
    local norm=""
    norm="$(printf '%s' "$DOMAIN_STRATEGY" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
    case "$norm" in
        ipifnonmatch) DOMAIN_STRATEGY="IPIfNonMatch" ;;
        ipondemand)   DOMAIN_STRATEGY="IPOnDemand" ;;
        asis)         DOMAIN_STRATEGY="AsIs" ;;
        *) die "--domain-strategy «${DOMAIN_STRATEGY}» не поддерживается (IPIfNonMatch | IPOnDemand | AsIs)." ;;
    esac
    if [[ "$DOMAIN_STRATEGY" == "AsIs" ]]; then
        warn "domainStrategy=AsIs: правило geoip:ru будет работать только для соединений, где клиент уже передал IP, а не домен. Для «РФ — напрямую» это заметно хуже."
    fi
}

# Встроенный список «напрямую». Порядок и состав — из ТЗ и docs §2.1/§5:
# РФ-сервисы, банки, Госуслуги, маркетплейсы + приватные адреса.
builtin_direct_list() {
    cat <<'EOF'
geosite:category-ru
geoip:ru
geoip:private
domain:gosuslugi.ru
domain:vk.com
domain:yandex.ru
domain:ozon.ru
domain:wildberries.ru
domain:sberbank.ru
domain:tbank.ru
EOF
}

read_direct_list() { # → DIRECT_TOKENS_JSON (jq-массив строк)
    local raw=""
    if [[ -n "$DIRECT_FILE" ]]; then
        [[ -f "$DIRECT_FILE" ]] || die "--direct-file: файл не найден: ${DIRECT_FILE}"
        [[ -r "$DIRECT_FILE" ]] || die "--direct-file: файл не читается: ${DIRECT_FILE}"
        raw="$(tr -d '\r' < "$DIRECT_FILE" \
            | sed -E 's/#.*$//' \
            | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
            | grep -v '^$' || true)"
        if [[ -z "$raw" ]]; then
            die "--direct-file ${DIRECT_FILE}: после удаления комментариев не осталось ни одной строки. Пустой список = весь трафик через выход, и подсеть выгорит на разрешённых РФ-сервисах."
        fi
    else
        raw="$(builtin_direct_list)"
    fi
    DIRECT_TOKENS_JSON="$(printf '%s\n' "$raw" | jq -R -s 'split("\n") | map(select(length > 0))')"
    DIRECT_COUNT="$(printf '%s' "$DIRECT_TOKENS_JSON" | jq 'length')"
}

inbound_tags_json() { # → jq-массив inboundTag (пусто = [])
    if [[ "${#INBOUND_TAGS[@]}" -eq 0 ]]; then
        printf '[]'
        return 0
    fi
    printf '%s\n' "${INBOUND_TAGS[@]}" | jq -R -s 'split("\n") | map(select(length > 0))'
}

# ------------------------------------------------------------- payload --------
# VLESS + Reality до зарубежного выхода. Поля ровно те, что читает клиентский
# слушатель Xray:
#   * vnext[0].users[0].flow = xtls-rprx-vision — как у клиентов на выходе
#     (install_node.sh создаёт их именно с этим flow); flow и mux несовместимы,
#     поэтому mux выключен явно, иначе Xray откажется стартовать;
#   * realitySettings: serverName (SNI маскировки выхода), publicKey (pbk),
#     shortId (sid), fingerprint, spiderX.
build_exit_outbound() {
    EXIT_OUTBOUND_JSON="$(jq -nc \
        --arg tag "$EXIT_REMARK" \
        --arg host "$EXIT_HOST" \
        --argjson port "$EXIT_PORT" \
        --arg uuid "$EXIT_UUID" \
        --arg flow "$EXIT_FLOW" \
        --arg sni "$EXIT_SNI" \
        --arg fp "$EXIT_FP" \
        --arg pbk "$EXIT_PBK" \
        --arg sid "$EXIT_SID" '
        {
          tag: $tag,
          protocol: "vless",
          settings: {
            vnext: [ {
              address: $host,
              port: $port,
              users: [ ({ id: $uuid, encryption: "none" }
                         + (if $flow == "" then {} else { flow: $flow } end)) ]
            } ]
          },
          streamSettings: {
            network: "tcp",
            security: "reality",
            realitySettings: (
              { serverName: $sni, fingerprint: $fp, publicKey: $pbk, spiderX: "/" }
              + (if $sid == "" then {} else { shortId: $sid } end)
            )
          },
          mux: { enabled: false }
        }')"
}

# Первое правило: разрешённые РФ-сервисы — напрямую со входа.
# jq сам делит строки списка на domain[] и ip[]: geosite:/domain:/full:/keyword:/
# regexp:/голый домен → domain; geoip:/CIDR/IP → ip.
build_direct_rule() {
    DIRECT_RULE_JSON="$(printf '%s' "$DIRECT_TOKENS_JSON" | jq -c \
        --arg rt "$DIRECT_RULE_TAG" \
        --arg ot "$DIRECT_OUTBOUND_TAG" '
        def ipish:
            startswith("geoip:")
            or test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}(/[0-9]{1,2})?$")
            or test("^[0-9a-fA-F:]*:[0-9a-fA-F:]*$")
            or test("^[0-9a-fA-F:.]+/[0-9]{1,3}$");
        . as $tokens
        | { type: "field", ruleTag: $rt, outboundTag: $ot }
          + (if ($tokens | map(select(ipish | not)) | length) > 0
             then { domain: ($tokens | map(select(ipish | not))) } else {} end)
          + (if ($tokens | map(select(ipish)) | length) > 0
             then { ip: ($tokens | map(select(ipish))) } else {} end)')"
}

# Последнее правило: всё, что не попало в direct (и в служебные правила панели),
# уходит на выход. --inbound-tag ограничивает правило конкретными инбаундами.
build_exit_rule() {
    EXIT_RULE_JSON="$(inbound_tags_json | jq -c \
        --arg rt "$EXIT_RULE_TAG" \
        --arg ot "$EXIT_REMARK" '
        { type: "field", ruleTag: $rt, outboundTag: $ot, network: "tcp,udp" }
        + (if length > 0 then { inboundTag: . } else {} end)')"
}

# ------------------------------------------------------------- слияние --------
# Правка шаблона. Печатает ТОЛЬКО JSON (никакого лога в stdout: результат
# забирается через $(...)).
merge_template() { # merge_template <template-json>
    local tpl="$1"
    printf '%s' "$tpl" | jq \
        --argjson exit_outbound "$EXIT_OUTBOUND_JSON" \
        --argjson direct_rule "$DIRECT_RULE_JSON" \
        --argjson exit_rule "$EXIT_RULE_JSON" \
        --arg tag "$EXIT_REMARK" \
        --arg drtag "$DIRECT_RULE_TAG" \
        --arg ertag "$EXIT_RULE_TAG" \
        --arg ds "$DOMAIN_STRATEGY" \
        --arg direct_outbound "$DIRECT_OUTBOUND_TAG" '
        . as $cfg
        | ($cfg.outbounds // []) as $obs
        | ($obs | map(select((.tag // "") != $tag))) as $kept
        | ($kept + [$exit_outbound]) as $with_exit
        | (if ($with_exit | any(.tag == $direct_outbound)) then $with_exit
           else $with_exit + [{ tag: $direct_outbound, protocol: "freedom", settings: {} }]
           end) as $outbounds
        | ($cfg.routing // {}) as $rt
        | ($rt.rules // []) as $rules
        | ($rules | map(select(((.ruleTag // "") != $drtag) and ((.ruleTag // "") != $ertag)))) as $own
        | ($own | map(select((((.inboundTag // []) | index("api")) != null) or ((.outboundTag // "") == "api")))) as $pinned
        | ($own | map(select((((.inboundTag // []) | index("api")) == null) and ((.outboundTag // "") != "api")))) as $rest
        | $cfg
        | .outbounds = $outbounds
        | .routing = ($rt + { domainStrategy: $ds, rules: ($pinned + [$direct_rule] + $rest + [$exit_rule]) })
        '
}

count_outbound_tag() { # <template> <тег> → сколько раз встречается
    printf '%s' "$1" | jq -r --arg tag "$2" '[.outbounds[]? | select((.tag // "") == $tag)] | length' 2>/dev/null || printf '0'
}

count_our_rules() { # <template> → сколько наших правил (по ruleTag)
    printf '%s' "$1" | jq -r --arg a "$DIRECT_RULE_TAG" --arg b "$EXIT_RULE_TAG" \
        '[.routing.rules[]? | select((.ruleTag // "") == $a or (.ruleTag // "") == $b)] | length' 2>/dev/null || printf '0'
}

# ------------------------------------------------------------------ diff ------
print_json_section() { # <заголовок> <json>
    printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"
    printf '%s' "$2" | jq . 2>/dev/null || printf '%s\n' "$2"
}

print_rules_summary() { # <template> <подпись>
    printf '%s' "$1" | jq -r --arg title "$2" '
        "  \($title): domainStrategy=\(.routing.domainStrategy // "—"), правил: \(.routing.rules | length)",
        ( [ .routing.rules[]?
            | "      - \(.ruleTag // "без ruleTag") → \(.outboundTag // .balancerTag // "?")"
              + (if (.inboundTag // []) | length > 0 then " [inboundTag: \((.inboundTag | join(",")))]" else "" end)
              + (if (.domain // []) | length > 0 then " domain: \((.domain | length)) шт." else "" end)
              + (if (.ip // []) | length > 0 then " ip: \((.ip | length)) шт." else "" end)
          ] | .[] )' 2>/dev/null || true
}

print_semantic_diff() { # <было> <стало>
    local before="$1" after="$2"
    printf '\n%s— outbounds и правила: было / стало%s\n' "$C_BOLD" "$C_OFF"
    printf '  outbounds было : %s\n' "$(printf '%s' "$before" | jq -r '[.outbounds[]?.tag // "?"] | join(", ")' 2>/dev/null || printf '—')"
    printf '  outbounds стало: %s\n' "$(printf '%s' "$after"  | jq -r '[.outbounds[]?.tag // "?"] | join(", ")' 2>/dev/null || printf '—')"
    print_rules_summary "$before" "правила было "
    print_rules_summary "$after"  "правила стало"
}

print_diff() { # <было> <стало> [подпись]
    local before="$1" after="$2" label="${3:-xrayTemplateConfig}"
    box "DIFF: БЫЛО → СТАЛО (${label})"
    if have diff; then
        local b a
        b="$(tmp_file)"; a="$(tmp_file)"
        printf '%s' "$before" | jq -S . > "$b" 2>/dev/null || printf '%s\n' "$before" > "$b"
        printf '%s' "$after"  | jq -S . > "$a" 2>/dev/null || printf '%s\n' "$after"  > "$a"
        diff -u -L "было" -L "стало" "$b" "$a" || true
        rm -f "$b" "$a"
    else
        warn "diff недоступен — печатаю изменения по секциям (outbounds / routing.rules)."
    fi
    print_semantic_diff "$before" "$after"
}

# Эталонный шаблон 3x-ui — только для dry-run: в dry-run конфиг панели не
# читается (сетевых запросов нет), поэтому «было» — это штатный шаблон
# (internal/web/service/config.json: outbounds direct/blocked, служебное
# правило inboundTag api → api, private → blocked, bittorrent → blocked).
reference_template() {
    cat <<'EOF'
{
  "log": { "loglevel": "warning" },
  "inbounds": [],
  "outbounds": [
    { "protocol": "freedom", "settings": {}, "tag": "direct" },
    { "protocol": "blackhole", "settings": {}, "tag": "blocked" }
  ],
  "routing": {
    "domainStrategy": "AsIs",
    "rules": [
      { "inboundTag": ["api"], "outboundTag": "api", "type": "field" },
      { "ip": ["geoip:private"], "outboundTag": "blocked", "type": "field" },
      { "outboundTag": "blocked", "protocol": ["bittorrent"], "type": "field" }
    ]
  }
}
EOF
}

# ------------------------------------------------------------------ API -------
# Чтение настроек: по ТЗ GET /panel/setting/all, но в живом 3x-ui этот
# эндпоинт объявлен как POST — поэтому есть аккуратный фолбэк.
fetch_settings() { # → SETTINGS_RESP
    local resp=""
    log "Читаю настройки панели: GET ${PANEL_URL}/panel/setting/all"
    resp="$(api_get /panel/setting/all)" \
        || die "Панель не отвечает по адресу ${PANEL_URL}. Проверь PANEL_URL, firewall и что служба x-ui запущена."
    if ! json_ok "$resp"; then
        warn "GET /panel/setting/all не дал успешного ответа: $(json_msg "$resp")"
        log "Повторяю чтение через POST /panel/setting/all: в 3x-ui этот эндпоинт объявлен как POST (GET понимают не все сборки)."
        resp="$(api_post /panel/setting/all '{}')" \
            || die "Панель не отвечает по адресу ${PANEL_URL} (POST /panel/setting/all)."
        json_ok "$resp" \
            || die "Панель ответила отказом: $(json_msg "$resp"). Проверь PANEL_TOKEN (скоуп admin)."
    fi
    ok "Настройки панели прочитаны."
    SETTINGS_RESP="$resp"
}

extract_template() { # <ответ настроек> → xrayTemplateConfig (JSON-строка) или пусто
    printf '%s' "$1" | jq -r '.obj.xrayTemplateConfig // empty' 2>/dev/null || true
}

template_missing() { # 0 — шаблона фактически нет
    local t="$1" squeezed=""
    [[ -z "$t" ]] && return 0
    squeezed="$(printf '%s' "$t" | tr -d '[:space:]')"
    case "$squeezed" in
        "" | "{}" | "null") return 0 ;;
    esac
    return 1
}

print_no_template_hint() {
    cat <<'EOF'

  ── Что делать, если Xray-шаблона нет ───────────────────────────────────
  1. Инбаунды на входной ноде ещё не созданы — запусти на входе:
       sudo bash scripts/install_node.sh --whitelist --sni <разрешённый домен>
     Он поднимает VLESS+Reality на 443 и заполняет xrayTemplateConfig панели —
     после этого повтори этот скрипт.
  2. Инбаунды есть, а шаблон пуст: панель → Настройки → Xray → вставить конфиг
     (или «Сбросить настройки Xray»), сохранить, затем повторить скрипт.
  3. Совсем новые сборки 3x-ui держат шаблон не в настройках, а в отдельном
     API: POST /panel/api/xray/ (чтение) и POST /panel/api/xray/update (запись).
     Здесь по ТЗ используется /panel/setting/all + /panel/setting/update; если
     у тебя такая сборка — возьми outbound и правила из вывода dry-run и примени
     их через /panel/api/xray/update.
EOF
}

# ------------------------------------------------------------------ запись ----
apply_cascade() {
    local template="" new_template="" payload="" resp="" had="" had_rules="" backup_dir=""

    fetch_settings
    template="$(extract_template "$SETTINGS_RESP")"

    if template_missing "$template"; then
        print_no_template_hint
        die "В панели нет Xray-шаблона: поле obj.xrayTemplateConfig пусто."
    fi

    printf '%s' "$template" | jq -e . >/dev/null 2>&1 \
        || die "obj.xrayTemplateConfig не разбирается как JSON. Панель должна хранить здесь JSON-строку; проверь шаблон в панели (Настройки → Xray)."

    had="$(count_outbound_tag "$template" "$EXIT_REMARK")"
    if [[ "$had" -gt 0 ]]; then
        ok "Outbound «${EXIT_REMARK}» уже есть в шаблоне (совпадений: ${had}) — обновляю его, не дублирую (идемпотентность)."
    else
        log "Outbound «${EXIT_REMARK}» в шаблоне не найден — добавляю."
    fi
    had_rules="$(count_our_rules "$template")"
    if [[ "$had_rules" -gt 0 ]]; then
        ok "Наши правила уже были (${had_rules}) — снимаю и ставлю заново в нужном порядке, дублей не будет."
    fi

    new_template="$(merge_template "$template")" \
        || die "Не удалось собрать новый шаблон (jq). Текущий конфиг не изменён."

    # --- обязательный бэкап ДО записи -------------------------------------
    backup_dir="$(dirname "$BACKUP_FILE")"
    if [[ ! -d "$backup_dir" ]]; then
        mkdir -p "$backup_dir" || die "Не удалось создать каталог для бэкапа: ${backup_dir}"
        log "Создан каталог для бэкапа: ${backup_dir}"
    fi
    printf '%s' "$template" | jq . > "$BACKUP_FILE" \
        || die "Не удалось записать бэкап в ${BACKUP_FILE}. Запись в панель отменена."
    chmod 600 "$BACKUP_FILE" 2>/dev/null || true
    ok "Бэкап текущего xrayTemplateConfig: ${BACKUP_FILE}"
    BACKUP_DONE=1

    print_diff "$template" "$new_template" "фактический конфиг панели"

    if ! printf '%s' "$new_template" | jq -e . >/dev/null 2>&1; then
        die "Собранный шаблон не является валидным JSON — запись отменена (бэкап: ${BACKUP_FILE})."
    fi

    # Полный объект настроек + подменённый шаблон: частичный объект панель
    # читает как «остальные поля пустые».
    payload="$(printf '%s' "$SETTINGS_RESP" | jq --arg tpl "$new_template" '.obj | .xrayTemplateConfig = $tpl')" \
        || die "Не удалось собрать тело запроса (jq)."

    log "Записываю шаблон: POST ${PANEL_URL}/panel/setting/update (xrayTemplateConfig, $(printf '%s' "$new_template" | wc -c | tr -d ' ') байт)"
    resp="$(api_post /panel/setting/update "$payload")" \
        || die "Панель не ответила на POST /panel/setting/update. Конфиг не изменён (или изменён частично — восстанови из ${BACKUP_FILE})."
    if ! json_ok "$resp"; then
        die "Панель отклонила обновление настроек: $(json_msg "$resp"). Бэкап: ${BACKUP_FILE}. Восстановление — см. блок «КАК ПРОВЕРИТЬ», п. 7."
    fi
    ok "Панель приняла новый xrayTemplateConfig."

    verify_written
}

verify_written() { # перечитать и подтвердить, что тег на месте
    local template="" got="" rules="" addr="" port=""
    log "Перечитываю конфиг из панели (проверка, что запись дошла)..."
    fetch_settings
    template="$(extract_template "$SETTINGS_RESP")"

    got="$(count_outbound_tag "$template" "$EXIT_REMARK")"
    if [[ -z "$got" || "$got" -lt 1 ]]; then
        err "Тег «${EXIT_REMARK}» не найден в конфиге после записи."
        err "Панель могла отклонить шаблон или перезаписать его своим."
        err "Откат: восстанови ${BACKUP_FILE} (блок «КАК ПРОВЕРИТЬ», п. 7) и проверь journalctl -u x-ui -n 50."
        exit 1
    fi
    rules="$(count_our_rules "$template")"
    addr="$(printf '%s' "$template" | jq -r --arg tag "$EXIT_REMARK" '[.outbounds[]? | select((.tag // "") == $tag) | .settings.vnext[0].address] | first // "?"' 2>/dev/null || printf '?')"
    port="$(printf '%s' "$template" | jq -r --arg tag "$EXIT_REMARK" '[.outbounds[]? | select((.tag // "") == $tag) | .settings.vnext[0].port] | first // "?"' 2>/dev/null || printf '?')"
    ok "Подтверждено: outbound «${EXIT_REMARK}» → ${addr}:${port} (совпадений: ${got})."
    ok "Наших правил в routing.rules: ${rules} (${DIRECT_RULE_TAG} + ${EXIT_RULE_TAG})."

    if [[ "$got" -gt 1 ]]; then
        warn "Outbound с тегом «${EXIT_REMARK}» больше одного (${got}) — это дубль: сними лишние в панели вручную."
    fi
    if [[ "$rules" -ne 2 ]]; then
        warn "Ожидалось два наших правила, найдено ${rules}: проверь порядок в панели (Настройки → Xray)."
    fi
}

# --------------------------------------------------------------- предпросмотр -
dry_run_preview() {
    local ref="" preview=""
    ref="$(reference_template)"
    preview="$(merge_template "$ref")" \
        || die "Не удалось собрать предпросмотр шаблона (jq)."

    print_diff "$ref" "$preview" "эталонный шаблон 3x-ui (конфиг панели в dry-run не читается)"

    box "ИДЕМПОТЕНТНОСТЬ"
    cat <<EOF
  Повторный запуск с --apply: тег «${EXIT_REMARK}» ищется в outbounds — если он
  есть, скрипт обновляет его, не дублирую второй; наши правила помечены ruleTag
  ${DIRECT_RULE_TAG} / ${EXIT_RULE_TAG}, снимаются и ставятся заново в правильном
  порядке. Второго outbound и вторых правил не появится.
EOF

    box "DRY-RUN: ЧТО БУДЕТ ЗАПИСАНО"
    cat <<EOF
  ${EXIT_COUNT_NOTE}
  Запись делается одним вызовом POST /panel/setting/update с полем
  xrayTemplateConfig (полный объект настроек, прочитанный из панели).
  Бэкап ${BACKUP_FILE} создаётся только с --apply и строго до записи.
EOF

    echo
    log "Сетевых запросов нет: ни GET /panel/setting/all, ни POST /panel/setting/update."
    log "Ничего не изменено. Для реальной записи добавь --apply."
}

# ----------------------------------------------------------------- сводка -----
print_summary() {
    local domains="" ips=""
    domains="$(printf '%s' "$DIRECT_TOKENS_JSON" | jq -r '
        def ipish: startswith("geoip:") or test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}(/[0-9]{1,2})?$")
          or test("^[0-9a-fA-F:]*:[0-9a-fA-F:]*$") or test("^[0-9a-fA-F:.]+/[0-9]{1,3}$");
        [.[] | select(ipish | not)] | length')"
    ips="$(printf '%s' "$DIRECT_TOKENS_JSON" | jq -r '
        def ipish: startswith("geoip:") or test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}(/[0-9]{1,2})?$")
          or test("^[0-9a-fA-F:]*:[0-9a-fA-F:]*$") or test("^[0-9a-fA-F:.]+/[0-9]{1,3}$");
        [.[] | select(ipish)] | length')"

    box "KOMETA • КАСКАД: ВХОД (РФ) → ЗАРУБЕЖНЫЙ ВЫХОД"
    printf '  панель входа : %s\n' "$PANEL_URL"
    printf '  выход        : %s:%s (VLESS+Reality, SNI %s, fp %s, flow %s)\n' \
        "$EXIT_HOST" "$EXIT_PORT" "$EXIT_SNI" "$EXIT_FP" "${EXIT_FLOW:-<без flow>}"
    printf '  тег outbound : %s\n' "$EXIT_REMARK"
    printf '  напрямую     : %s записей (домены: %s, ip: %s)%s\n' \
        "$DIRECT_COUNT" "$domains" "$ips" \
        "$( [[ -n "$DIRECT_FILE" ]] && printf ' из %s' "$DIRECT_FILE" || printf ' — встроенный список' )"
    printf '  domainStrategy: %s\n' "$DOMAIN_STRATEGY"
    printf '  правила      : сначала direct (РФ — напрямую), последним — %s\n' "$EXIT_REMARK"
    printf '  inboundTag   : %s\n' "$( [[ "${#INBOUND_TAGS[@]}" -gt 0 ]] && printf '%s' "$(inbound_tags_json | jq -r 'join(", ")')" || printf 'все инбаунды (правило-ловушка)' )"
    printf '  бэкап        : %s%s\n' "$BACKUP_FILE" "$( [[ "$APPLY" -eq 1 ]] && printf '' || printf ' (только с --apply)' )"
    printf '  режим        : %s\n' "$( [[ "$APPLY" -eq 1 ]] && printf 'APPLY (записываю в панель)' || printf 'DRY-RUN (ничего не отправляю)' )"
}

print_payload() {
    print_json_section "OUTBOUND «${EXIT_REMARK}» (уйдёт в outbounds xrayTemplateConfig)" "$EXIT_OUTBOUND_JSON"
    box "ПРАВИЛА МАРШРУТИЗАЦИИ (порядок в routing.rules)"
    cat <<EOF
  1) служебные правила панели (inboundTag: api → api) — остаются ВЫШЕ нашего
     direct: иначе служебный трафик панели попадёт под geoip:private и уедет
     в direct (панель теряет статистику);
  2) НАШЕ первое: direct — список «напрямую» (${DIRECT_COUNT} записей);
  3) прочие правила шаблона (blocked: bittorrent и т.п.) — сохраняются как были;
  4) НАШЕ последнее: всё остальное → ${EXIT_REMARK}$( [[ "${#INBOUND_TAGS[@]}" -gt 0 ]] && printf ' (только inboundTag: %s)' "$(inbound_tags_json | jq -r 'join(", ")')" || printf ' (все инбаунды)' ).
EOF
    print_json_section "ПРАВИЛО «НАПРЯМУЮ» (direct — РФ-сервисы не уходят в туннель)" "$DIRECT_RULE_JSON"
    print_json_section "ПРАВИЛО «НА ВЫХОД» (${EXIT_REMARK} — всё остальное)" "$EXIT_RULE_JSON"
}

print_warnings() {
    warn "Не гонять через вход разрешённые РФ-сервисы: подсеть выгорает именно на этом (docs/ОБХОД-БЕЛЫХ-СПИСКОВ-ПЛАН.md §5)."
    warn "Разделяй входной и выходной IP: Reality форвардит неавторизованные соединения на dest в обход routing rules — подтверждено экспериментом 30.04.2026 (в логах nginx-заглушки появился IP входной ноды)."
    warn "Ротация serverNames на инбаунде входа — против паттерна «IP ↔ один SNI»."
    if printf '%s' "$DIRECT_TOKENS_JSON" | jq -e 'index("geoip:private") != null' >/dev/null 2>&1; then
        warn "В списке «напрямую» есть geoip:private: наше правило стоит ВЫШЕ штатного правила 3x-ui «geoip:private → blocked», то есть приватные адреса станут доступны с входа, а не блокироваться. Нужно штатное поведение — убери строку geoip:private из --direct-file."
    fi
}

print_how_to_check() {
    box "КАК ПРОВЕРИТЬ (руками на входной ноде)"
    cat <<EOF
  1. Перезапустить ядро и посмотреть, что конфиг принят:
       x-ui restart
       journalctl -u x-ui -n 50 --no-pager
     В логе не должно быть «infra/conf: …» — это значит, что панель записала
     шаблон, а Xray его не принял (откат — п. 7).

  2. Что outbound и правила на месте (read-only):
       curl -s -H "Authorization: Bearer \$PANEL_TOKEN" \\
         ${PANEL_URL}/panel/setting/all \\
         | jq -r '.obj.xrayTemplateConfig' \\
         | jq '{outbounds: [.outbounds[].tag], rules: [.routing.rules[] | {ruleTag, outboundTag}]}'
     Ожидаем тег ${EXIT_REMARK} и два наших правила: ${DIRECT_RULE_TAG}, ${EXIT_RULE_TAG}.

  3. Клиентская ссылка обязана указывать на ВХОДНОЙ адрес, а не на выход:
       у подписки в vless://…@<АДРЕС_ВХОДА>:443 … должен стоять IP/домен ВХОДА.
     Если в ссылке адрес выхода — в режиме БС клиент пойдёт напрямую на
     зарубежный IP и не подключится.

  4. Что «РФ — напрямую» реально работает:
       * Госуслуги/банк/маркетплейс открываются с включённым туннелем;
       * в логах Xray входа нет обращений клиента к этим доменам;
       * внешний IP при этом — адрес ВЫХОДА:  curl -s https://api.ipify.org
  5. НЕ гонять через вход разрешённые РФ-сервисы и держать лимит клиентов на
     адрес: подсеть выгорает именно на «лишнем» трафике (docs/ОБХОД-БЕЛЫХ-СПИСКОВ-ПЛАН.md §5).
  6. Разделять входной и выходной IP + ротация serverNames: Reality утекает
     dest-IP входа (подтверждено 30.04.2026).
EOF
    if [[ "${BACKUP_DONE:-0}" -eq 1 ]]; then
        cat <<EOF
  7. Откат (бэкап снят до записи, файл ${BACKUP_FILE}):
       curl -s -H "Authorization: Bearer \$PANEL_TOKEN" ${PANEL_URL}/panel/setting/all \\
         | jq --rawfile tpl '${BACKUP_FILE}' '.obj | .xrayTemplateConfig = \$tpl' > /tmp/restore.json
       curl -s -X POST -H "Authorization: Bearer \$PANEL_TOKEN" \\
         -H 'Content-Type: application/json' --data-binary @/tmp/restore.json \\
         ${PANEL_URL}/panel/setting/update | jq .
       x-ui restart
EOF
    else
        cat <<EOF
  7. Откат: с --apply скрипт сам снимает бэкап в ${BACKUP_FILE} и печатает
     готовую команду восстановления.
EOF
    fi
}

# ---------------------------------------------------------------- main --------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --exit-host)       EXIT_HOST="${2:?}"; shift 2 ;;
            --exit-port)       EXIT_PORT="${2:?}"; shift 2 ;;
            --exit-uuid)       EXIT_UUID="${2:?}"; shift 2 ;;
            --exit-pbk)        EXIT_PBK="${2:?}"; shift 2 ;;
            --exit-sni)        EXIT_SNI="${2:?}"; shift 2 ;;
            --exit-sid)
                # Пустая строка — осознанное «без shortId»: ${2:?} её не пропустит.
                [[ $# -ge 2 ]] || die "--exit-sid требует значение (пустая строка = не писать shortId)."
                EXIT_SID="$2"; shift 2 ;;
            --exit-flow)
                # Пустая строка — осознанное «без flow» (Reality без Vision).
                [[ $# -ge 2 ]] || die "--exit-flow требует значение (пустая строка = без flow)."
                EXIT_FLOW="$2"; shift 2 ;;
            --exit-fp)         EXIT_FP="${2:?}"; shift 2 ;;
            --exit-remark)     EXIT_REMARK="${2:?}"; shift 2 ;;
            --direct-file)     DIRECT_FILE="${2:?}"; shift 2 ;;
            --backup-file)     BACKUP_FILE="${2:?}"; shift 2 ;;
            --inbound-tag)     INBOUND_TAGS+=("${2:?}"); shift 2 ;;
            --domain-strategy) DOMAIN_STRATEGY="${2:?}"; shift 2 ;;
            --panel-url)       PANEL_URL="${2:?}"; shift 2 ;;
            --panel-token)     PANEL_TOKEN="${2:?}"; shift 2 ;;
            --insecure)        CURL_OPTS+=(--insecure); shift ;;
            --apply)           APPLY=1; shift ;;
            --dry-run)         APPLY=0; shift ;;
            -h | --help)       usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    have jq || die "Нужен jq (apt install jq / brew install jq)."
    require_panel
    validate_params
    validate_domain_strategy
    read_direct_list
    build_exit_outbound
    build_direct_rule
    build_exit_rule

    BACKUP_FILE="${BACKUP_FILE:-./backup-xray-$(date +%Y%m%d-%H%M%S).json}"
    BACKUP_DONE=0
    EXIT_COUNT_NOTE="outbound «${EXIT_REMARK}» → ${EXIT_HOST}:${EXIT_PORT} + 2 правила (${DIRECT_RULE_TAG}, ${EXIT_RULE_TAG})"

    print_summary
    print_payload
    print_warnings

    if [[ "$APPLY" -eq 1 ]]; then
        have curl || die "Нужен curl для --apply."
        apply_cascade
    else
        dry_run_preview
    fi

    print_how_to_check
}

main "$@"
