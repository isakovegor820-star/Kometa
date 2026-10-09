#!/usr/bin/env bash
# =============================================================================
#  Kometa — приёмка ревью 09.10.2026 (ветка fix/review-2026-10)
#
#  Запускается владельцем одной командой:
#      bash scripts/accept_review.sh
#
#  Что делает по порядку и печатает итог по каждому пункту:
#    B1–B4  — PoC-регрессии платежей (tests/test_review_regressions.py);
#    WATA   — провайдера нет: вебхук 404, упоминаний в коде и .env.example нет;
#    B6     — релизный гейт: CI-воркфлоу, честная итоговая строка pytest;
#    B7     — ретенция: tests/test_retention.py (30 дней / 12 месяцев / заказы /
#             удаление по запросу);
#    H3/H5/H6/H8 — улучшения спринта (копейки, PRAGMA, сессии, лимиты);
#    ПОЛНЫЙ НАБОР — весь pytest;
#    ПРЕДПОЛЁТ — bash scripts/preflight.sh (готовность к запуску);
#    ПРОБЫ  — POST /payments/wata/webhook → 404, grep по коду и .env.example,
#             git status --short пуст после прогона.
#
#  Формат: «<пункт>: ПРИНЯТО / НЕ ПРИНЯТО (причина)».
#  Код возврата ≠ 0, если хотя бы один пункт не принят.
#
#  Флаг для локальной машины, где сервис не запущен:
#      ACCEPT_LOCAL_PREFLIGHT_OK=1 bash scripts/accept_review.sh
#  Он понижает только пункт «ПРЕДПОЛЁТ» до предупреждения (на прод-сервере
#  предполёт обязан быть зелёным, поэтому по умолчанию он считается строго).
# =============================================================================

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_DIR="$(pwd)"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "${RED}Нет pytest: создай окружение (.venv) и установи requirements.txt${NC}"
  exit 2
fi

PASSED=0
FAILED=0
declare -a SUMMARY=()

_banner() { echo; echo "${BOLD}$1${NC}"; }

_record() { # <пункт> <ok|fail|warn> <причина>
  local item="$1" state="$2" reason="${3:-}"
  case "$state" in
    ok)
      PASSED=$((PASSED + 1))
      SUMMARY+=("${item}: ПРИНЯТО")
      echo "  ${GREEN}✅ ${item}: ПРИНЯТО${NC}"
      ;;
    warn)
      SUMMARY+=("${item}: ПРИНЯТО С ОГОВОРКОЙ (${reason})")
      echo "  ${YELLOW}⚠️  ${item}: ПРИНЯТО С ОГОВОРКОЙ (${reason})${NC}"
      ;;
    *)
      FAILED=$((FAILED + 1))
      SUMMARY+=("${item}: НЕ ПРИНЯТО (${reason})")
      echo "  ${RED}❌ ${item}: НЕ ПРИНЯТО (${reason})${NC}"
      ;;
  esac
}

# Прогон pytest по конкретным тестам. Печатает хвост вывода, чтобы причина
# провала была видна сразу, без второго запуска.
check_tests() { # <пункт> <описание> <node id…>
  local item="$1" title="$2"; shift 2
  local output code
  output="$("$PY" -m pytest -q -o addopts="" --tb=short "$@" 2>&1)"
  code=$?
  if [[ $code -eq 0 ]]; then
    _record "$item" ok
  else
    echo "$output" | tail -6
    _record "$item" fail "провал тестов ($title): $(echo "$output" | grep -E '^[0-9]+ (failed|passed)|failed' | tail -1)"
  fi
}

# ---------------------------------------------------------------- pytest: B1–B4
_banner "1. PoC-регрессии платежей (B1–B4)"
check_tests "B1" "вебхук не выдаёт доступ за чужую сумму" \
  tests/test_review_regressions.py::test_wata_webhook_does_not_exist \
  tests/test_review_regressions.py::test_paid_webhook_with_wrong_amount_does_not_grant
check_tests "B2" "оплата не теряется при недоступной панели" \
  tests/test_review_regressions.py::test_panel_outage_keeps_paid_order_in_grant_queue \
  tests/test_review_regressions.py::test_grant_is_retried_until_panel_gives_access \
  tests/test_review_regressions.py::test_short_grant_is_treated_as_failure
check_tests "B3" "автоплатёж не подтверждает чужой заказ" \
  tests/test_review_regressions.py::test_amount_without_kopecks_is_not_guessed_between_two_orders \
  tests/test_review_regressions.py::test_amount_without_kopecks_matches_the_only_candidate
check_tests "B4" "одна оплата — одна выдача" \
  tests/test_review_regressions.py::test_one_payment_grants_once_even_with_stale_order_object

# ------------------------------------------------------------- проба: WATA нет
_banner "2. Удаление WATA"
# «По коду» — это app/, scripts/ и конфиги. Тесты сюда не входят намеренно:
# в регрессии B1 адрес /payments/wata/webhook упоминается, чтобы prove 404.
# Сам этот скрипт исключён: ему положено называть адрес пробы.
WATA_HITS="$(grep -ri --include='*.py' --include='*.sh' --include='*.html' --include='*.yml' \
  --exclude='accept_review.sh' -e wata app scripts 2>/dev/null | grep -v '__pycache__' || true)"
