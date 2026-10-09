#!/usr/bin/env bash
# ============================================================================
# ЭТАП 0 через телефон, НЕ меняя маршрут по умолчанию на Mac.
#
# Зачем отдельный скрипт. Если переключить весь Mac на телефон, то при
# включённом режиме ограничений у оператора Mac потеряет интернет ЦЕЛИКОМ —
# включая сессию, в которой мы разбираем результаты замера. Мы это проверили
# на себе: маршрут по умолчанию уходил в VPN, и все адреса давали rc=0.
#
# Поэтому на время замера добавляются ТОЧЕЧНЫЕ маршруты (/32) только до
# измеряемых адресов. Всё остальное (в том числе связь с моделью) продолжает
# идти через домашний канал. Замер при этом честный: пакеты до целевых
# адресов физически уходят в SIM оператора.
#
# Требует прав root — правит таблицу маршрутизации. Пароль вводит владелец.
#
# Использование:
#   sudo ./scripts/lte_gate0_via_phone.sh --operator MTS --region "Саратов"
#   sudo ./scripts/lte_gate0_via_phone.sh --operator MTS --region "Саратов" --via en5
#   sudo ./scripts/lte_gate0_via_phone.sh --list          # только показать план
# ============================================================================
set -u

# Пути явно: при запуске через osascript «do shell script … with administrator
# privileges» окружение минимальное, и route/ifconfig/ipconfig можно не найти.
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:/usr/local/bin:${PATH:-}"

cd "$(dirname "$0")/.." || exit 2

VIA=""
OPERATOR="MTS"
REGION=""
LABEL=""
LIST_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --via)      VIA="${2:?}"; shift 2 ;;
        --operator) OPERATOR="${2:?}"; shift 2 ;;
        --region)   REGION="${2:?}"; shift 2 ;;
        --label)    LABEL="${2:?}"; shift 2 ;;
        --list)     LIST_ONLY=1; shift ;;
        -h|--help)  sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$VIA" ]]; then
    # Автопоиск интерфейса телефона по типовым диапазонам теринга.
    for i in $(ifconfig -l); do
        a="$(ipconfig getifaddr "$i" 2>/dev/null || true)"
        case "$a" in
            172.20.10.*|192.168.42.*|192.168.43.*) VIA="$i"; break ;;
        esac
    done
fi

if [[ -z "$VIA" ]]; then
    echo "❌ Не найден интерфейс телефона."
    echo "   Проверьте: телефон подключён кабелем, на нём включена «Точка доступа»,"
    echo "   Wi-Fi на телефоне ВЫКЛЮЧЕН, на вопрос «Доверять этому компьютеру?» — «Доверять»."
    echo "   Посмотреть интерфейсы: ifconfig -l"
    exit 2
fi

VIA_IP="$(ipconfig getifaddr "$VIA" 2>/dev/null || true)"
if [[ -z "$VIA_IP" ]]; then
    if ifconfig "$VIA" >/dev/null 2>&1; then
        echo "❌ Интерфейс ${VIA} существует, но адреса нет — телефон не отдал сеть." >&2
        echo "   Проверьте на телефоне «Точку доступа» и выключенный Wi-Fi." >&2
    else
        echo "❌ Интерфейса ${VIA} нет — телефон не подключён (или другое имя интерфейса)." >&2
        echo "   Посмотреть список: ifconfig -l" >&2
    fi
    exit 2
fi

# Шлюз = телефон. Обычно .1 в подсети теринга.
GW="$(ipconfig getoption "$VIA" router 2>/dev/null || true)"
if [[ -z "$GW" ]]; then
    GW="${VIA_IP%.*}.1"
fi

echo "=============================================================================="
echo " ЭТАП 0 через телефон (точечные маршруты, маршрут по умолчанию не трогаем)"
echo "   Интерфейс телефона : ${VIA}  (${VIA_IP})"
echo "   Шлюз (телефон)     : ${GW}"
echo "   Оператор/регион    : ${OPERATOR} / ${REGION:-не указан}"
echo "=============================================================================="
echo

# Список адресов замера — единый источник правды в lte_gate0.sh.
# Цикл, а не mapfile: в штатном macOS bash 3.2 mapfile не существует.
TARGETS=()
while IFS= read -r line; do
    [[ -n "$line" ]] && TARGETS+=("$line")
