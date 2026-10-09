#!/usr/bin/env bash
# =============================================================================
#  Kometa — проверка готовности к запуску (preflight)
#
#  Запускать ПЕРЕД тем как открывать продажи: скрипт проверяет, что всё
#  настроено и связалось, и говорит человеческим языком, что не так.
#
#  Использование:
#      bash scripts/preflight.sh              # проверить всё
#      bash scripts/preflight.sh --quiet      # только проблемы (для cron)
#
#  Код возврата: 0 — можно продавать, 1 — есть блокеры.
# =============================================================================

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_DIR="$(pwd)"
ENV_FILE="$PROJECT_DIR/.env"
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
BLOCKERS=0
WARNINGS=0

ok()   { [[ $QUIET -eq 1 ]] || echo "  ${GREEN}✅${NC} $1"; }
warn() { WARNINGS=$((WARNINGS+1)); echo "  ${YELLOW}⚠️ ${NC} $1"; }
bad()  { BLOCKERS=$((BLOCKERS+1)); echo "  ${RED}❌${NC} $1"; }
head_() { [[ $QUIET -eq 1 ]] || echo; [[ $QUIET -eq 1 ]] || echo "${BOLD}$1${NC}"; }

# ---------------------------------------------------------------- окружение
env_value() {
  [[ -f "$ENV_FILE" ]] || return 1
  # берём последнее непустое значение, игнорируя комментарии
  grep -E "^${1}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

# ID инбаундов верхнего уровня из ответа панели (JSON читается со stdin).
# Разбор — scripts/panel_inbound_ids.py: у элементов clientStats есть СВОЁ поле
# id, поэтому греп по сырому JSON считал статистику клиента за инбаунд.
panel_inbound_ids() {
  local helper="${PROJECT_DIR}/scripts/panel_inbound_ids.py"
  if [[ -f "$helper" ]] && command -v python3 >/dev/null 2>&1; then
    python3 "$helper" 2>/dev/null || true
    return 0
  fi
  if command -v jq >/dev/null 2>&1; then
    jq -r '.obj[]?.id' 2>/dev/null || true
    return 0
  fi
  return 1
}

[[ $QUIET -eq 1 ]] || echo "${BOLD}🛰  Kometa — проверка готовности к запуску${NC}"

head_ "1. Конфигурация (.env)"

if [[ ! -f "$ENV_FILE" ]]; then
  bad ".env не найден — скопируй .env.example в .env и заполни"
else
  ok ".env найден"
fi

BOT_TOKEN="$(env_value BOT_TOKEN || true)"
ADMIN_IDS="$(env_value ADMIN_IDS || true)"
PANEL_TYPE="$(env_value PANEL_TYPE || true)"
PANEL_URL="$(env_value PANEL_URL || true)"
PANEL_TOKEN="$(env_value PANEL_TOKEN || true)"
PANEL_INBOUNDS="$(env_value PANEL_INBOUND_IDS || true)"
WEB_PORT="$(env_value WEB_PORT || true)"
PUBLIC_BASE_URL="$(env_value PUBLIC_BASE_URL || true)"
MANUAL_DETAILS="$(env_value MANUAL_PAYMENT_DETAILS || true)"
CRYPTO_TOKEN="$(env_value CRYPTOBOT_TOKEN || true)"
STARS_ENABLED="$(env_value STARS_ENABLED || true)"
ADMIN_PANEL_PASSWORD="$(env_value ADMIN_PANEL_PASSWORD || true)"
[[ -z "$WEB_PORT" ]] && WEB_PORT=8080
[[ -z "$PANEL_TYPE" ]] && PANEL_TYPE=fake

# --- токен бота
if [[ -z "$BOT_TOKEN" ]]; then
  bad "BOT_TOKEN пуст — бот не запустится"
elif [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
  warn "BOT_TOKEN не похож на токен от @BotFather (ожидается вид 123456:AA...)"
else
  BOT_INFO="$(curl -s -m 15 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)"
  if grep -q '"ok":true' <<<"$BOT_INFO"; then
    BOT_USERNAME="$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$BOT_INFO")"
    ok "Токен бота рабочий: @${BOT_USERNAME}"
  else
    bad "Telegram не принял токен бота — проверь BOT_TOKEN (или сделай /revoke у @BotFather)"
  fi
fi

# --- админы
if [[ -z "$ADMIN_IDS" ]]; then
  bad "ADMIN_IDS пуст — никто не увидит заявки и алерты"
else
  ok "Админы: ${ADMIN_IDS}"
fi

# --- приём денег
PAYMENTS=""
[[ -n "$MANUAL_DETAILS" ]] && PAYMENTS="${PAYMENTS}перевод "
[[ -n "$CRYPTO_TOKEN" ]] && PAYMENTS="${PAYMENTS}крипта "
[[ "$STARS_ENABLED" == "true" ]] && PAYMENTS="${PAYMENTS}звёзды "
if [[ -z "$PAYMENTS" ]]; then
  bad "Ни один способ оплаты не настроен: заполни MANUAL_PAYMENT_DETAILS, CRYPTOBOT_TOKEN или STARS_ENABLED=true"
else
  ok "Способы оплаты, которые увидит клиент: ${PAYMENTS}"
  [[ -z "$MANUAL_DETAILS" ]] && warn "Реквизиты для перевода не заданы — оплата по СБП не показывается (а это половина аудитории)"
fi

# --- автопроверка переводов
AUTOPAY_ENABLED="$(env_value AUTOPAY_ENABLED || true)"
CSV_GLOB="$(env_value STATEMENT_CSV_GLOB || true)"
IMAP_USER="$(env_value BANK_IMAP_USER || true)"
if [[ "$AUTOPAY_ENABLED" == "true" ]]; then
  CSV_FILES=0
  if [[ -n "$CSV_GLOB" ]]; then
    # shellcheck disable=SC2086
    CSV_FILES=$(find $(dirname "$CSV_GLOB") -maxdepth 1 -name "$(basename "$CSV_GLOB")" 2>/dev/null | wc -l | tr -d ' ')
  fi
  if [[ "$CSV_FILES" -gt 0 ]]; then
    ok "Автоплатёж включён: найдено файлов выписки — ${CSV_FILES}"
  elif [[ -n "$IMAP_USER" ]]; then
    ok "Автоплатёж включён: почта банка (${IMAP_USER})"
  else
    bad "AUTOPAY_ENABLED=true, но источники не настроены: нет файлов по STATEMENT_CSV_GLOB и пуст BANK_IMAP_USER"
  fi
else
  warn "Автоплатёж выключен: переводы по СБП придётся подтверждать вручную (AUTOPAY_ENABLED=true включает автопроверку)"
fi

# --- админ-панель
ADMIN_LOCAL_ONLY="$(env_value ADMIN_LOCAL_ONLY || true)"
if [[ -z "$ADMIN_PANEL_PASSWORD" ]]; then
  warn "ADMIN_PANEL_PASSWORD не задан — веб-панель /admin выключена"
elif [[ "$ADMIN_LOCAL_ONLY" == "false" ]]; then
  case "${PUBLIC_BASE_URL:-}" in
    https://*) ok "Веб-панель /admin открыта наружу по HTTPS — допустимо" ;;
    *) bad "ADMIN_LOCAL_ONLY=false без HTTPS: пароль админки пойдёт открытым текстом. Верни true и ходи через SSH-туннель" ;;
  esac