WATA_ENV="$(grep -i -e wata .env.example 2>/dev/null || true)"
if [[ -z "$WATA_HITS" && -z "$WATA_ENV" ]]; then
  _record "WATA-код" ok
else
  echo "$WATA_HITS" | head -3
  echo "$WATA_ENV" | head -3
  _record "WATA-код" fail "упоминания WATA остались в коде или .env.example"
fi

# --------------------------------------------------------------- B6: гейт CI
_banner "3. Релизный гейт (B6)"
GATE_OK=1
[[ -f .github/workflows/tests.yml ]] || { GATE_OK=0; echo "  нет .github/workflows/tests.yml"; }
if grep -qE '^addopts\s*=\s*-q\s*$' pytest.ini 2>/dev/null; then
  GATE_OK=0
  echo "  pytest.ini прячет итоговую строку (addopts = -q)"
fi
if [[ $GATE_OK -eq 1 ]]; then
  _record "B6" ok
else
  _record "B6" fail "CI-воркфлоу или настройки pytest не в порядке"
fi

# ------------------------------------------------------- B7 и улучнения спринта
_banner "4. Ретенция и улучшения спринта (B7, H3, H5, H6, H8)"
check_tests "B7" "ретенция, анонимизация, удаление по запросу" \
  tests/test_retention.py
check_tests "H3" "частичный UNIQUE по копейкам" \
  tests/test_kopeck_signature.py
check_tests "H5" "PRAGMA WAL/FK/busy_timeout/synchronous" \
  tests/test_db_pragmas.py
check_tests "H6" "версия админ-сессии" \
  tests/test_admin_sessions.py
check_tests "H8" "лимиты по IP, кэш подписки, параллельный опрос" \
  tests/test_sub_rate_limit.py

# ------------------------------------------------------------- полный набор
_banner "5. Полный набор тестов"
FULL_OUT="$("$PY" -m pytest -q -o addopts="" --tb=line 2>&1)"
FULL_CODE=$?
echo "  $(echo "$FULL_OUT" | tail -1)"
if [[ $FULL_CODE -eq 0 ]]; then
  _record "ПОЛНЫЙ НАБОР" ok
else
  echo "$FULL_OUT" | grep -E "^(FAILED|ERROR)" | head -10
  _record "ПОЛНЫЙ НАБОР" fail "есть падения (код $FULL_CODE)"
fi

# ------------------------------------------------------------------ предполёт
_banner "6. Предполёт (готовность к запуску)"
PREFLIGHT_OUT="$(bash scripts/preflight.sh 2>&1)"
PREFLIGHT_CODE=$?
echo "  $(echo "$PREFLIGHT_OUT" | tail -1 | sed 's/\x1b\[[0-9;]*m//g')"
if [[ $PREFLIGHT_CODE -eq 0 ]]; then
  _record "ПРЕДПОЛЁТ" ok
elif [[ "${ACCEPT_LOCAL_PREFLIGHT_OK:-0}" == "1" ]]; then
  _record "ПРЕДПОЛЁТ" warn "локальная машина: сервис не запущен, см. причину выше"
else
  echo "$PREFLIGHT_OUT" | grep -E "❌" | head -5 | sed 's/\x1b\[[0-9;]*m//g'
  _record "ПРЕДПОЛЁТ" fail "preflight не прошёл (на этой машине сервис может быть просто не запущен)"
fi

# --------------------------------------------------------------------- пробы
_banner "7. Пробы"
PROBE_OUT="$("$PY" - <<'PY' 2>&1
import asyncio
import httpx

from app.web.sub import build_app


async def main() -> None:
    """POST на удалённый вебхук WATA должен отвечать 404."""
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/payments/wata/webhook", content=b"{}")
        print(response.status_code)


asyncio.run(main())
PY
)"
if [[ "$(echo "$PROBE_OUT" | tail -1)" == "404" ]]; then
  _record "ПРОБА 404" ok
else
  echo "  ответ: $(echo "$PROBE_OUT" | tail -2)"
  _record "ПРОБА 404" fail "POST /payments/wata/webhook отвечает не 404"
fi

# Дерево после прогона: тесты и предполёт не должны оставлять правок.
DIRTY="$(git status --short | head -10)"
if [[ -z "$DIRTY" ]]; then
  _record "ДЕРЕВО ЧИСТОЕ" ok
else
  echo "$DIRTY"
  _record "ДЕРЕВО ЧИСТОЕ" fail "после прогона есть незакоммиченные изменения"
fi

# --------------------------------------------------------------------- итог
_banner "ИТОГ"
for line in "${SUMMARY[@]}"; do
  echo "  $line"
done
echo
if [[ $FAILED -eq 0 ]]; then
  echo "${GREEN}${BOLD}ПРИНЯТО ВСЁ: пунктов — ${#SUMMARY[@]}, замечаний нет. Можно открывать PR.${NC}"
  exit 0
fi
echo "${RED}${BOLD}НЕ ПРИНЯТО: пунктов ${#SUMMARY[@]}, провалено ${FAILED}. PR открывать рано.${NC}"
exit 1
