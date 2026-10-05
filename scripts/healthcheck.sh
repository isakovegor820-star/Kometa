#!/usr/bin/env bash
# =============================================================================
#  Kometa — проверка здоровья сервиса
# =============================================================================
#  Проверяет:
#    1) доступность API панели 3x-ui (/panel/api/inbounds/list с токеном из .env)
#       и что ID инбаундов из PANEL_INBOUND_IDS реально существуют;
#    2) живость бота: docker-контейнер (kometa-bot), либо systemd-юнит,
#       либо процесс `python -m app.main`;
#    3) веб-слой ссылки-подписки: http://127.0.0.1:8080/health;
#    4) свободное место на диске, память и load average;
#    5) локальную панель (systemd x-ui или контейнер kometa-panel), если она тут.
#
#  Код возврата: 0 — всё хорошо, 1 — есть проблема (что именно — печатается).
#
#  Запуск вручную:  bash scripts/healthcheck.sh
#  Из cron (каждые 15 минут, с уведомлением в Telegram при проблеме):
#    */15 * * * * cd /root/kometa && bash scripts/healthcheck.sh --notify --quiet
#
#  Уведомление идёт через Bot API (curl) на ADMIN_CHAT_ID, а если он не задан —
#  на первый ID из ADMIN_IDS. Токен и chat_id берутся ТОЛЬКО из .env.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------- настройки ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

BOT_CONTAINER="${BOT_CONTAINER:-kometa-bot}"
PANEL_CONTAINER="${PANEL_CONTAINER:-kometa-panel}"
BOT_SERVICE="${BOT_SERVICE:-kometa-bot}"

DISK_WARN=85; DISK_FAIL=95      # % занятого места
MEM_WARN=15;  MEM_FAIL=7        # % свободной памяти
LOAD_WARN_MULT=2; LOAD_FAIL_MULT=4

NOTIFY=0
NOTIFY_ALWAYS=0
QUIET=0
JSON=0
SKIP_PANEL=0
SKIP_BOT=0
PUSH_URL=""

FAILURES=()
WARNINGS=()