else
  ok "Веб-панель /admin доступна только с localhost (SSH-туннель: ssh -L 8090:127.0.0.1:8090 root@IP)"
fi

head_ "2. Сервис запущен"

if pgrep -f "app.main" >/dev/null 2>&1; then
  ok "Процесс бота работает ($(pgrep -f 'app.main' | head -1))"
elif command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -q kometa-bot; then
  ok "Контейнер kometa-bot работает (docker)"
else
  bad "Бот не запущен: .venv/bin/python -m app.main  (или docker compose up -d)"
fi

# Веб-слой может слушать https (так и нужно на боевом сервере: Happ и v2rayNG
# отказываются добавлять подписку по http). Определяем схему по .env.
WEB_SSL_CERT="$(env_value WEB_SSL_CERT || true)"
WEB_SCHEME="http"
CURL_TLS=()
if [[ -n "$WEB_SSL_CERT" ]]; then
  WEB_SCHEME="https"
  # сертификат Let's Encrypt выпущен на публичное имя, по 127.0.0.1 он не совпадёт
  CURL_TLS=(--insecure)
fi

# `${CURL_TLS[@]+...}` вместо `${CURL_TLS[@]}`: в bash 3.2 (macOS) раскрытие
# пустого массива при `set -u` — это ошибка «unbound variable», и проверка
# веб-слоя падала с техническим текстом вместо понятного вердикта.
HEALTH="$(curl -s -m 8 ${CURL_TLS[@]+"${CURL_TLS[@]}"} "${WEB_SCHEME}://127.0.0.1:${WEB_PORT}/health" || true)"
if grep -q '"status":"ok"' <<<"$HEALTH"; then
  ok "Веб-слой отвечает на ${WEB_SCHEME}://127.0.0.1:${WEB_PORT}"
else
  bad "Веб-слой не отвечает на ${WEB_SCHEME}://127.0.0.1:${WEB_PORT}/health — ссылки-подписки не будут открываться"
fi

head_ "3. Панель VPN"

if [[ "$PANEL_TYPE" == "fake" ]]; then
  warn "PANEL_TYPE=fake — это режим разработки: реальных VPN-ключей нет, продавать нельзя"
  warn "После установки панели поставь PANEL_TYPE=xui, PANEL_URL, PANEL_TOKEN, PANEL_INBOUND_IDS"