done < <(./scripts/lte_gate0.sh --print-targets 2>/dev/null | sort -u)
if [[ "${#TARGETS[@]}" -eq 0 ]]; then
    echo "❌ Не удалось получить список адресов из ./scripts/lte_gate0.sh --print-targets" >&2
    exit 2
fi

echo "Адресов в замере: ${#TARGETS[@]}"
for ip in "${TARGETS[@]}"; do
    printf '   %-16s маршрут сейчас: %s\n' "$ip" "$(route -n get "$ip" 2>/dev/null | awk '/interface:/{print $2}' || echo '?')"
done
echo

if [[ "$LIST_ONLY" -eq 1 ]]; then
    echo "(режим --list: маршруты не менялись, замер не запускался)"
    exit 0
fi

if [[ "$EUID" -ne 0 ]]; then
    echo "❌ Нужны права root: скрипт правит таблицу маршрутизации. Запустите с sudo:" >&2
    echo "   sudo ./scripts/lte_gate0_via_phone.sh --operator ${OPERATOR} --region \"${REGION:-<город>}\"" >&2
    exit 2
fi

RUN_USER="${SUDO_USER:-}"
# При запуске через osascript «with administrator privileges» SUDO_USER пуст.
# Тогда берём владельца рабочей копии — замер должен писать файлы от него, а не от root.
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    RUN_USER="$(stat -f %Su . 2>/dev/null || true)"
fi
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    echo "❌ Не удалось определить пользователя для запуска замера (SUDO_USER пуст)." >&2
    echo "   Запустите через sudo из своей учётной записи." >&2
    exit 2
fi

ADDED=()
cleanup() {
    echo
    echo "Снимаю точечные маршруты (${#ADDED[@]} шт.)..."
    for ip in "${ADDED[@]:-}"; do
        [[ -n "$ip" ]] && route delete -host "$ip" >/dev/null 2>&1
    done
    remaining="$(route -n get "${TARGETS[0]}" 2>/dev/null | awk '/interface:/{print $2}')"
    echo "Проверка: маршрут до ${TARGETS[0]} теперь через ${remaining:-?} (должен вернуться домашний)"
}
trap cleanup EXIT

echo "Ставлю точечные маршруты через ${VIA} (${GW}):"
FAILED=0
for ip in "${TARGETS[@]}"; do
    if route add -host "$ip" "$GW" >/dev/null 2>&1; then
        ADDED+=("$ip")
    else
        # Маршрут мог уже существовать — проверим, куда он ведёт.
        cur="$(route -n get "$ip" 2>/dev/null | awk '/interface:/{print $2}')"
        if [[ "$cur" == "$VIA" ]]; then
            ADDED+=("$ip")
        else
            echo "   ❌ не удалось: ${ip} (маршрут ведёт в ${cur:-?})"
            FAILED=$((FAILED + 1))
        fi
    fi
done
echo "   поставлено маршрутов: ${#ADDED[@]}, ошибок: ${FAILED}"
echo

echo "Проверка маршрутов (должны идти через ${VIA}):"
BAD=0
for ip in "${TARGETS[@]}"; do
    cur="$(route -n get "$ip" 2>/dev/null | awk '/interface:/{print $2}')"
    if [[ "$cur" != "$VIA" ]]; then
        echo "   ❌ ${ip} → ${cur:-нет маршрута}"
        BAD=$((BAD + 1))
    fi
done
if [[ "$BAD" -eq 0 ]]; then
    echo "   ✅ все ${#TARGETS[@]} адресов пойдут через ${VIA} — замер пойдёт в сеть оператора"
else
    echo "   ❌ ${BAD} адресов НЕ идут через телефон — замер будет недействителен, останавливаюсь"
    exit 1
fi
echo
echo "Маршрут по умолчанию не тронут: $(route -n get default 2>/dev/null | awk '/interface:/{print $2}') — сессия управления жива."
echo

echo "=============================================================================="
echo " Запускаю замер от пользователя ${RUN_USER}"
echo "=============================================================================="
sudo -u "$RUN_USER" env \
    LTE_OPERATOR="$OPERATOR" LTE_REGION="$REGION" \
    ./scripts/lte_gate0.sh --operator "$OPERATOR" --region "${REGION:-не указан}" \
    --label "${LABEL:-$OPERATOR ${REGION}}" --via "$VIA"
rc=$?
echo "Замер завершён, код: ${rc}"
exit "$rc"
