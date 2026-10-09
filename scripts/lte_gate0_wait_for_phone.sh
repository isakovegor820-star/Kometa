#!/usr/bin/env bash
# ============================================================================
# Ждёт, пока Mac окажется в сети телефона, и только тогда запускает замер.
#
# Зачем. Если подключиться к точке доступа телефона по Wi-Fi, маршрут по
# умолчанию на Mac сменится на оператора. При включённом режиме ограничений
# это означает, что Mac потеряет обычный интернет — в том числе канал, по
# которому идёт разбор результатов. Поэтому замер запускается ФОНОМ и пишет
# всё на диск: даже если связь с моделью прервётся, данные не потеряются.
#
# Использование:
#   ./scripts/lte_gate0_wait_for_phone.sh --operator MTS --region "Саратов"
#   ./scripts/lte_gate0_wait_for_phone.sh --timeout 600 --region "Саратов"
# ============================================================================
set -u

cd "$(dirname "$0")/.." || exit 2

OPERATOR="MTS"
REGION="Саратов"
TIMEOUT=900
POLL=5
BATCH=""
CONTROL="https://ya.ru"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --operator) OPERATOR="${2:?}"; shift 2 ;;
        --region)   REGION="${2:?}"; shift 2 ;;
        --timeout)  TIMEOUT="${2:?}"; shift 2 ;;
        --batch)    BATCH="${2:?}"; shift 2 ;;
        --control)  CONTROL="${2:?}"; shift 2 ;;
        -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

WAIT_LOG=".research/gate0-wait.log"
mkdir -p .research

is_phone_ip() {
    case "${1:-}" in
        172.20.10.*|192.168.42.*|192.168.43.*) return 0 ;;
        *) return 1 ;;
    esac
}
egress() {
    local i; i="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
    printf '%s|%s' "$i" "$(ipconfig getifaddr "$i" 2>/dev/null || true)"
}

echo "[$(date '+%H:%M:%S')] Жду сеть телефона (до ${TIMEOUT} с)..." | tee -a "$WAIT_LOG"

IFACE=""; IP=""
waited=0

# Если точка доступа УЖЕ подключена — сначала ждём её отключения.
# Иначе замер уйдёт в ТЕКУЩУЮ точку, а метка будет про другую: именно так
# 09.10.2026 «замер по Энгельсу» мог бы молча измерить Саратов.
CUR="$(egress)"; IFACE="${CUR%%|*}"; IP="${CUR#*|}"
if is_phone_ip "$IP"; then
    echo "[$(date '+%H:%M:%S')] Внимание: точка доступа уже подключена (${IP})." | tee -a "$WAIT_LOG"
    echo "             Жду её отключения, чтобы не померить не ту точку." | tee -a "$WAIT_LOG"
    while [[ "$waited" -lt "$TIMEOUT" ]]; do
        CUR="$(egress)"; IFACE="${CUR%%|*}"; IP="${CUR#*|}"
        is_phone_ip "$IP" || { echo "[$(date '+%H:%M:%S')] Точка доступа отключена (${IP:-без адреса}) — теперь жду новую." | tee -a "$WAIT_LOG"; break; }
        sleep "$POLL"; waited=$((waited + POLL))
    done
fi

while [[ "$waited" -lt "$TIMEOUT" ]]; do
    CUR="$(egress)"; IFACE="${CUR%%|*}"; IP="${CUR#*|}"
    if is_phone_ip "$IP"; then
        echo "[$(date '+%H:%M:%S')] Сеть телефона найдена: ${IFACE} (${IP})" | tee -a "$WAIT_LOG"
        break
    fi
    sleep "$POLL"; waited=$((waited + POLL))
done

if [[ "$waited" -ge "$TIMEOUT" ]]; then
    echo "[$(date '+%H:%M:%S')] Сеть телефона не появилась за ${TIMEOUT} с — замер не запускаю." | tee -a "$WAIT_LOG"
    exit 1
fi

# Проверяем, что это действительно сеть телефона, а не домашний роутер с таким же диапазоном.
echo "[$(date '+%H:%M:%S')] Запускаю замер. Если связь с моделью прервётся — данные пишутся на диск." | tee -a "$WAIT_LOG"
if [[ -n "$BATCH" ]]; then
    ./scripts/lte_batch.sh --targets "$BATCH" --operator "$OPERATOR" --region "$REGION" \
        --label "$OPERATOR $REGION" --control "$CONTROL"
else
    ./scripts/lte_gate0.sh --operator "$OPERATOR" --region "$REGION" --label "$OPERATOR $REGION"
fi
rc=$?
echo "[$(date '+%H:%M:%S')] Замер завершён, код ${rc}. Отчёт: .research/probes.csv, лог: .research/*.log" | tee -a "$WAIT_LOG"
exit "$rc"
