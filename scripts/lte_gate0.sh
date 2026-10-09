#!/usr/bin/env bash
# ============================================================================
# ЭТАП 0: бесплатный замер «включён ли режим ограничений у оператора».
#
# Ничего не покупает. Ничего не меняет ни на телефоне, ни на серверах.
# Обёртка над scripts/lte_probe.sh — добавляет то, из-за чего замеры чаще
# всего оказываются мусором:
#
#   1) предполётная проверка: трафик ДЕЙСТВИТЕЛЬНО идёт через мобильную сеть
#      (ноутбук подключён к телефону, Wi-Fi на ноутбуке и телефоне выключен);
#   2) контрольный URL выбирается из тех, что РЕАЛЬНО отвечают: под режимом
#      ограничений внешний контроль (ifconfig.co) сам может быть закрыт, и
#      скрипт написал бы «замер недостоверен» на ровном месте;
#   3) контрольная ПАРА: разрешённый адрес + заведомо заблокированный
#      зарубежный адрес с живым TLS-слушателем (наши ноды отвечают на 443);
#   4) гипотеза про IPv6 — проверяется до того, как на неё потратят деньги.
#
# Запуск (с ноутбука, подключённого к телефону):
#   ./scripts/lte_gate0.sh --operator MTS --region "Казань"
#   ./scripts/lte_gate0.sh --operator MTS --region "Казань" --neg-host 77.221.147.230
#
# Результат: .research/probes.csv (машинный отчёт) + .research/gate0-<дата>.log
# ============================================================================
set -u

cd "$(dirname "$0")/.." || exit 2

OPERATOR="${LTE_OPERATOR:-MTS}"
REGION="${LTE_REGION:-}"
LABEL="${LABEL:-}"
NEG_HOST="${NEG_HOST:-150.241.106.75}"   # наша DE-нода: TLS на 443 есть, в белом списке НЕТ
REPORT=".research/probes.csv"
LOG=""
FORCE=0
SKIP_IPV6=0
PRINT_TARGETS=0
VIA_IFACE=""

# Кандидаты в контрольные URL. Порядок = приоритет: сначала то, что под
# режимом ограничений обязано работать (крупные российские сервисы), потом внешний.
CONTROL_CANDIDATES=(
    "https://ya.ru"
    "https://mail.ru"
    "https://www.sberbank.ru"
    "https://www.gosuslugi.ru"
    "https://ifconfig.co"
)

#: Контроль «заведомо разрешённого» адреса ПО IP (без DNS). Все четыре адреса
#: проверены: входят в CIDR-список белого списка и отвечают rc=0 по голому IP,
#: то есть TLS поднимается без SNI. Нужны для режима --via: там контроль обязан
#: идти по тому же маршруту, что и замер, иначе он проверит не сеть оператора.
VIA_CONTROL_IPS=(
    "5.255.255.242|ya.ru, в CIDR 5.255.255.0/24"
    "90.156.232.4|mail.ru, 29 подтверждённых в /24"
    "89.221.239.1|mail.ru, 9 подтверждённых в /24"
    "185.180.201.1|mail.ru, 5 подтверждённых в /24"
)

# Адреса-кандидаты (все — плотные подсети, подтверждённые ОБЕИМИ базами,
# проверено ./scripts/whitelist_check.sh 09.10.2026). Слушателя на них нет —
# это нормально: rc=28 (тишина) и есть признак «L3 пропускает».
CANDIDATES=(
    "91.215.42.5|DDoS-Guard 253/256, есть и в CIDR"
    "176.57.66.5|DDoS-Guard 250/256, НЕТ в CIDR"
    "195.208.66.5|251/256, есть и в CIDR"
    "95.181.181.5|EdgeCenter 248/256, есть и в CIDR"
)

