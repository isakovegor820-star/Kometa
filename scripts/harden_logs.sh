#!/usr/bin/env bash
# =============================================================================
#  Kometa — гигиена логов на узле 3x-ui: не храним историю посещений и DNS
# =============================================================================
#  Зачем. В политике конфиденциальности (docs/legal/ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ.md
#  §2.3) заявлено: «не собираем и не храним историю посещённых сайтов и
#  DNS-запросы». Сейчас это заявление конфигом НЕ обеспечено
#  (docs/СХЕМА-КОНКУРЕНТА-ЧТО-НУЖНО.md §4):
#    * Xray по умолчанию может писать access-лог — это и есть история
#      посещений (домены/IP и время по каждому соединению);
#    * в шаблоне Xray не выключен DNS-лог (`dnsLog`);
#    * sniffing включён с `routeOnly: false` — сервер разбирает SNI/Host и
#      подменяет адрес назначения, а разобранный домен оседает в логах;
#    * панель 3x-ui пишет свои служебные логи отдельно от Xray.
#
#  Правило проекта: СНАЧАЛА конфиг, ПОТОМ обещание. Обратный порядок — это
#  обещание, которое опровергается одним `ls` на нашем же сервере.
#
#  Что делает ЭТОТ скрипт.
#    1. Логи Xray в глобальном шаблоне панели (`xrayTemplateConfig`) —
#       секция `log` приводится к виду:
#           "access": "none"      — access-лог не пишется вообще;
#           "loglevel": "warning" — ошибки остаются, отладка (debug/info) нет;
#           "dnsLog": false       — DNS-запросы не логируются;
#           "error": ""           — пустой путь = stderr/systemd journal.
#       Если секции `log` нет — она создаётся; прочие её поля сохраняются.
#       Если уже так — печатается «уже настроено» и запись НЕ делается.
#    2. Sniffing на инбаундах (только с `--fix-sniffing`) — для всех
#       VLESS-инбаундов выставляется `sniffing.routeOnly = true`: домен нужен
#       только для маршрутизации, адрес назначения им не подменяется, и в логи
#       (если бы они были) попадает меньше. Уже `routeOnly: true` — пропуск.
#    3. Служебные логи панели — скрипт НЕ выдумывает флаги: он читает справку
#       `x-ui setting -h` (или `x-ui help`) и печатает только те строки про
#       логи, которые реально есть в этой сборке. Нет ключей/нет бинаря —
#       честная подсказка «проверь вручную» и что смотреть в UI панели.
#    4. Памятка «КАК ПРОВЕРИТЬ» — рестарт, journalctl, каталоги логов, порты и
#       как убедиться, что access-лог Xray больше не растёт.
#
#  Чего скрипт НЕ делает: не удаляет уже накопленные файлы логов (он про них
#  только предупреждает — это история посещений, её надо снести руками),
#  не трогает клиентов и статистику трафика (учёт ГБ — не история посещений),
#  не ходит в сеть без `--apply`.
#
#  Про API панели (те же приёмы, что в scripts/add_cascade_exit.sh).
#  Чтение настроек — GET /panel/setting/all (в живом 3x-ui эндпоинт объявлен
#  как POST, поэтому есть фолбэк), поле `obj.xrayTemplateConfig` — JSON-СТРОКА.
#  Запись — POST /panel/setting/update ПОЛНЫМ объектом настроек (прочитанный
#  объект + подменённый xrayTemplateConfig): частичный объект панель трактует
#  как «остальные поля пустые» и может обнулить чужие настройки.
#  Инбаунды — GET /panel/api/inbounds/list и POST /panel/api/inbounds/update/{id}.
#  Обязательный бэкап делается СТРОГО до записи.
#
#  Идемпотентность. Повторный запуск не пишет то, что уже настроено: секция
#  log сравнивается с целевой по четырём ключам, а `sniffing.routeOnly`
#  проверяется у каждого инбаунда. Уже настроено — «уже настроено», без записи.
#
#  Примеры:
#     # 1. Dry-run (по умолчанию): печатает diff и НЕ ходит в панель вообще.
#     PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... ./scripts/harden_logs.sh
#
#     # 2. Только логи Xray (без инбаундов) — как в п.1.
#     PANEL_URL=... PANEL_TOKEN=... ./scripts/harden_logs.sh --apply
#
#     # 3. Логи Xray + sniffing на всех VLESS-инбаундах.
#     PANEL_URL=... PANEL_TOKEN=... ./scripts/harden_logs.sh --fix-sniffing --apply
#
#     # 4. Только sniffing, шаблон логов не читать и не менять.
#     PANEL_URL=... PANEL_TOKEN=... ./scripts/harden_logs.sh --only-sniffing --apply
#
#     # 5. Ограничить правку sniffing одним инбаундом (по remark).
#     PANEL_URL=... PANEL_TOKEN=... ./scripts/harden_logs.sh \
#       --fix-sniffing --inbound-tag Kometa-Reality-443 --apply
#
#  Зависимости: bash, jq (всегда), curl (только с --apply). diff — опционально
#  (без него печатается разбор по секциям и по инбаундам).
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------- параметры -------
PANEL_URL="${PANEL_URL:-}"
PANEL_TOKEN="${PANEL_TOKEN:-}"
BACKUP_FILE="${BACKUP_FILE:-}"
INBOUNDS_BACKUP_FILE="${INBOUNDS_BACKUP_FILE:-}"
# Бинарь панели (для справки по её служебным логам; локально на ноде).
XUI_BIN="${XUI_BIN:-/usr/local/x-ui/x-ui}"

APPLY=0
FIX_SNIFFING=0
ONLY_SNIFFING=0
CURL_OPTS=()
INBOUND_TAGS=()