else
  if [[ -z "$PANEL_URL" || -z "$PANEL_TOKEN" ]]; then
    bad "PANEL_URL или PANEL_TOKEN пуст — бот не сможет выдавать доступ"
  else
    API="${PANEL_URL%/}/panel/api/inbounds/list"
    RESPONSE="$(curl -sk -m 15 -H "Authorization: Bearer ${PANEL_TOKEN}" "$API" || true)"
    if grep -q '"success":true' <<<"$RESPONSE"; then
      ok "Панель отвечает и токен принят"
      # Только id верхнего уровня: у элементов clientStats своё поле id, и
      # грепом по сырому JSON проверка находила «инбаунд» в статистике клиента.
      IDS_IN_PANEL="$(printf '%s' "$RESPONSE" | panel_inbound_ids | sort -n | tr '\n' ' ')"
      ok "Инбаунды в панели: ${IDS_IN_PANEL:-нет}"
      if [[ -z "$PANEL_INBOUNDS" ]]; then
        bad "PANEL_INBOUND_IDS пуст — бот не знает, куда добавлять клиентов"
      else
        MISSING=""
        for id in ${PANEL_INBOUNDS//,/ }; do
          printf ' %s ' "$IDS_IN_PANEL" | grep -q " ${id} " || MISSING="${MISSING} ${id}"
        done
        if [[ -n "$MISSING" ]]; then
          bad "В панели нет инбаундов с ID:${MISSING} — проверь PANEL_INBOUND_IDS"
        else
          ok "Все ID инбаундов из .env существуют в панели: ${PANEL_INBOUNDS}"
        fi
      fi
    else
      bad "Панель не ответила или токен неверный (проверь PANEL_URL, PANEL_TOKEN, что панель запущена)"
    fi
  fi
fi

# Ноды мультигео: у каждой своя панель и свои ID, поэтому проверка основной
# панели о них молчит. Сверяем тем же кодом, что и бот.
if [[ -f "${PROJECT_DIR}/app/tools/check_nodes.py" ]]; then
  if [[ -x "${PROJECT_DIR}/.venv/bin/python" && -w "${PROJECT_DIR}/data" ]]; then
    if NODES_OUT="$(cd "$PROJECT_DIR" && ./.venv/bin/python -m app.tools.check_nodes 2>&1)"; then
      ok "Инбаунды нод сходятся с их панелями"
    else
      bad "Инбаунды нод разошлись с панелями — клиенты не получат локацию:
$(printf '%s' "$NODES_OUT" | sed 's/^/     /')"
    fi
  else
    warn "Ноды не проверены: нужен .venv и права на data/ — запусти .venv/bin/python -m app.tools.check_nodes"
  fi
fi

head_ "4. Ссылка-подписка"

if [[ -z "$PUBLIC_BASE_URL" || "$PUBLIC_BASE_URL" == *"127.0.0.1"* || "$PUBLIC_BASE_URL" == *"localhost"* ]]; then
  bad "PUBLIC_BASE_URL локальный (${PUBLIC_BASE_URL:-пусто}) — клиенты с телефонов не откроют ссылку. Укажи http://IP:${WEB_PORT}"
elif [[ "$PUBLIC_BASE_URL" == http://* ]]; then
  # Старт на IP без домена — нормальный режим, но с оговорками
  ok "Публичный адрес: ${PUBLIC_BASE_URL} (работаем по IP, без домена)"
  warn "HTTPS нет: подписки идут по http — это работает, но IP легче заблокировать, а админку нельзя открывать наружу"
else
  ok "Публичный адрес: ${PUBLIC_BASE_URL}"
  if curl -s -m 8 -o /dev/null -w "%{http_code}" "${PUBLIC_BASE_URL%/}/health" | grep -q 200; then
    ok "Адрес доступен извне сервиса"
  else
    warn "По публичному адресу /health не отвечает — проверь firewall и что порт открыт"
  fi
fi

head_ "5. Сервер"

if [[ "$(uname)" == "Linux" ]]; then
  LOAD="$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)"
  ok "Load average: ${LOAD}"
  FREE_MB="$(df -Pm / | awk 'NR==2 {print $4}')"
  if [[ "${FREE_MB:-0}" -lt 1024 ]]; then
    warn "Свободно ${FREE_MB} МБ на диске — мало для логов и обновлений"
  else
    ok "Свободно на диске: ${FREE_MB} МБ"
  fi
  if timedatectl 2>/dev/null | grep -q "synchronized: yes"; then
    ok "Время синхронизировано (важно для TLS и VPN)"
  else
    warn "Проверь синхронизацию времени: timedatectl (для Reality/TLS нужен точный час)"
  fi
else
  warn "Проверка ресурсов сервера пропущена (не Linux)"
fi

head_ "Итог"

if [[ $BLOCKERS -gt 0 ]]; then
  echo "  ${RED}${BOLD}Продавать пока нельзя: блокеров ${BLOCKERS}, предупреждений ${WARNINGS}.${NC}"
  exit 1
fi
if [[ $WARNINGS -gt 0 ]]; then
  echo "  ${YELLOW}${BOLD}Можно запускать, но есть предупреждения: ${WARNINGS}.${NC}"
  exit 0
fi
echo "  ${GREEN}${BOLD}Всё готово — можно открывать продажи 🚀${NC}"
exit 0