# Подсети для проверки «эффекта соседа» (пустые .1/.77/.254).
RANGES=(
    "91.215.42.0/24"
    "176.57.66.0/24"
)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --operator) OPERATOR="${2:?}"; shift 2 ;;
        --region)   REGION="${2:?}"; shift 2 ;;
        --label)    LABEL="${2:?}"; shift 2 ;;
        --neg-host) NEG_HOST="${2:?}"; shift 2 ;;
        --report)   REPORT="${2:?}"; shift 2 ;;
        --log)      LOG="${2:?}"; shift 2 ;;
        --force)    FORCE=1; shift ;;
        --print-targets) PRINT_TARGETS=1; shift ;;
        --via)      VIA_IFACE="${2:?}"; shift 2 ;;
        --skip-ipv6) SKIP_IPV6=1; shift ;;
        -h|--help)  sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

mkdir -p .research

# --print-targets: выдать список ВСЕХ адресов, до которых пойдёт замер.
# Нужно обёртке lte_gate0_via_phone.sh: она ставит точечные маршруты через
# телефон, чтобы НЕ переключать маршрут по умолчанию (иначе при работающем
# режиме ограничений Mac потерял бы связь целиком). Печатает и выходит.
if [[ "$PRINT_TARGETS" -eq 1 ]]; then
    for entry in "${CANDIDATES[@]}"; do printf '%s\n' "${entry%%|*}"; done
    for r in "${RANGES[@]}"; do
        base="${r%%/*}"; base="${base%.*}"
        for s in 1 77 254; do printf '%s.%s\n' "$base" "$s"; done
    done
    printf '%s\n' "$NEG_HOST"
    for entry in "${VIA_CONTROL_IPS[@]}"; do printf '%s\n' "${entry%%|*}"; done
    for url in "${CONTROL_CANDIDATES[@]}"; do
        host="${url#https://}"; host="${host%%/*}"
        python3 - "$host" <<'PY' 2>/dev/null || true
import socket, sys
try:
    for info in socket.getaddrinfo(sys.argv[1], 443, socket.AF_INET):
        print(info[4][0])
except OSError:
    pass
PY
    done
    exit 0
fi

ISO="$(date '+%Y-%m-%d')"
TS="$(date '+%Y-%m-%d %H:%M:%S')"
[[ -n "$LOG" ]] || LOG=".research/gate0-${ISO}.log"
: > "$LOG"

# Всё, что печатается, попадает и в лог — журнал этапа собирается сам.
exec > >(tee -a "$LOG") 2>&1

if [[ -z "$REGION" ]]; then
    printf 'Город (для метки замера, напр. «Казань»): '
    read -r REGION || REGION="не указан"
    [[ -n "$REGION" ]] || REGION="не указан"
fi
[[ -n "$LABEL" ]] || LABEL="${OPERATOR} ${REGION}"
export LTE_OPERATOR="$OPERATOR" LTE_REGION="$REGION"

PASS_N=0; FAIL_N=0; WARN_N=0
ok()   { PASS_N=$((PASS_N + 1)); printf '  ✅ %s\n' "$1"; }
bad()  { FAIL_N=$((FAIL_N + 1)); printf '  ❌ %s\n' "$1"; }
warn() { WARN_N=$((WARN_N + 1)); printf '  ⚠️  %s\n' "$1"; }