# Целевая секция log. Значения — из docs/СХЕМА-КОНКУРЕНТА-ЧТО-НУЖНО.md §4:
# access=none (не писать журнал доступа), loglevel=warning (ошибки — да,
# отладка — нет), dnsLog=false (DNS-запросы не логировать), error="" (пустой
# путь = stderr/systemd journal, отдельный файл ошибок не создаётся).
TARGET_ACCESS="none"
TARGET_LOGLEVEL="warning"
TARGET_DNSLOG="false"
TARGET_ERROR=""

# Что ищем в справке панели. Это НЕ команда и НЕ список флагов, которые скрипт
# станет выполнять: это regexp для grep по выводу `x-ui setting -h`. Печатаем
# только реально найденные строки — выдуманных флагов панели здесь нет.
PANEL_LOG_HINT_RE='(^|[[:space:]])(-logLevel|-logPath|-logFile|-log|loglevel|log level|log folder|log path|log dir)'

# --------------------------------------------------------------- вывод --------
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

# Временный файл без обязательной зависимости от mktemp.
tmp_file() {
    mktemp 2>/dev/null || printf '%s/kometa-logs.%s.%s' "${TMPDIR:-/tmp}" "$$" "${RANDOM:-0}"
}

usage() {
    cat <<'EOF'
Гигиена логов на узле 3x-ui: выключить access- и DNS-лог Xray, перевести
sniffing в routeOnly — чтобы заявление «не храним историю посещений и
DNS-запросы» было обеспечено конфигом, а не только текстом политики.

По умолчанию — DRY-RUN: скрипт печатает целевую секцию log, diff и план по
инбаундам, но в панель НЕ ходит (ни одного сетевого запроса) и ничего не
меняет. Запись — только с явным --apply и только после обязательного бэкапа.

Использование:
  PANEL_URL=https://127.0.0.1:2053 PANEL_TOKEN=... \
    ./scripts/harden_logs.sh [--fix-sniffing] [--apply]

Обязательно (для любого режима, кроме --help):
  PANEL_URL          адрес панели узла (env или --panel-url)
  PANEL_TOKEN        API-токен панели, скоуп admin (env или --panel-token)

Что правится:
  (по умолчанию)     секция log в xrayTemplateConfig панели:
                     access=none, loglevel=warning, dnsLog=false, error=""
                     (пустой error = stderr/systemd journal)
  --fix-sniffing     плюс sniffing.routeOnly=true у всех VLESS-инбаундов
                     (домен нужен только для маршрутизации и не подменяет
                     адрес назначения). Уже routeOnly:true — пропуск
  --only-sniffing    секцию log НЕ читать и НЕ менять: только sniffing
                     (подразумевает --fix-sniffing)
  --inbound-tag TAG  ограничить правку sniffing инбаундами с этим remark
                     (можно повторять). По умолчанию — все VLESS-инбаунды

Запись:
  --backup-file FILE путь бэкапа текущего xrayTemplateConfig
                     (по умолчанию ./backup-xray-logs-<дата-время>.json)
  --inbounds-backup-file FILE
                     путь бэкапа списка инбаундов до правки sniffing
                     (по умолчанию ./backup-inbounds-sniffing-<дата-время>.json)
  --apply            реально записать (без него — только показать)
  --dry-run          явный dry-run (то же поведение, что и по умолчанию)
  --insecure         не проверять TLS-сертификат панели (самоподписанный)
  --panel-url URL    адрес панели (альтернатива PANEL_URL)
  --panel-token T    токен панели (альтернатива PANEL_TOKEN)
  -h, --help         эта справка

Служебные логи САМОЙ панели 3x-ui этим шаблоном не выключаются: скрипт читает
справку `x-ui setting -h` и печатает только те ключи, что реально есть в твоей
сборке. Если ключей нет — он честно скажет «проверь вручную» и что смотреть в
UI панели. Выдуманных флагов скрипт не выполняет.

Переменные окружения: PANEL_URL, PANEL_TOKEN, BACKUP_FILE,
INBOUNDS_BACKUP_FILE, XUI_BIN.
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

canon() { # <json> → канонический вид (сортировка ключей, одна строка)
    printf '%s' "$1" | jq -S -c . 2>/dev/null || printf '%s' "$1"
}

# ------------------------------------------------------------ целевая log -----
build_log_section() { # → целевая секция log (jq-объект)
    jq -nc --arg access "$TARGET_ACCESS" \
           --arg loglevel "$TARGET_LOGLEVEL" \
           --argjson dnslog "$TARGET_DNSLOG" \
           --arg error "$TARGET_ERROR" \
        '{access: $access, loglevel: $loglevel, dnsLog: $dnslog, error: $error}'
}

fix_log_section() { # fix_log_section <template-json> <целевая секция> → шаблон
    local tpl="$1" section="$2"
    printf '%s' "$tpl" | jq --argjson log "$section" '
        . as $cfg
        | ($cfg.log // {}) as $old
        | $cfg + { log: ($old + $log) }'
}

# Значение ключа секции log «как есть»: `//` тут не годится — в jq он считает
# пустыми и null, и false, а dnsLog=false обязан сравниться именно с false.
log_value_of() { # <template> <ключ> → jq-значение или null, если ключа нет
    printf '%s' "$1" | jq -c --arg k "$2" \
        '(.log // {}) as $l | if ($l | has($k)) then $l[$k] else null end' 2>/dev/null || printf 'null'
}

log_section_matches() { # <template> → 0 если все четыре ключа уже целевые
    local tpl="$1" got=""
    got="$(printf '%s' "$tpl" | jq -c --argjson want "$LOG_SECTION" '
        (.log // {}) as $l
        | { access:   ($l | if has("access")   then .access   else null end),
            loglevel: ($l | if has("loglevel") then .loglevel else null end),
            dnsLog:   ($l | if has("dnsLog")   then .dnsLog   else null end),
            error:    ($l | if has("error")    then .error    else null end) }
        | . == $want' 2>/dev/null || printf 'false')"
    [[ "$got" == "true" ]]
}

print_log_state() { # <template> — что сейчас в секции log и что станет
    local tpl="$1" key cur want mark
    printf '  %-9s %-28s %-22s %s\n' "ключ" "сейчас" "нужно" "статус"
    for key in access loglevel dnsLog error; do
        cur="$(log_value_of "$tpl" "$key")"
        want="$(printf '%s' "$LOG_SECTION" | jq -c --arg k "$key" '.[$k]' 2>/dev/null || printf '?')"
        if [[ "$cur" == "$want" ]]; then mark="уже так"; else mark="→ будет изменено"; fi
        printf '  %-9s %-28s %-22s %s\n' "$key" "$cur" "$want" "$mark"
    done
}

# ------------------------------------------------------------------ diff ------
print_json_section() { # <заголовок> <json>
    printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"
    printf '%s' "$2" | jq . 2>/dev/null || printf '%s\n' "$2"
}

print_semantic_diff() { # <было> <стало>
    local before="$1" after="$2"
    printf '\n%s— секция log: было / стало%s\n' "$C_BOLD" "$C_OFF"
    printf '  было : %s\n' "$(printf '%s' "$before" | jq -c '.log // {}' 2>/dev/null || printf '—')"
    printf '  стало: %s\n' "$(printf '%s' "$after"  | jq -c '.log // {}' 2>/dev/null || printf '—')"
    printf '  access-лог Xray: %s\n' "$(printf '%s' "$after" | jq -r 'if (.log.access // "") == "none" then "выключен (none)" else "ПИШЕТСЯ: \(.log.access // "по умолчанию")" end' 2>/dev/null || printf '—')"
    printf '  DNS-лог Xray   : %s\n' "$(printf '%s' "$after" | jq -r 'if (.log.dnsLog // false) == false then "выключен (false)" else "ВКЛЮЧЁН" end' 2>/dev/null || printf '—')"
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
        warn "diff недоступен — печатаю изменения по секции log."
    fi
    print_semantic_diff "$before" "$after"
}

# Эталонный шаблон 3x-ui — только для dry-run: конфиг панели в dry-run не
# читается (сетевых запросов нет), поэтому «было» — это штатный шаблон
# (internal/web/service/config.json: log только с loglevel, outbounds
# direct/blocked, служебное правило inboundTag api → api).
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
# Чтение настроек: по ТЗ GET /panel/setting/all, но в живом 3x-ui этот эндпоинт
# объявлен как POST (internal/web/controller/setting.go) — поэтому фолбэк.
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
  1. Инбаунды на ноде ещё не созданы — запусти на ноде:
       sudo bash scripts/install_node.sh
     Он поднимает VLESS+Reality и заполняет xrayTemplateConfig панели —
     после этого повтори этот скрипт.
  2. Инбаунды есть, а шаблон пуст: панель → Настройки → Xray → вставить
     конфиг (или «Сбросить настройки Xray»), сохранить, затем повторить.
  3. Совсем новые сборки 3x-ui держат шаблон не в настройках, а в отдельном
     API: /panel/api/xray/ (чтение) и /panel/api/xray/update (запись). Здесь
     по ТЗ используется /panel/setting/all + /panel/setting/update; если у
     тебя такая сборка — возьми секцию log из dry-run и впиши её в шаблон
     через UI панели (Настройки → Xray).
EOF
}

# ------------------------------------------------------- правка шаблона -------
apply_logs() { # --apply: бэкап → POST /panel/setting/update → проверка
    local template="" new_template="" payload="" resp="" backup_dir="" size=""

    fetch_settings
    template="$(extract_template "$SETTINGS_RESP")"

    if template_missing "$template"; then
        print_no_template_hint
        die "В панели нет Xray-шаблона: поле obj.xrayTemplateConfig пусто."
    fi

    printf '%s' "$template" | jq -e . >/dev/null 2>&1 \
        || die "obj.xrayTemplateConfig не разбирается как JSON. Панель должна хранить здесь JSON-строку; проверь шаблон в панели (Настройки → Xray)."

    box "СЕКЦИЯ log В ШАБЛОНЕ ПАНЕЛИ: СЕЙЧАС / НУЖНО"
    print_log_state "$template"

    if log_section_matches "$template"; then
        ok "Секция log: уже настроено (access=none, loglevel=warning, dnsLog=false, error=\"\") — шаблон не перезаписываю (идемпотентность)."
        LOG_STATE="уже настроено"
        return 0
    fi

    new_template="$(fix_log_section "$template" "$LOG_SECTION")" \
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

    printf '%s' "$new_template" | jq -e . >/dev/null 2>&1 \
        || die "Собранный шаблон не является валидным JSON — запись отменена (бэкап: ${BACKUP_FILE})."

    # Полный объект настроек + подменённый шаблон: частичный объект панель
    # читает как «остальные поля пустые».
    payload="$(printf '%s' "$SETTINGS_RESP" | jq --arg tpl "$new_template" '.obj | .xrayTemplateConfig = $tpl')" \
        || die "Не удалось собрать тело запроса (jq)."

    size="$(printf '%s' "$new_template" | wc -c | tr -d ' ')"
    log "Записываю шаблон: POST ${PANEL_URL}/panel/setting/update (xrayTemplateConfig, ${size} байт)"
    resp="$(api_post /panel/setting/update "$payload")" \
        || fail_after_write "Панель не ответила на POST /panel/setting/update. Конфиг не изменён (или изменён частично — откати из ${BACKUP_FILE}: команда ниже)."
    if ! json_ok "$resp"; then
        fail_after_write "Панель отклонила обновление настроек: $(json_msg "$resp"). Бэкап: ${BACKUP_FILE}."
    fi
    ok "Панель приняла новый xrayTemplateConfig."
    LOG_STATE="изменён"

    verify_log_template
}

# Откат печатается СРАЗУ при ошибке: «см. блок КАК ПРОВЕРИТЬ» не годится —
# до этого блока выполнение уже не дойдёт, а откатывать надо немедленно.
print_rollback_now() {
    box "ОТКАТ: ВЕРНУТЬ ПРЕЖНИЙ xrayTemplateConfig (${BACKUP_FILE})"
    cat <<EOF
      curl -s -H "Authorization: Bearer \$PANEL_TOKEN" ${PANEL_URL}/panel/setting/all \\
        | jq --rawfile tpl '${BACKUP_FILE}' '.obj | .xrayTemplateConfig = \$tpl' > /tmp/restore.json
      curl -s -X POST -H "Authorization: Bearer \$PANEL_TOKEN" \\
        -H 'Content-Type: application/json' --data-binary @/tmp/restore.json \\
        ${PANEL_URL}/panel/setting/update | jq .
      systemctl restart x-ui
EOF
    err "Проверь журнал после отката: journalctl -u x-ui -n 50 --no-pager"
}

print_inbounds_rollback_now() {
    box "ОТКАТ SNIFFING: ВЕРНУТЬ ИНБАУНДЫ ИЗ ${INBOUNDS_BACKUP_FILE}"
    cat <<EOF
  Для каждого правленого инбаунда (id=<ID>):
      curl -s -X POST -H "Authorization: Bearer \$PANEL_TOKEN" \\
        -H 'Content-Type: application/json' \\
        --data-binary "\$(jq -c '.obj[] | select(.id==<ID>) | del(.clientStats)' ${INBOUNDS_BACKUP_FILE})" \\
        ${PANEL_URL}/panel/api/inbounds/update/<ID> | jq .
      systemctl restart x-ui
EOF
}

fail_after_write() { # <строка…> — сообщить об ошибке, напечатать откат и выйти
    local line=""
    for line in "$@"; do
        err "$line"
    done
    if [[ "${BACKUP_DONE:-0}" -eq 1 ]]; then
        print_rollback_now
    fi
    if [[ "${INBOUNDS_BACKUP_DONE:-0}" -eq 1 ]]; then
        print_inbounds_rollback_now
    fi
    exit 1
}

verify_log_template() { # перечитать и подтвердить секцию log
    local template="" got=""
    log "Перечитываю конфиг из панели (проверка, что запись дошла)..."
    fetch_settings
    template="$(extract_template "$SETTINGS_RESP")"

    if log_section_matches "$template"; then
        ok 'Подтверждено: log.access="none", log.loglevel="warning", log.dnsLog=false, log.error="" — access- и DNS-лог Xray выключены.'
        return 0
    fi
    got="$(printf '%s' "$template" | jq -c '.log // {}' 2>/dev/null || printf '?')"
    fail_after_write \
        "После записи секция log не совпадает с целевой: ${got}" \
        "Панель могла отклонить шаблон или перезаписать его своим." \
        "Откат: восстанови ${BACKUP_FILE} и проверь journalctl -u x-ui -n 50."
}

# ----------------------------------------------------------------- sniffing ---
sniffing_of() { # <inbound-json> → sniffing как объект (строка JSON или объект)
    printf '%s' "$1" | jq -c '
        (.sniffing // null) as $s
        | (if ($s | type) == "string" then (try ($s | fromjson) catch null) else $s end) as $p
        | (if ($p | type) == "object" then $p else {} end)'
}

build_inbound_update() { # <inbound-json> <новый sniffing-объект> → payload update
    # settings/streamSettings/sniffing отдаём как есть; тип sniffing сохраняем
    # таким, каким его вернула панель (в 3x-ui это JSON-строка, в старых
    # сборках — вложенный объект). clientStats (статистика по клиентам) в
    # теле update не нужен — это read-only поле ответа list.
    printf '%s' "$1" | jq -c --argjson sn "$2" '
        del(.clientStats)
        | if ((.sniffing // null) | type) == "object"
          then .sniffing = $sn
          else .sniffing = ($sn | tojson) end'
}

inbound_selected() { # <remark> → 0 если инбаунд проходит фильтр --inbound-tag
    local remark="$1" t=""
    if [[ "${#INBOUND_TAGS[@]}" -eq 0 ]]; then
        return 0
    fi
    for t in ${INBOUND_TAGS[@]+"${INBOUND_TAGS[@]}"}; do
        if [[ "$remark" == "$t" ]]; then
            return 0
        fi
    done
    return 1
}

fetch_inbounds() { # → INBOUNDS_RESP / INBOUNDS_OBJ
    local resp=""
    log "Читаю инбаунды: GET ${PANEL_URL}/panel/api/inbounds/list"
    resp="$(api_get /panel/api/inbounds/list)" \
        || die "Панель не отвечает по адресу ${PANEL_URL} (GET /panel/api/inbounds/list)."
    json_ok "$resp" \
        || die "Панель ответила отказом на список инбаундов: $(json_msg "$resp"). Проверь PANEL_TOKEN (скоуп admin)."
    INBOUNDS_RESP="$resp"
    INBOUNDS_OBJ="$(printf '%s' "$resp" | jq -c '.obj // []')"
    ok "Инбаундов получено: $(printf '%s' "$INBOUNDS_OBJ" | jq 'length')."
}

# Разбор списка инбаундов: кто подлежит правке, кто пропускается и почему.
# Заполняет PLAN_IDS / PLAN_PAYLOADS и счётчики пропусков.
plan_inbounds() { # <inbounds-json-array>
    local arr="$1" ib="" id="" remark="" proto="" port="" sn="" route="" payload="" line=""
    PLAN_IDS=(); PLAN_PAYLOADS=()
    SKIP_NOT_VLESS=0; SKIP_FILTERED=0; SKIP_ALREADY=0

    while IFS= read -r ib; do
        [[ -n "$ib" ]] || continue
        id="$(printf '%s' "$ib" | jq -r '.id // "?"')"
        remark="$(printf '%s' "$ib" | jq -r '.remark // ""')"
        proto="$(printf '%s' "$ib" | jq -r '.protocol // ""')"
        port="$(printf '%s' "$ib" | jq -r '.port // "?"')"

        if [[ "$proto" != "vless" ]]; then
            SKIP_NOT_VLESS=$((SKIP_NOT_VLESS + 1))
            printf '  id=%-5s %-32s протокол %s — пропуск (не VLESS)\n' "$id" "${remark:-<без remark>}" "${proto:-?}"
            continue
        fi
        if ! inbound_selected "$remark"; then
            SKIP_FILTERED=$((SKIP_FILTERED + 1))
            printf '  id=%-5s %-32s пропуск: не подходит под --inbound-tag\n' "$id" "${remark:-<без remark>}"
            continue
        fi

        sn="$(sniffing_of "$ib")"
        route="$(printf '%s' "$sn" | jq -r 'if .routeOnly == true then "true" else "false" end')"
        if [[ "$route" == "true" ]]; then
            SKIP_ALREADY=$((SKIP_ALREADY + 1))
            printf '  id=%-5s %-32s routeOnly уже true — пропуск (идемпотентность)\n' "$id" "${remark:-<без remark>}"
            continue
        fi

        payload="$(build_inbound_update "$ib" "$(printf '%s' "$sn" | jq -c '.routeOnly = true')")"
        PLAN_IDS+=("$id")
        PLAN_PAYLOADS+=("$payload")
        line="$(printf '  id=%-5s %-32s %s:%s  sniffing.routeOnly: %s → true' \
            "$id" "${remark:-<без remark>}" "$proto" "$port" "$route")"
        if [[ "$(printf '%s' "$sn" | jq -r '.enabled // false')" != "true" ]]; then
            line="${line}  (sniffing.enabled не true — разбор трафика и так выключен)"
        fi
        printf '%s\n' "$line"
    done < <(printf '%s' "$arr" | jq -c '.[]?')
}

reference_inbounds() { # эталон «как создаёт install_node.sh» — только для dry-run
    cat <<'EOF'
{
  "success": true,
  "obj": [
    {
      "id": 1,
      "remark": "Kometa-Reality-443",
      "protocol": "vless",
      "port": 443,
      "sniffing": "{\"enabled\":true,\"destOverride\":[\"http\",\"tls\",\"quic\"],\"metadataOnly\":false,\"routeOnly\":false}"
    }
  ]
}
EOF
}

print_sniffing_preview() { # dry-run: показать, что именно станет с sniffing
    local ref="" ib="" before="" after=""
    box "SNIFFING (--fix-sniffing): ЧТО БУДЕТ ИЗМЕНЕНО"
    ref="$(reference_inbounds)"
    while IFS= read -r ib; do
        [[ -n "$ib" ]] || continue
        before="$(sniffing_of "$ib")"
        after="$(printf '%s' "$before" | jq -c '.routeOnly = true')"
        printf '  эталонный инбаунд 3x-ui: id=%s remark=%s (vless:%s)\n' \
            "$(printf '%s' "$ib" | jq -r '.id')" \
            "$(printf '%s' "$ib" | jq -r '.remark // "?"')" \
            "$(printf '%s' "$ib" | jq -r '.port // "?"')"
        printf '    sniffing было : %s\n' "$before"
        printf '    sniffing стало: %s\n' "$after"
        if have diff; then
            printf '    diff: -"routeOnly":false  +"routeOnly":true (адрес назначения не подменяется доменом)\n'
        fi
    done < <(printf '%s' "$ref" | jq -c '.obj[]?')
    cat <<'EOF'

  В --apply список читается из панели (GET /panel/api/inbounds/list), правка —
  POST /panel/api/inbounds/update/{id}: только protocol=vless, у остальных
  (AmneziaWG, MTProto, trojan) sniffing не трогаем. Уже routeOnly:true —
  пропуск, повторный запуск ничего не пишет (идемпотентность).
  Перед первой правкой снимается бэкап списка инбаундов — откат по id.
EOF
}

apply_sniffing() { # --apply: бэкап списка → update по каждому → проверка
    local i=0 id="" payload="" resp="" backup_dir="" n=0
    fetch_inbounds

    box "SNIFFING: ПЛАН ПРАВОК (VLESS-инбаунды)"
    plan_inbounds "$INBOUNDS_OBJ"
    n="${#PLAN_IDS[@]}"

    if [[ "$n" -eq 0 ]]; then
        ok "Правок нет: у всех подходящих VLESS-инбаундов sniffing.routeOnly уже true (пропущено: ${SKIP_ALREADY}, не VLESS: ${SKIP_NOT_VLESS}, отфильтровано: ${SKIP_FILTERED})."
        if [[ "$SKIP_FILTERED" -gt 0 && "${#INBOUND_TAGS[@]}" -gt 0 ]]; then
            warn "Фильтр --inbound-tag отсеял ${SKIP_FILTERED} инбаунд(ов): проверь, что remark указан точно (панель → инбаунды)."
        fi
        SNIFF_STATE="уже настроено"
        return 0
    fi

    # --- бэкап списка инбаундов ДО первой правки ---------------------------
    backup_dir="$(dirname "$INBOUNDS_BACKUP_FILE")"
    if [[ ! -d "$backup_dir" ]]; then
        mkdir -p "$backup_dir" || die "Не удалось создать каталог для бэкапа: ${backup_dir}"
        log "Создан каталог для бэкапа: ${backup_dir}"
    fi
    printf '%s' "$INBOUNDS_RESP" | jq . > "$INBOUNDS_BACKUP_FILE" \
        || die "Не удалось записать бэкап инбаундов в ${INBOUNDS_BACKUP_FILE}. Правка sniffing отменена."
    chmod 600 "$INBOUNDS_BACKUP_FILE" 2>/dev/null || true
    ok "Бэкап списка инбаундов (до правки): ${INBOUNDS_BACKUP_FILE}"
    INBOUNDS_BACKUP_DONE=1

    log "Правлю sniffing у инбаундов: ${n} шт. (POST /panel/api/inbounds/update/{id})"
    i=0
    while [[ "$i" -lt "$n" ]]; do
        id="${PLAN_IDS[$i]}"
        payload="${PLAN_PAYLOADS[$i]}"
        resp="$(api_post "/panel/api/inbounds/update/${id}" "$payload")" \
            || fail_after_write "Панель не ответила на POST /panel/api/inbounds/update/${id}. Инбаунд не изменён (или изменён частично — откати из ${INBOUNDS_BACKUP_FILE}: команда ниже)."
        if ! json_ok "$resp"; then
            fail_after_write "Панель отклонила правку инбаунда id=${id}: $(json_msg "$resp"). Бэкап: ${INBOUNDS_BACKUP_FILE}."
        fi
        ok "Инбаунд id=${id}: sniffing.routeOnly = true записан."
        i=$((i + 1))
    done
    SNIFF_STATE="изменено инбаундов: ${n}"

    verify_inbounds
}

verify_inbounds() { # перечитать список и подтвердить routeOnly у правленых id
    local i=0 id="" got="" bad=0
    log "Перечитываю инбаунды (проверка, что правка дошла)..."
    fetch_inbounds
    i=0
    while [[ "$i" -lt "${#PLAN_IDS[@]}" ]]; do
        id="${PLAN_IDS[$i]}"
        got="$(printf '%s' "$INBOUNDS_OBJ" | jq -r --argjson id "$id" \
            '[.[] | select(.id == $id)] | first | if . == null then "нет" else (.sniffing | if type == "string" then (fromjson? // {}) else . end | .routeOnly) end' 2>/dev/null || printf '?')"
        if [[ "$got" != "true" ]]; then
            bad=$((bad + 1))
            err "Инбаунд id=${id}: после правки routeOnly = ${got} (ожидалось true)."
        fi
        i=$((i + 1))
    done
    if [[ "$bad" -gt 0 ]]; then
        fail_after_write \
            "Панель не подтвердила правку sniffing у ${bad} инбаунд(ов)." \
            "Откат: восстанови инбаунды из ${INBOUNDS_BACKUP_FILE} и проверь journalctl -u x-ui -n 50."
    fi
    ok "Подтверждено: у правленых инбаундов sniffing.routeOnly = true."
}

# ---------------------------------------------------- логи самой панели -------
# ВАЖНО: здесь НЕ выполняются никакие «полезные» флаги панели. Скрипт только
# читает справку и печатает то, что в ней реально есть. Мы не знаем заранее,
# какие ключи поддерживает конкретная сборка 3x-ui, и выдумывать их нельзя:
# несуществующий флаг либо уронит команду, либо будет молча проигнорирован.
print_panel_logs_hint() {
    local bin="" out="" matches="" tried=""
    box "СЛУЖЕБНЫЕ ЛОГИ ПАНЕЛИ 3x-ui (отдельно от логов Xray)"
    cat <<'EOF'
  Шаблон Xray (xrayTemplateConfig) логи САМОЙ панели не выключает: панель
  пишет свои журналы отдельно — в systemd journal и/или файлы /var/log/x-ui/.
  Ниже — только то, что удалось прочитать в справке твоей сборки.
EOF

    if [[ -x "$XUI_BIN" ]]; then
        bin="$XUI_BIN"
    elif bin="$(command -v x-ui 2>/dev/null)" && [[ -n "$bin" ]]; then
        :
    else
        bin=""
    fi

    if [[ -n "$bin" ]]; then
        log "Читаю справку панели: ${bin} setting -h"
        out="$("$bin" setting -h 2>&1 || true)"
        tried="${bin} setting -h"
        if [[ -z "$out" ]]; then
            log "«${bin} setting -h» пусто — пробую «${bin} help»."
            out="$("$bin" help 2>&1 || true)"
            tried="${bin} help"
        fi
        matches="$(printf '%s\n' "$out" | grep -Ei -- "$PANEL_LOG_HINT_RE" | head -n 20 || true)"
        if [[ -n "$matches" ]]; then
            printf '  В выводе «%s» есть строки про логи — применяй ТОЛЬКО их:\n' "$tried"
            printf '%s\n' "$matches" | sed 's/^/      /'
            printf '\n'
            printf '  Смысл: ограничить уровень логов панели (чем выше уровень, тем меньше\n'
            printf '  пишется). Точные имена и значения — из строк выше, не из головы.\n'
        else
            printf '  В выводе «%s» ключей про логи НЕ нашлось.\n' "$tried"
            printf '  Значит, этой сборкой логи панели через CLI не ограничить.\n'
            printf '  Посмотри настройки в UI панели: Настройки → Панель / Xray (уровень\n'
            printf '  логов) и проверь вручную, пишет ли панель файлы в /var/log/x-ui/.\n'
        fi
    else
        printf '  Бинарь панели не найден (%s) — скрипт запущен не на ноде.\n' "$XUI_BIN"
        printf '  Это не ошибка: шаблон и инбаунды правятся по API. Но логи панели\n'
        printf '  смотреть надо на самой ноде — проверь вручную:\n'
        printf '      /usr/local/x-ui/x-ui setting -h | grep -i log\n'
        printf '      /usr/local/x-ui/x-ui help | grep -i log\n'
        printf '      ls -la /var/log/x-ui/ 2>/dev/null\n'
    fi

    cat <<'EOF'

  Если подходящих ключей нет — НЕ выдумывай их: проверь вручную в UI панели
  (Настройки → Панель/Xray) и в конфиге службы. Что смотреть:
    * куда пишет панель (journald или файлы в /var/log/x-ui/);
    * есть ли ротация (logrotate) и срок хранения;
    * не хранит ли панель IP-адреса клиентов в своих журналах.
  Это учёт работы сервиса, а не история посещений, но срок хранения должен быть
  ограничен — иначе «не храним» превращается в «храним, но недолго».
EOF
}

# ------------------------------------------------------------------ сводка -----
print_summary() {
    box "KOMETA • ГИГИЕНА ЛОГОВ (3x-ui): «НЕ ХРАНИМ ИСТОРИЮ ПОСЕЩЕНИЙ И DNS»"
    printf '  панель       : %s\n' "$PANEL_URL"
    if [[ "$ONLY_SNIFFING" -eq 1 ]]; then
        printf '  шаблон логов : не трогаю (--only-sniffing)\n'
    else
        printf '  шаблон логов : log.access=%s, log.loglevel=%s, log.dnsLog=%s, log.error=%s\n' \
            "$TARGET_ACCESS" "$TARGET_LOGLEVEL" "$TARGET_DNSLOG" "\"$TARGET_ERROR\""
    fi
    if [[ "$FIX_SNIFFING" -eq 1 ]]; then
        printf '  sniffing     : routeOnly=true у VLESS-инбаундов%s\n' \
            "$( [[ "${#INBOUND_TAGS[@]}" -gt 0 ]] && printf ' (--inbound-tag: %s)' "$(printf '%s, ' ${INBOUND_TAGS[@]+"${INBOUND_TAGS[@]}"} | sed 's/, $//')" || printf ' (все VLESS)' )"
    else
        printf '  sniffing     : не трогаю (нужен --fix-sniffing)\n'
    fi
    printf '  бэкап шаблона: %s%s\n' "$BACKUP_FILE" "$( [[ "$APPLY" -eq 1 ]] && printf '' || printf ' (только с --apply)' )"
    if [[ "$FIX_SNIFFING" -eq 1 ]]; then
        printf '  бэкап списка : %s%s\n' "$INBOUNDS_BACKUP_FILE" "$( [[ "$APPLY" -eq 1 ]] && printf '' || printf ' (только с --apply)' )"
    fi
    printf '  режим        : %s\n' "$( [[ "$APPLY" -eq 1 ]] && printf 'APPLY (записываю в панель)' || printf 'DRY-RUN (ничего не отправляю)' )"
}

print_payload() {
    print_json_section "ЦЕЛЕВАЯ СЕКЦИЯ log (уйдёт в xrayTemplateConfig)" "$LOG_SECTION"
    box "ИДЕМПОТЕНТНОСТЬ"
    cat <<EOF
  Повторный запуск с --apply ничего не перезапишет:
    * секция log сравнивается с целевой по четырём ключам (access, loglevel,
      dnsLog, error) — совпало → «уже настроено», POST не делается;
    * sniffing проверяется у каждого инбаунда: routeOnly уже true → пропуск,
      инбаунд не отправляется на update.
  То есть второй прогон — это ноль записей и ноль изменений.
EOF
}

dry_run_preview() {
    local ref="" preview=""
    if [[ "$ONLY_SNIFFING" -eq 0 ]]; then
        ref="$(reference_template)"
        preview="$(fix_log_section "$ref" "$LOG_SECTION")" \
            || die "Не удалось собрать предпросмотр шаблона (jq)."
        print_diff "$ref" "$preview" "эталонный шаблон 3x-ui (конфиг панели в dry-run не читается)"
    fi

    box "DRY-RUN: ЧТО БУДЕТ ЗАПИСАНО"
    cat <<EOF
  $( [[ "$ONLY_SNIFFING" -eq 1 ]] && printf 'Шаблон Xray не читается и не меняется (--only-sniffing).' || printf 'Запись шаблона — один вызов POST /panel/setting/update с полным объектом настроек панели (прочитанный объект + подменённый xrayTemplateConfig).' )
  Бэкап ${BACKUP_FILE} создаётся только с --apply и строго до записи.
$( [[ "$FIX_SNIFFING" -eq 1 ]] && printf '  sniffing: сначала GET /panel/api/inbounds/list, затем POST /panel/api/inbounds/update/{id} по каждому изменяемому VLESS-инбаунду; бэкап списка — %s.\n' "$INBOUNDS_BACKUP_FILE" || printf '  sniffing не трогается: добавь --fix-sniffing.\n' )
EOF

    echo
    log "Сетевых запросов нет: ни GET /panel/setting/all, ни POST /panel/setting/update, ни /panel/api/inbounds/list."
    log "Ничего не изменено. Для реальной записи добавь --apply."
}

print_result() {
    box "ИТОГ"
    if [[ "$APPLY" -eq 1 ]]; then
        printf '  шаблон логов : %s\n' "${LOG_STATE:-не трогался (--only-sniffing)}"
        if [[ "$FIX_SNIFFING" -eq 1 ]]; then
            printf '  sniffing     : %s\n' "${SNIFF_STATE:-не выполнялся}"
        fi
        if [[ "${BACKUP_DONE:-0}" -eq 1 ]]; then
            printf '  бэкап шаблона: %s\n' "$BACKUP_FILE"
        fi
        if [[ "${INBOUNDS_BACKUP_DONE:-0}" -eq 1 ]]; then
            printf '  бэкап списка : %s\n' "$INBOUNDS_BACKUP_FILE"
        fi
    else
        printf '  режим        : DRY-RUN — ничего не записано, сетевых запросов не было\n'
    fi
}

# -------------------------------------------------------------- как проверить --
print_how_to_check() {
    box "КАК ПРОВЕРИТЬ (руками на ноде)"
    cat <<EOF
  1. Перезапустить панель/Xray и убедиться, что конфиг принят:
       systemctl restart x-ui
       journalctl -u x-ui -n 50 --no-pager
     В логе не должно быть ошибок разбора конфига (infra/conf: …) — иначе
     панель записала шаблон, а Xray его не принял (откат — п. 7).

  2. Логи панели и Xray — где они лежат и растут ли:
       ls -la /var/log/x-ui/ 2>/dev/null
       ls -la /var/log/xray/ 2>/dev/null
     Здесь же видно, остались ли старые файлы: уже накопленный access.log —
     это ИСТОРИЯ ПОСЕЩЕНИЙ, её надо снести, одного выключения мало:
       rm -f /var/log/xray/access.log*

  3. Что access-лог Xray больше НЕ растёт:
       # 1) запомнить размер (файла может не быть — это и есть цель):
       sudo stat -c '%n %s %y' /var/log/xray/access.log 2>/dev/null || echo "access.log нет — уже хорошо"
       # 2) погонять через узел любой трафик 5–10 минут;
       # 3) сравнить ещё раз той же командой.
     Плюс независимая проверка — в самом шаблоне (Настройки → Xray) должно
     стоять "access": "none" и "dnsLog": false, а в журнале Xray не должно
     появляться строк про accepted-соединения с доменами:
       sudo journalctl -u x-ui --since '-10 min' | grep -c 'accepted' || true
     Если access.log появился снова — панель перезаписала шаблон своим
     (Настройки → Xray → «Сбросить настройки Xray» вернёт дефолт): повтори
     этот скрипт и проверь п. 1.

  4. Порты узла — что реально слушается:
       ss -tlnp

  5. Учёт трафика в панели (ГБ по клиентам) остаётся — это биллинг, а не
     история посещений. Проверь, что он жив: панель → инбаунды → трафик
     клиента растёт после п. 3.

  6. Политика конфиденциальности — ТОЛЬКО ПОСЛЕ п. 1–5:
     строка «не храним историю посещений и DNS-запросы» в
     docs/legal/ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ.md должна опираться на конфиг,
     а не наоборот. Порядок обратный — это обещание, которое опровергается
     одним ls у нас же на сервере.

  7. Откат (бэкап снимается строго до записи):
EOF
    if [[ "${BACKUP_DONE:-0}" -eq 1 ]]; then
        cat <<EOF
       # вернуть прежний xrayTemplateConfig:
       curl -s -H "Authorization: Bearer \$PANEL_TOKEN" ${PANEL_URL}/panel/setting/all \\
         | jq --rawfile tpl '${BACKUP_FILE}' '.obj | .xrayTemplateConfig = \$tpl' > /tmp/restore.json
       curl -s -X POST -H "Authorization: Bearer \$PANEL_TOKEN" \\
         -H 'Content-Type: application/json' --data-binary @/tmp/restore.json \\
         ${PANEL_URL}/panel/setting/update | jq .
       systemctl restart x-ui
EOF
    else
        cat <<EOF
       файл ${BACKUP_FILE} появится только после --apply; в нём — прежний
       xrayTemplateConfig целиком, восстановление командой из этого пункта.
EOF
    fi
    if [[ "${INBOUNDS_BACKUP_DONE:-0}" -eq 1 ]]; then
        cat <<EOF
       # вернуть sniffing конкретного инбаунда (id=<ID>) из бэкапа списка:
       curl -s -X POST -H "Authorization: Bearer \$PANEL_TOKEN" \\
         -H 'Content-Type: application/json' \\
         --data-binary "\$(jq -c '.obj[] | select(.id==<ID>) | del(.clientStats)' ${INBOUNDS_BACKUP_FILE})" \\
         ${PANEL_URL}/panel/api/inbounds/update/<ID> | jq .
       systemctl restart x-ui
EOF
    fi
}

# ------------------------------------------------------------------ main -------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --panel-url)       PANEL_URL="${2:?}"; shift 2 ;;
            --panel-token)     PANEL_TOKEN="${2:?}"; shift 2 ;;
            --backup-file)     BACKUP_FILE="${2:?}"; shift 2 ;;
            --inbounds-backup-file) INBOUNDS_BACKUP_FILE="${2:?}"; shift 2 ;;
            --inbound-tag)     INBOUND_TAGS+=("${2:?}"); shift 2 ;;
            --fix-sniffing)    FIX_SNIFFING=1; shift ;;
            --only-sniffing)   ONLY_SNIFFING=1; FIX_SNIFFING=1; shift ;;
            --insecure)        CURL_OPTS+=(--insecure); shift ;;
            --apply)           APPLY=1; shift ;;
            --dry-run)         APPLY=0; shift ;;
            -h | --help)       usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    have jq || die "Нужен jq (apt install jq / brew install jq)."
    require_panel

    BACKUP_FILE="${BACKUP_FILE:-./backup-xray-logs-$(date +%Y%m%d-%H%M%S).json}"
    INBOUNDS_BACKUP_FILE="${INBOUNDS_BACKUP_FILE:-./backup-inbounds-sniffing-$(date +%Y%m%d-%H%M%S).json}"

    LOG_SECTION="$(build_log_section)" || die "Не удалось собрать целевую секцию log (jq)."
    LOG_STATE=""
    SNIFF_STATE=""
    BACKUP_DONE=0
    INBOUNDS_BACKUP_DONE=0
    PLAN_IDS=()
    PLAN_PAYLOADS=()

    print_summary

    if [[ "$ONLY_SNIFFING" -eq 1 ]]; then
        log "--only-sniffing: шаблон логов Xray не читаю и не меняю."
    else
        print_payload
    fi

    if [[ "$FIX_SNIFFING" -eq 1 ]]; then
        print_sniffing_preview
    else
        box "SNIFFING"
        cat <<'EOF'
  Sniffing инбаундов не трогаю: для этого нужен --fix-sniffing.
  Почему это важно для «не храним историю посещений»: при routeOnly=false
  разобранный домен (SNI/Host) подменяет адрес назначения, то есть сервер
  разбирает и применяет домен из трафика клиента. При routeOnly=true домен
  используется только для маршрутизации и не подменяет адрес.
EOF
    fi

    if [[ "$APPLY" -eq 1 ]]; then
        have curl || die "Нужен curl для --apply."
    fi

    print_panel_logs_hint

    if [[ "$APPLY" -eq 1 ]]; then
        if [[ "$ONLY_SNIFFING" -eq 0 ]]; then
            apply_logs
        fi
        if [[ "$FIX_SNIFFING" -eq 1 ]]; then
            apply_sniffing
        fi
    else
        dry_run_preview
    fi

    print_result
    print_how_to_check
}

main "$@"