# ------------------------------------------------------------------- вывод ----
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_OFF=""
fi
log()  { [[ "$QUIET" -eq 1 ]] || printf '%s[•]%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()   { [[ "$QUIET" -eq 1 ]] || printf '%s[✓]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
err()  { printf '%s[✗]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }

problem() { FAILURES+=("$1"); err "$1"; }
soft()    { WARNINGS+=("$1"); warn "$1"; }

usage() {
    cat <<'EOF'
Проверка здоровья Kometa (панель + бот + сервер).

Использование:
  bash scripts/healthcheck.sh [флаги]

Флаги:
  --notify         отправить результат в Telegram (BOT_TOKEN + ADMIN_CHAT_ID/ADMIN_IDS)
  --always         уведомлять всегда, а не только при проблеме
  --quiet          печатать только проблемы (удобно для cron)
  --json           напечатать итог одной строкой JSON
  --push URL       отправить пинг в Uptime Kuma Push URL
  --skip-panel     не проверять панель
  --skip-bot       не проверять бота
  -h, --help       эта справка
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

env_get() { # значение переменной из .env без выполнения файла
    local key="$1" line
    [[ -f "$ENV_FILE" ]] || return 1
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

# --------------------------------------------------------------- проверки ----
check_config() {
    if [[ ! -f "$ENV_FILE" ]]; then
        problem "Нет файла .env (${ENV_FILE}) — бот не сможет запуститься. Скопируй: cp .env.example .env"
        return 0
    fi
    ok ".env найден: ${ENV_FILE}"
    local token
    token="$(env_get BOT_TOKEN || true)"
    if [[ -z "$token" ]]; then
        problem "BOT_TOKEN пустой — бот не подключится к Telegram (@BotFather → токен → .env)."
    else
        ok "BOT_TOKEN заполнен."
    fi
}

check_panel() {
    local ptype url token ids resp code latency start end
    ptype="$(env_get PANEL_TYPE || true)"
    ptype="${ptype:-fake}"

    if [[ "$ptype" != "xui" ]]; then
        log "PANEL_TYPE=${ptype} — панель не проверяю (для локальной разработки это норма)."
        return 0
    fi

    url="$(env_get PANEL_URL || true)"
    token="$(env_get PANEL_TOKEN || true)"
    if [[ -z "$url" || -z "$token" ]]; then
        problem "PANEL_TYPE=xui, но PANEL_URL или PANEL_TOKEN пусты в .env."
        return 0
    fi
    url="${url%/}"

    start="$(date +%s)"
    resp="$(curl -sS --max-time 20 -H "Authorization: Bearer ${token}" \
        "${url}/panel/api/inbounds/list" 2>&1)" || {
        problem "API панели недоступен: ${url}/panel/api/inbounds/list (панель выключена, порт закрыт или неверный адрес)."
        return 0
    }
    end="$(date +%s)"
    latency=$((end - start))

    if [[ "$(printf '%s' "$resp" | grep -c '"success":true' || true)" -eq 0 ]]; then
        if printf '%s' "$resp" | grep -q '401\|Unauthorized'; then
            problem "Панель отклонила токен (401). Выпусти новый: x-ui setting -getApiToken -tokenName kometa-bot -tokenScope admin"
        else
            problem "Панель ответила ошибкой на /inbounds/list: $(printf '%s' "$resp" | head -c 200)"
        fi
        return 0
    fi
    ok "API панели отвечает (${latency}s): ${url}"

    local count
    count="$(printf '%s' "$resp" | { grep -o '"id":' || true; } | wc -l | tr -d ' ')"
    log "Инбаундов в панели: ${count}"

    ids="$(env_get PANEL_INBOUND_IDS || true)"
    if [[ -z "$ids" ]]; then
        soft "PANEL_INBOUND_IDS пустой — бот не сможет собрать ссылку-подписку."
        return 0
    fi
    local id missing=""
    ids="$(printf '%s' "$ids" | tr -d ' ')"
    local IFS_OLD="$IFS"; IFS=','
    for id in $ids; do
        [[ -n "$id" ]] || continue
        printf '%s' "$resp" | grep -q "\"id\":${id}[,}]" || missing="${missing} ${id}"
    done
    IFS="$IFS_OLD"
    if [[ -n "$missing" ]]; then
        problem "В панели нет инбаундов с ID:${missing} — проверь PANEL_INBOUND_IDS и что инбаунды не удалены."
    else
        ok "Все инбаунды из PANEL_INBOUND_IDS на месте (${ids})."
    fi

    # Локальная панель: служба или контейнер (если панель на этом же сервере).
    if [[ -f /etc/x-ui/x-ui.db ]] || [[ -x /usr/local/x-ui/x-ui ]]; then
        if systemctl is-active --quiet x-ui 2>/dev/null; then
            ok "Служба x-ui на этом сервере: active."
        else
            problem "Служба x-ui на этом сервере не активна: systemctl status x-ui"
        fi
    elif have docker && docker inspect "$PANEL_CONTAINER" >/dev/null 2>&1; then
        local pstate
        pstate="$(docker inspect -f '{{.State.Status}}' "$PANEL_CONTAINER" 2>/dev/null || echo unknown)"
        if [[ "$pstate" == "running" ]]; then
            ok "Контейнер панели ${PANEL_CONTAINER}: running."
        else
            problem "Контейнер панели ${PANEL_CONTAINER} в состоянии '${pstate}'."
        fi
    fi
}

check_bot() {
    local running=0 how="" web_port health

    if have docker && docker inspect "$BOT_CONTAINER" >/dev/null 2>&1; then
        local state health_state
        state="$(docker inspect -f '{{.State.Status}}' "$BOT_CONTAINER" 2>/dev/null || echo unknown)"
        health_state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$BOT_CONTAINER" 2>/dev/null || echo none)"
        how="docker:${BOT_CONTAINER}"
        if [[ "$state" == "running" ]]; then
            running=1
            ok "Контейнер бота: running (health=${health_state})."
            if [[ "$health_state" == "unhealthy" ]]; then
                problem "Docker помечает контейнер бота как unhealthy — смотри: docker logs ${BOT_CONTAINER} --tail 50"
            fi
        else
            problem "Контейнер бота ${BOT_CONTAINER} в состоянии '${state}' — подними: docker compose up -d"
        fi
    elif have systemctl && systemctl list-unit-files 2>/dev/null | grep -q "^${BOT_SERVICE}\.service"; then
        how="systemd:${BOT_SERVICE}"
        if systemctl is-active --quiet "$BOT_SERVICE"; then
            running=1
            ok "Служба ${BOT_SERVICE}: active."
        else
            problem "Служба ${BOT_SERVICE} не активна: systemctl status ${BOT_SERVICE}"
        fi
    elif pgrep -f "app.main" >/dev/null 2>&1; then
        running=1
        how="process:app.main"
        ok "Процесс 'python -m app.main' найден."
    else
        problem "Бот не найден: ни контейнера ${BOT_CONTAINER}, ни службы ${BOT_SERVICE}, ни процесса app.main."
    fi

    # Веб-слой ссылки-подписки (порт 8080).
    web_port="$(env_get WEB_PORT || true)"
    web_port="${web_port:-8080}"
    health="$(curl -sS --max-time 8 "http://127.0.0.1:${web_port}/health" 2>&1 || true)"
    if printf '%s' "$health" | grep -q 'ok'; then
        ok "Веб-слой подписки отвечает: http://127.0.0.1:${web_port}/health"
    else
        problem "Веб-слой подписки не отвечает на порту ${web_port} — клиенты не смогут обновить подписку (/sub/<token>)."
    fi

    [[ "$running" -eq 1 ]] || log "Способ запуска бота: ${how:-не определён}"
}

check_system() {
    # --- диск ---
    local path used avail
    path="$ROOT_DIR"
    [[ -d "$path" ]] || path="/"
    used="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}' || true)"
    avail="$(df -Ph "$path" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
    if [[ -n "$used" ]]; then
        if [[ "$used" -ge "$DISK_FAIL" ]]; then
            problem "Мало места на диске: занято ${used}% (свободно ${avail}) — бэкапы и БД могут не записаться."
        elif [[ "$used" -ge "$DISK_WARN" ]]; then
            soft "Диск занят на ${used}% (свободно ${avail}). Почисти: старые архивы в backups/, docker system prune"
        else
            ok "Диск: занято ${used}% (свободно ${avail})."
        fi
    else
        soft "Не удалось определить свободное место (df)."
    fi

    # --- память ---
    if [[ -r /proc/meminfo ]]; then
        local total avail_kb pct_free
        total="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
        avail_kb="$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)"
        if [[ -n "$total" && -n "$avail_kb" && "$total" -gt 0 ]]; then
            pct_free=$((avail_kb * 100 / total))
            if [[ "$pct_free" -lt "$MEM_FAIL" ]]; then
                problem "Почти нет свободной памяти: ${pct_free}% — проверь процессы (ps aux --sort=-%mem | head)."
            elif [[ "$pct_free" -lt "$MEM_WARN" ]]; then
                soft "Свободной памяти мало: ${pct_free}%."
            else
                ok "Память: свободно ${pct_free}%."
            fi
        fi
    fi

    # --- нагрузка ---
    local cores load1 load_int
    cores="$( (nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null) | head -n 1 || true)"
    load1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || true)"
    if [[ -n "$cores" && -n "$load1" ]]; then
        load_int="$(printf '%s' "$load1" | cut -d. -f1)"
        if [[ "$load_int" -ge $((cores * LOAD_FAIL_MULT)) ]]; then
            problem "Высокая нагрузка: load ${load1} при ${cores} ядрах — сервер перегружен."
        elif [[ "$load_int" -ge $((cores * LOAD_WARN_MULT)) ]]; then
            soft "Повышенная нагрузка: load ${load1} при ${cores} ядрах."
        else
            ok "Нагрузка: load ${load1} при ${cores} ядрах."
        fi
    fi

    # --- аптайм ---
    if [[ -r /proc/uptime ]]; then
        local up
        up="$(awk '{printf "%d", $1/86400}' /proc/uptime)"
        log "Сервер работает ${up} дн."
    fi
}

# ------------------------------------------------------------------ отчёты ----
send_telegram() { # send_telegram <текст>
    local text="$1" token chat resp
    token="$(env_get BOT_TOKEN || true)"
    chat="$(env_get ADMIN_CHAT_ID || true)"
    [[ -n "$chat" ]] || chat="$(env_get ADMIN_IDS || true)"
    chat="${chat%%,*}"          # первый ID из списка
    chat="$(printf '%s' "$chat" | tr -d ' ')"
    if [[ -z "$token" || -z "$chat" ]]; then
        warn "Не отправляю в Telegram: нет BOT_TOKEN или ADMIN_CHAT_ID/ADMIN_IDS в .env."
        return 0
    fi
    resp="$(curl -sS --max-time 15 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
        -d "chat_id=${chat}" -d 'disable_web_page_preview=true' \
        --data-urlencode "text=${text}" 2>&1 || true)"
    if printf '%s' "$resp" | grep -q '"ok":true'; then
        ok "Уведомление отправлено в Telegram (chat_id=${chat})."
    else
        warn "Telegram не принял сообщение: $(printf '%s' "$resp" | head -c 200)"
    fi
}

push_uptime_kuma() { # push_uptime_kuma <up|down> <сообщение>
    local status="$1" msg="$2"
    [[ -n "$PUSH_URL" ]] || return 0
    curl -sS --max-time 10 -G "$PUSH_URL" \
        --data-urlencode "status=${status}" --data-urlencode "msg=${msg}" \
        >/dev/null 2>&1 || warn "Uptime Kuma push не удался (${PUSH_URL})."
}

# ------------------------------------------------------------------- main -----
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --notify)     NOTIFY=1; shift ;;
            --always)     NOTIFY_ALWAYS=1; NOTIFY=1; shift ;;
            --quiet)      QUIET=1; shift ;;
            --json)       JSON=1; shift ;;
            --push)       PUSH_URL="${2:?--push требует URL}"; shift 2 ;;
            --skip-panel) SKIP_PANEL=1; shift ;;
            --skip-bot)   SKIP_BOT=1; shift ;;
            -h | --help)  usage; exit 0 ;;
            *) err "Неизвестный флаг: $1 (см. --help)"; exit 1 ;;
        esac
    done

    have curl || { err "Нужен curl (apt-get install -y curl)."; exit 1; }

    log "Проверка Kometa: $(date '+%Y-%m-%d %H:%M:%S') на $(hostname 2>/dev/null || echo '?')"

    check_config

    if [[ "$SKIP_PANEL" -eq 0 ]]; then check_panel; else log "Проверка панели пропущена (--skip-panel)."; fi
    if [[ "$SKIP_BOT" -eq 0 ]]; then check_bot; else log "Проверка бота пропущена (--skip-bot)."; fi
    check_system

    local problems="${#FAILURES[@]}" warnings="${#WARNINGS[@]}"
    local host now
    host="$(hostname 2>/dev/null || echo '?')"
    now="$(date '+%Y-%m-%d %H:%M:%S')"

    local summary
    if [[ "$problems" -gt 0 ]]; then
        summary="ПРОБЛЕМА (${problems})"
    elif [[ "$warnings" -gt 0 ]]; then
        summary="РАБОТАЕТ, но есть предупреждения (${warnings})"
    else
        summary="ВСЁ В ПОРЯДКЕ"
    fi

    # Итоговая плашка со списком того, что именно сломалось.
    printf '\n%s══════════════════════════════════════════════════════════════%s\n' "$C_BOLD" "$C_OFF"
    printf '%s%s%s — %s\n' "$C_BOLD" "$summary" "$C_OFF" "$host"
    if [[ "$problems" -gt 0 ]]; then
        printf '%sЧТО СЛОМАЛОСЬ:%s\n' "$C_RED" "$C_OFF"
        local f
        for f in ${FAILURES[@]+"${FAILURES[@]}"}; do printf '  • %s\n' "$f"; done
    fi
    if [[ "$warnings" -gt 0 ]]; then
        printf '%sПРЕДУПРЕЖДЕНИЯ:%s\n' "$C_YELLOW" "$C_OFF"
        local w
        for w in ${WARNINGS[@]+"${WARNINGS[@]}"}; do printf '  • %s\n' "$w"; done
    fi
    printf '%s══════════════════════════════════════════════════════════════%s\n' "$C_BOLD" "$C_OFF"

    if [[ "$JSON" -eq 1 ]]; then
        local ok_json="true"
        [[ "$problems" -gt 0 ]] && ok_json="false"
        printf '{"ok":%s,"host":"%s","time":"%s","problems":%s,"warnings":%s}\n' \
            "$ok_json" "$host" "$now" "$problems" "$warnings"
    fi

    # Уведомления.
    if [[ "$NOTIFY" -eq 1 ]] && { [[ "$problems" -gt 0 ]] || [[ "$NOTIFY_ALWAYS" -eq 1 ]]; }; then
        local text
        text="Kometa: ${summary}
Сервер: ${host}
Время: ${now}"
        if [[ "$problems" -gt 0 ]]; then
            text="${text}
Что сломалось:
$(for f in ${FAILURES[@]+"${FAILURES[@]}"}; do printf -- '- %s\n' "$f"; done)"
        fi
        if [[ "$warnings" -gt 0 ]]; then
            text="${text}
Предупреждения:
$(for w in ${WARNINGS[@]+"${WARNINGS[@]}"}; do printf -- '- %s\n' "$w"; done)"
        fi
        send_telegram "$text"
    fi

    if [[ "$problems" -gt 0 ]]; then
        push_uptime_kuma down "$summary"
        exit 1
    fi
    push_uptime_kuma up "$summary"
    exit 0
}

main "$@"