# Запись в общий CSV тем же форматом, что у lte_probe.sh.
csv_row() { # csv_row <режим> <цель> <группа> <вердикт> <детали>
    [[ -f "$REPORT" ]] || printf '%s\n' "время,метка,оператор,регион,режим,цель,группа,вердикт,детали" >> "$REPORT"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$(date '+%Y-%m-%d %H:%M')" "${LABEL//,/;}" "$OPERATOR" "${REGION//,/;}" \
        "$1" "$2" "$3" "$4" "${5//,/;}" >> "$REPORT"
}

# Одно подключение curl → rc. Ровно тот же идиом, что в lte_probe.sh.
hit_rc() { # hit_rc <url или https://host:port>
    local rc=0
    curl -sk -o /dev/null -m 8 "$1" 2>/dev/null || rc=$?
    printf '%s' "$rc"
}

echo "=============================================================================="
echo " ЭТАП 0 — бесплатный замер. Оператор: ${OPERATOR}, регион: ${REGION}"
echo " Метка отчёта: ${LABEL}"
echo " Время: ${TS}"
echo " Лог: ${LOG}   CSV: ${REPORT}"
echo " Расход: 0 ₽. Ничего не покупается и не меняется."
echo "=============================================================================="
echo

# --- 1. Предполётная проверка: трафик идёт через мобильную сеть? --------------
echo "1. ПРЕДПОЛЁТНАЯ ПРОВЕРКА (без неё замер недействителен)"
echo "------------------------------------------------------------------------------"

DEF_LINE="$(route -n get default 2>/dev/null)"
IFACE="$(printf '%s\n' "$DEF_LINE" | awk '/interface:/{print $2}')"
GW="$(printf '%s\n' "$DEF_LINE" | awk '/gateway:/{print $2}')"
WIFI_DEV="$(networksetup -listallhardwareports 2>/dev/null | awk '/Hardware Port: Wi-Fi/{getline; print $2}')"
WIFI_PWR="$(networksetup -getairportpower "$WIFI_DEV" 2>/dev/null | awk '{print $NF}')"
IFACE_IP="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"

echo "  Интерфейс по умолчанию : ${IFACE:-не определён}  (${IFACE_IP:-без адреса})"
echo "  Шлюз                   : ${GW:-не определён}"
echo "  Wi-Fi интерфейс        : ${WIFI_DEV:-не найден} — питание: ${WIFI_PWR:-неизвестно}"
WIFI_SSID="$(networksetup -getairportnetwork "$WIFI_DEV" 2>/dev/null | sed 's/^Current Wi-Fi Network: //')"
case "$WIFI_SSID" in *"not associated"*|*"not associated with an AirPort"*) WIFI_SSID="" ;; esac
[[ -n "$WIFI_SSID" ]] && echo "  Wi-Fi сеть             : ${WIFI_SSID}"

# Точка доступа ТЕЛЕФОНА — это тоже мобильная сеть, браковать её нельзя.
# Признаки: имя сети как у телефона либо адрес из типового диапазона теринга.
HOTSPOT=0
case "$WIFI_SSID" in
    *iPhone*|*iPad*|*Android*|*Galaxy*|*Pixel*|*Hotspot*|*hotspot*|*Xiaomi*|*Redmi*|\
    *HUAWEI*|*Honor*|*realme*|*POCO*|*vivo*|*OPPO*|*ZTE*|*МТС*|*MTS*|*МегаФон*|\
    *MegaFon*|*Билайн*|*Beeline*|*tele2*|*Tele2*|*Yota*) HOTSPOT=1 ;;
esac
case "${IFACE_IP:-}" in
    172.20.10.*|192.168.42.*|192.168.43.*) HOTSPOT=1 ;;
esac

EGRESS_OK=1
if [[ -z "$IFACE" ]]; then
    bad "не удалось определить интерфейс по умолчанию — нет сети?"
    EGRESS_OK=0
elif [[ -n "$WIFI_DEV" && "$IFACE" == "$WIFI_DEV" && "$HOTSPOT" -eq 0 ]]; then
    bad "трафик идёт через Wi-Fi «${WIFI_SSID:-неизвестная сеть}» (${IFACE}) — это НЕ сеть телефона"
    echo "      ЗАМЕР НЕДЕЙСТВИТЕЛЕН. Подключитесь к точке доступа ТЕЛЕФОНА (или по кабелю),"
    echo "      и обязательно выключите Wi-Fi НА ТЕЛЕФОНЕ — иначе он раздаёт домашний Wi-Fi, а не SIM."
    EGRESS_OK=0
elif [[ -n "$WIFI_DEV" && "$IFACE" == "$WIFI_DEV" && "$HOTSPOT" -eq 1 ]]; then
    ok "трафик идёт через точку доступа телефона «${WIFI_SSID}» (${IFACE_IP}) — это мобильная сеть"
    echo "      Проверьте на телефоне: Wi-Fi ВЫКЛЮЧЕН, горит 4G/5G. Иначе раздаётся домашний Wi-Fi."
