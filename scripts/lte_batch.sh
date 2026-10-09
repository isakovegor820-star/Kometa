#!/usr/bin/env bash
# ============================================================================
# Батч адресов через сеть оператора — с проверкой ВЫХОДА в НАЧАЛЕ И В КОНЦЕ.
#
# Зачем отдельный скрипт. 09.10.2026 батч отработал через iPhone, а следующий
# запуск ушёл уже через домашний Wi-Fi: точка доступа отвалилась, macOS молча
# вернулся на домашнюю сеть. Внешне это выглядело как обычный валидный результат —
# на этом можно было построить ложный вывод. lte_probe.sh проверяет только
# «интернет есть», а надо проверять, ЧЕРЕЗ КОГО он есть.
#
# Здесь: выход фиксируется до и после, сравнивается, и при смене результат
# помечается недействительным (код 3), а не «на всякий случай принимается».
#
# Использование:
#   ./scripts/lte_batch.sh --targets .research/gate0-targets-batch2.txt \
#       --operator MTS --region "Саратов" --control https://ya.ru
# ============================================================================
set -u

export PATH="/usr/sbin:/sbin:/usr/bin:/bin:/usr/local/bin:${PATH:-}"
cd "$(dirname "$0")/.." || exit 2

TARGETS=""
REPORT=".research/probes.csv"
LABEL=""
OPERATOR="${LTE_OPERATOR:-MTS}"
REGION="${LTE_REGION:-}"
CONTROL="https://ya.ru"
REPEAT=3
FORCE=0
LOG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --targets)  TARGETS="${2:?}"; shift 2 ;;
        --report)   REPORT="${2:?}"; shift 2 ;;
        --label)    LABEL="${2:?}"; shift 2 ;;
        --operator) OPERATOR="${2:?}"; shift 2 ;;
        --region)   REGION="${2:?}"; shift 2 ;;
        --control)  CONTROL="${2:?}"; shift 2 ;;
        --repeat)   REPEAT="${2:?}"; shift 2 ;;
        --log)      LOG="${2:?}"; shift 2 ;;
        --force)    FORCE=1; shift ;;
        -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$TARGETS" ]] || { echo "Ошибка: нужен --targets <файл>" >&2; exit 2; }
[[ -f "$TARGETS" ]] || { echo "Файл не найден: $TARGETS" >&2; exit 2; }
[[ -n "$LABEL" ]] || LABEL="${OPERATOR} ${REGION:-не указан}"
[[ -n "$LOG" ]] || LOG=".research/batch-$(date '+%Y-%m-%d').log"
mkdir -p .research
export LTE_OPERATOR="$OPERATOR" LTE_REGION="$REGION"

exec > >(tee -a "$LOG") 2>&1

egress_iface() { route -n get default 2>/dev/null | awk '/interface:/{print $2}'; }
egress_ip()    { ipconfig getifaddr "$(egress_iface)" 2>/dev/null || true; }
is_mobile_ok() { # типовые диапазоны теринга
    case "${1:-}" in
        172.20.10.*|192.168.42.*|192.168.43.*) return 0 ;;
    esac
    # Некоторые телефоны раздают из 10.x. Проверено 09.10.2026: точка доступа
    # Билайна выдала Mac адрес 10.116.143.9 (внешний адрес 217.118.90.154,
    # AS16345 PJSC Vimpelcom — оператор подтверждён отдельной проверкой по внешнему IP).
    # Ослаблять проверку «на любой 10.x» нельзя: под неё попадёт и корпоративный VPN.
    # Поэтому такой выход принимается ТОЛЬКО при явном подтверждении:
    #   LTE_EGRESS_CONFIRMED=10.116.143.9 ./scripts/lte_batch.sh ...
    if [[ -n "${LTE_EGRESS_CONFIRMED:-}" && "${1:-}" == "$LTE_EGRESS_CONFIRMED" ]]; then
        return 0
    fi
    return 1
}

IFACE0="$(egress_iface)"; IP0="$(egress_ip)"
echo "=============================================================================="
echo " БАТЧ ЧЕРЕЗ СЕТЬ ОПЕРАТОРА"
echo " Время      : $(date '+%Y-%m-%d %H:%M:%S')"
echo " Метка      : ${LABEL}"
echo " Выход ДО   : ${IFACE0:-?} (${IP0:-без адреса})"
echo " Цели       : ${TARGETS}"
echo " Контроль   : ${CONTROL}"
echo " Расход     : 0 ₽"
echo "=============================================================================="
echo

if ! is_mobile_ok "$IP0"; then
    echo "❌ Выход ${IFACE0} (${IP0:-нет адреса}) НЕ похож на сеть телефона."
    echo "   Батч через домашний канал измерил бы не оператора. Останавливаюсь."
    echo "   Проверка: ipconfig getifaddr en0 — ожидается 172.20.10.x или 192.168.4x.x"
    [[ "$FORCE" -eq 1 ]] || exit 1
    echo "   ⚠️  --force: продолжаю, результат будет помечен как НЕДЕЙСТВИТЕЛЬНЫЙ."
fi

./scripts/lte_probe.sh --pool "$TARGETS" --repeat "$REPEAT" \
    --control "$CONTROL" --report "$REPORT" --label "$LABEL"
PROBE_RC=$?

echo
echo "------------------------------------------------------------------------------"
IFACE1="$(egress_iface)"; IP1="$(egress_ip)"
echo " Выход ПОСЛЕ : ${IFACE1:-?} (${IP1:-без адреса})"

RESULT="ДЕЙСТВИТЕЛЕН"
if [[ "$IP0" != "$IP1" ]]; then
    RESULT="НЕДЕЙСТВИТЕЛЕН: выход сменился во время батча (${IP0} → ${IP1})"
fi
if ! is_mobile_ok "$IP0"; then
    RESULT="НЕДЕЙСТВИТЕЛЕН: выход не был мобильным (${IP0})"
fi

printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "${LABEL//,/;}" "$OPERATOR" "${REGION//,/;}" \
    "egress" "start:${IFACE0}/${IP0}" "" "${RESULT%%:*}" \
    "end:${IFACE1}/${IP1};probe_rc=${PROBE_RC}" >> "$REPORT"

echo " ИТОГ        : ${RESULT}"
echo " CSV         : ${REPORT}"
echo " Лог         : ${LOG}"
echo "=============================================================================="

[[ "$RESULT" == "ДЕЙСТВИТЕЛЕН" ]] || exit 3
exit "$PROBE_RC"