elif [[ "$IFACE" == utun* || "$IFACE" == ipsec* || "$IFACE" == ppp* || "$IFACE" == tun* || "$IFACE" == tap* || "$IFACE" == wg* ]]; then
    : # туннель: вердикт выносит проверка VPN ниже, здесь молчим
else
    ok "интерфейс по умолчанию — не Wi-Fi (${IFACE})"
fi

if [[ "$HOTSPOT" -eq 0 && -n "${IFACE_IP:-}" && "$IFACE" != "$WIFI_DEV" ]]; then
    case "$IFACE_IP" in
        172.20.10.*)  ok "похоже на USB-тетеринг iPhone (172.20.10.0/28) — трафик пойдёт через SIM" ;;
        192.168.42.*|192.168.43.*) ok "похоже на USB-тетеринг Android — трафик пойдёт через SIM" ;;
        *) warn "адрес ${IFACE_IP} не похож на типовой теринг — убедитесь, что это мобильная сеть телефона" ;;
    esac
fi

if [[ "$WIFI_PWR" == "On" ]]; then
    warn "Wi-Fi на ноутбуке включён — выключите, чтобы исключить утечку трафика мимо SIM"
fi

# VPN — самый опасный источник ложного «всё работает»: через туннель любой
# адрес отвечает, и замер превращается в фикцию. Проверено на своей машине:
# при активном VPN маршрут по умолчанию ушёл в utun4, и все адреса дали rc=0.
case "$IFACE" in
    utun*|ipsec*|ppp*|tun*|tap*|wg*)
        bad "маршрут по умолчанию идёт через туннель ${IFACE} — ВКЛЮЧЁН VPN"
        echo "      Через VPN ЛЮБОЙ адрес «работает» — замер будет фикцией."
        echo "      Выключите VPN и повторите. Проверить: scutil --nc list | grep Connected"
        EGRESS_OK=0 ;;
esac
VPN_IFACES="$(ifconfig 2>/dev/null | grep -c '^utun' || true)"
if [[ "${VPN_IFACES:-0}" -gt 0 && "$EGRESS_OK" -eq 1 ]]; then
    warn "есть интерфейсы utun (${VPN_IFACES}), но маршрут по умолчанию не через них — проверьте, что VPN выключен"
fi

# --- Режим --via: замер идёт через телефон точечными маршрутами ---------------
# Маршрут по умолчанию остаётся домашним (сессия управления не рвётся), а
# адреса замера уходят в SIM. Здесь проверяем, что маршруты РЕАЛЬНО поставлены:
# без этого замер ушёл бы через домашний канал и дал бы ложное «всё проходит».
if [[ -n "$VIA_IFACE" ]]; then
    echo
    echo "  Режим --via (точечные маршруты через телефон):"
    if ! ifconfig "$VIA_IFACE" >/dev/null 2>&1; then
        bad "интерфейс ${VIA_IFACE} не найден"
        EGRESS_OK=0
    else
        VIA_IP="$(ipconfig getifaddr "$VIA_IFACE" 2>/dev/null || true)"
        ok "интерфейс телефона ${VIA_IFACE} (${VIA_IP:-без адреса})"
        PROBE_ONE="${CANDIDATES[0]%%|*}"
        R_ONE="$(route -n get "$PROBE_ONE" 2>/dev/null | awk '/interface:/{print $2}')"
        R_NEG="$(route -n get "$NEG_HOST" 2>/dev/null | awk '/interface:/{print $2}')"
        R_CTL="$(route -n get "${VIA_CONTROL_IPS[0]%%|*}" 2>/dev/null | awk '/interface:/{print $2}')"
        echo "      ${PROBE_ONE} → ${R_ONE:-нет маршрута} · ${NEG_HOST} → ${R_NEG:-нет маршрута} · контроль ${VIA_CONTROL_IPS[0]%%|*} → ${R_CTL:-нет маршрута}"
        if [[ "$R_ONE" == "$VIA_IFACE" && "$R_NEG" == "$VIA_IFACE" && "$R_CTL" == "$VIA_IFACE" ]]; then
            ok "замер, негативный контроль и контроль идут через ${VIA_IFACE} — это сеть оператора"
            echo "      Маршрут по умолчанию (${IFACE}) не тронут: связь с моделью не пострадает."
            EGRESS_OK=1
        else
            bad "часть адресов НЕ идёт через ${VIA_IFACE} — замер был бы через домашний канал"
            echo "      Запустите: sudo ./scripts/lte_gate0_via_phone.sh --operator ${OPERATOR} --region \"${REGION}\""
            EGRESS_OK=0
        fi
    fi
fi

if [[ "$EGRESS_OK" -eq 0 && "$FORCE" -eq 0 ]]; then
    echo
    echo "  СТОП: сначала подключите ноутбук к телефону (Wi-Fi на телефоне ВЫКЛЮЧЕН),"
    echo "        затем повторите. Для отладочного прогона есть флаг --force."
    exit 1
fi
echo

# --- 2. Какой контрольный URL реально отвечает --------------------------------
echo "2. ВЫБОР КОНТРОЛЬНОГО URL (под ограничениями внешний контроль может быть закрыт)"
echo "------------------------------------------------------------------------------"
CONTROL=""
if [[ -n "$VIA_IFACE" ]]; then
    # Режим --via: контроль привязан к IP, а не к имени. Иначе DNS мог бы выдать
    # адрес, до которого нет точечного маршрута, и контроль ушёл бы мимо SIM —
    # то есть проверял бы домашний канал вместо сети оператора.
    echo "  Режим --via: контроль по IP (маршрут предсказуем, DNS не влияет)."
    for entry in "${VIA_CONTROL_IPS[@]}"; do
        cip="${entry%%|*}"; cnote="${entry#*|}"
        url="https://${cip}/"
        rc="$(hit_rc "$url")"
        if [[ "$rc" == "0" ]]; then
            mark="✅ rc=0"
            [[ -n "$CONTROL" ]] || CONTROL="$url"
        else
            mark="❌ rc=${rc}"
        fi
        printf '  %-24s %-30s %s\n' "$url" "(${cnote})" "$mark"
        csv_row "control-url" "$url" "via:${VIA_IFACE}" \
            "$([[ "$rc" == "0" ]] && echo ПРОХОДИТ || echo БЛОК)" "rc=${rc}"
    done
else
    for url in "${CONTROL_CANDIDATES[@]}"; do
    rc="$(hit_rc "$url")"
    if [[ "$rc" == "0" ]]; then
        mark="✅ rc=0"
        [[ -n "$CONTROL" ]] || CONTROL="$url"
    else
        mark="❌ rc=${rc}"
    fi
    printf '  %-28s %s\n' "$url" "$mark"
    csv_row "control-url" "$url" "" "$([[ "$rc" == "0" ]] && echo ПРОХОДИТ || echo БЛОК)" "rc=${rc}"
    done
fi

if [[ -z "$CONTROL" ]]; then
    echo
    bad "ни один контрольный URL не ответил — сеть мертва ЛИБО это не режим списков, а полное отключение"
    echo "      Меряем дальше без контроля: результат будет помечен как недостоверный."
    CONTROL="https://ya.ru"
else
    ok "контроль: ${CONTROL}"
fi
echo

# --- 3. Контрольная пара ------------------------------------------------------
echo "3. КОНТРОЛЬНАЯ ПАРА (начало батча)"
echo "------------------------------------------------------------------------------"
CTRL_RC="$(hit_rc "$CONTROL")"
if [[ "$CTRL_RC" == "0" ]]; then
    ok "разрешённый адрес ${CONTROL} → rc=0"
else
    bad "разрешённый адрес ${CONTROL} → rc=${CTRL_RC} (контроль не сработал — замер будет недостоверен)"
fi
NEG_RC="$(hit_rc "https://${NEG_HOST}:443")"
case "$NEG_RC" in
    0|60|92) ok "заблокированный ${NEG_HOST}:443 → rc=${NEG_RC} — TLS состоялся, значит фильтра НЕТ (адрес проходит)" ;;
    7)       ok "заблокированный ${NEG_HOST}:443 → rc=7 (RST) — фильтр РАБОТАЕТ, это ожидаемый признак режима" ;;
    28)      warn "заблокированный ${NEG_HOST}:443 → rc=28 (тишина) — сервер мог не ответить; контроль слабый" ;;
    *)       warn "заблокированный ${NEG_HOST}:443 → rc=${NEG_RC} — неоднозначно" ;;
esac
csv_row "control-pair" "${CONTROL}" "allowed" "$([[ "$CTRL_RC" == "0" ]] && echo ПРОХОДИТ || echo БЛОК)" "rc=${CTRL_RC}"
csv_row "control-pair" "${NEG_HOST}:443" "blocked-expected" "$([[ "$NEG_RC" == "7" ]] && echo БЛОК || echo НЕ-БЛОК)" "rc=${NEG_RC}"
# Выход в CSV, а не только в логе: без этого строки батча невозможно отличить
# от замеров, ушедших мимо SIM (такой случай был 09.10.2026 — точка доступа
# отвалилась, macOS вернулся на домашний Wi-Fi, а метка осталась «МТС Саратов»).
csv_row "egress" "start:${IFACE:-?}/${IFACE_IP:-нет}" "mobile=$([[ "$EGRESS_OK" -eq 1 ]] && echo да || echo нет)" \
    "$([[ "$EGRESS_OK" -eq 1 ]] && echo ДЕЙСТВИТЕЛЕН || echo НЕДЕЙСТВИТЕЛЕН)" "via=${VIA_IFACE:-default}"
echo

# --- 4. Глубокий замер адресов-кандидатов -------------------------------------
echo "4. АДРЕСА-КАНДИДАТЫ В ПЛОТНЫХ БЕЛЫХ ПОДСЕТЯХ (--deep, 3 попытки)"
echo "   Ожидание под режимом: rc=28 (тишина) = подсеть пропускается; rc=7 = блок."
echo "------------------------------------------------------------------------------"
for entry in "${CANDIDATES[@]}"; do
    addr="${entry%%|*}"; note="${entry#*|}"
    echo
    echo ">>> ${addr}  (${note})"
    ./scripts/lte_probe.sh --deep --host "$addr" --deep-ports 443 --repeat 3 \
        --control "$CONTROL" --report "$REPORT" --label "$LABEL"
done
echo

# --- 5. Эффект соседа: пустые адреса в /24 ------------------------------------
echo "5. ЭФФЕКТ СОСЕДА: пустые адреса .1/.77/.254 в плотных /24"
echo "------------------------------------------------------------------------------"
for r in "${RANGES[@]}"; do
    echo
    echo ">>> ${r}"
    ./scripts/lte_probe.sh --check-range "$r" --repeat 3 \
        --control "$CONTROL" --report "$REPORT" --label "$LABEL"
done
echo

# --- 6. IPv6-гипотеза ---------------------------------------------------------
if [[ "$SKIP_IPV6" -eq 1 ]]; then
    echo "6. IPv6-ГИПОТЕЗА — пропущена по флагу --skip-ipv6"
else
    echo "6. IPv6-ГИПОТЕЗА (в списке 0 IPv6 — возможно, фильтр только по IPv4)"
    echo "------------------------------------------------------------------------------"
    V6="$(ifconfig "${VIA_IFACE:-$IFACE}" 2>/dev/null | awk '/inet6/ && !/fe80/ && !/::1/ {print $2}' | head -1)"
    if [[ -z "$V6" ]]; then
        echo "  Глобального IPv6 на ${VIA_IFACE:-$IFACE} нет → у оператора абоненту IPv6 не выдаётся."
        echo "  Гипотезу на этой SIM проверить НЕВОЗМОЖНО (это не «не работает», это «не применимо»)."
        csv_row "ipv6" "interface:${IFACE}" "" "НЕПРИМЕНИМО" "нет глобального IPv6 у абонента"
    else
        ok "у абонента есть глобальный IPv6: ${V6}"
        echo "  ping6 до Timeweb  : $(ping6 -c 2 -i 1 -t 5 2a03:6f00:a::1:cc85 2>&1 | tail -1)"
        echo "  ping6 до Cloudflare: $(ping6 -c 2 -i 1 -t 5 2606:4700:4700::1111 2>&1 | tail -1)"
        for target in "2a03:6f00:a::1:cc85" "2606:4700:4700::1111"; do
            rc="$(hit_rc "https://[${target}]/")"
            printf '  curl -6 https://[%s]/ → rc=%s\n' "$target" "$rc"
            csv_row "ipv6" "[${target}]" "" "$([[ "$rc" == "28" || "$rc" == "7" ]] && echo "НЕТ ОТВЕТА" || echo "ОТВЕТ ЕСТЬ")" "rc=${rc}"
        done
        echo "  Читать так: rc=28 на зарубежном IPv6 при закрытом IPv4 — гипотеза подтверждается."
        echo "  rc=7 или 28 на обоих — скорее всего IPv6 нет дальше оператора (CGNAT/без маршрута)."
    fi
fi
echo

# --- 7. Контроль в конце ------------------------------------------------------
echo "7. КОНТРОЛЬ В КОНЦЕ БАТЧА"
echo "------------------------------------------------------------------------------"
END_RC="$(hit_rc "$CONTROL")"
if [[ "$END_RC" == "0" ]]; then
    ok "разрешённый адрес ${CONTROL} → rc=0 (режим не переключился во время замера)"
else
    bad "разрешённый адрес ${CONTROL} → rc=${END_RC} — режим переключался, ЧАСТЬ ЗАМЕРОВ НЕДОСТОВЕРНА"
fi
END_NEG="$(hit_rc "https://${NEG_HOST}:443")"
echo "  заблокированный ${NEG_HOST}:443 → rc=${END_NEG}"
csv_row "control-end" "${CONTROL}" "allowed" "$([[ "$END_RC" == "0" ]] && echo ПРОХОДИТ || echo БЛОК)" "rc=${END_RC}"
END_IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
END_IP="$(ipconfig getifaddr "$END_IFACE" 2>/dev/null || true)"
csv_row "egress" "end:${END_IFACE:-?}/${END_IP:-нет}" "mobile=?" \
    "$([[ "$END_IP" == "$IFACE_IP" ]] && echo ДЕЙСТВИТЕЛЕН || echo "НЕДЕЙСТВИТЕЛЕН: выход сменился")" \
    "start=${IFACE_IP:-нет}"
if [[ "$END_IP" != "$IFACE_IP" ]]; then
    bad "выход сменился во время замера: ${IFACE_IP:-нет} → ${END_IP:-нет} — РЕЗУЛЬТАТ НЕДЕЙСТВИТЕЛЕН"
fi
echo

echo "=============================================================================="
echo " ИТОГ ЭТАПА 0"
echo "   контроль начало/конец: rc=${CTRL_RC} / rc=${END_RC}"
echo "   негативный контроль  : rc=${NEG_RC} / rc=${END_NEG}   (7 = фильтр работает)"
echo "   пройдено проверок: ${PASS_N}, провалено: ${FAIL_N}, предупреждений: ${WARN_N}"
echo "   CSV: ${REPORT}"
echo "   Лог: ${LOG}"
echo "=============================================================================="
echo "Как читать ГЛАВНОЕ:"
echo "  • негативный контроль rc=7 И хотя бы один кандидат дал rc=28/0 → режим ограничений"
echo "    ВКЛЮЧЁН и вход в разрешённой подсети существует. Это зелёный свет на этап 1."
echo "  • ВСЁ дало rc=7 (и кандидаты, и .1/.77/.254) → либо режим выключен, либо фильтр"
echo "    другой природы. ДЕНЬГИ НЕ ТРАТИТЬ, вернуться к владельцу с вопросом,"
echo "    где именно он наблюдает ограничения."
echo "  • контроль rc≠0 → сеть мертва, замер недействителен, повторить позже."
echo "=============================================================================="
