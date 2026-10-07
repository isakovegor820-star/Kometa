#!/usr/bin/env bash
# LTE-замер: что именно режет оператор на конкретной симке.
#
# Запускать С ТЕЛЕФОНА (Termux) или с ноутбука, подключённого к мобильной точке,
# при ВЫКЛЮЧЕННОМ VPN. Скрипт только проверяет доступность, ничего не меняет
# ни на сервере, ни на телефоне.
#
# Примеры:
#   ./scripts/lte_probe.sh --host 203.0.113.10 --sni www.microsoft.com
#   ./scripts/lte_probe.sh --host 203.0.113.10 --ports 443,8443,2053 --udp-port 443
#   LTE_OPERATOR=MTS LTE_REGION=MSK ./scripts/lte_probe.sh --host 203.0.113.10
#
# Что проверяется (см. docs/LTE-ВАРИАНТЫ-2026-10.md, §10):
#   1) контроль: интернет вообще есть
#   2) TCP-доступность портов          — грубый L4-фильтр
#   3) TLS с SNI                       — базовый хендшейк
#   4) TLS БЕЗ SNI (-noservername)     — эвристика июня 2026 не применяется к пустому SNI
#   5) QUIC/443 (--http3-only)         — блокировка QUIC ломает Hysteria2/TUIC
#   6) UDP-порт                        — грубая блокировка UDP по порту
#
#
# Отдельный режим --deep: одиночный адрес, повторы и РАЗЛИЧЕНИЕ RST/таймаута.
# Нужен для проверки адреса-кандидата в «белой» подсети: RST от ТСПУ (rc=7) и
# тишина несуществующего слушателя (rc=28) — разные вещи, и по ним решается,
# брать адрес или нет. См. docs/БЕЛЫЕ-СПИСКИ-ВНЕДРЕНИЕ.md, §3.1.
#
# Результат: таблица «проверка → итог → что это значит».

set -u

HOST=""
SNI=""
PORTS="443,8443,2053"
UDP_PORT="443"
CONTROL_URL="https://ifconfig.co"
TIMEOUT=8
DEEP=0
REPEAT=3
DEEP_PORTS="443"
CHECK_RANGE=""
RANGE_LIST=""
POOL_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)      HOST="${2:?}"; shift 2 ;;
        --sni)       SNI="${2:?}"; shift 2 ;;
        --ports)     PORTS="${2:?}"; shift 2 ;;
        --udp-port)  UDP_PORT="${2:?}"; shift 2 ;;
        --control)   CONTROL_URL="${2:?}"; shift 2 ;;
        --timeout)   TIMEOUT="${2:?}"; shift 2 ;;
        --deep)      DEEP=1; shift ;;
        --repeat)    REPEAT="${2:?}"; shift 2 ;;
        --deep-ports) DEEP_PORTS="${2:?}"; shift 2 ;;
        --check-range) CHECK_RANGE="${2:?}"; shift 2 ;;
        --range-list)  RANGE_LIST="${2:?}"; shift 2 ;;
        --pool)        POOL_FILE="${2:?}"; shift 2 ;;
        -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
        *)           echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$HOST" && -z "$CHECK_RANGE" && -z "$RANGE_LIST" && -z "$POOL_FILE" ]]; then
    echo "Ошибка: обязателен --host <IP или домен ноды> (или --check-range/--range-list)" >&2
    exit 2
fi


# --- утилиты ---------------------------------------------------------------

# Переносимый timeout: GNU coreutils timeout / gtimeout / фолбэк на фоновый процесс.
run_timeout() { # run_timeout <секунды> <команда...>
    local secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$@"
    else
        "$@" &
        local pid=$!
        ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) >/dev/null 2>&1 &
        local killer=$!
        wait "$pid" 2>/dev/null
        local rc=$?
        kill "$killer" 2>/dev/null
        wait "$killer" 2>/dev/null
        return $rc
    fi
}

pass() { printf '%-34s ✅ %s\n' "$1" "$2"; }
fail() { printf '%-34s ❌ %s\n' "$1" "$2"; }
warn() { printf '%-34s ⚠️  %s\n' "$1" "$2"; }
skip() { printf '%-34s ➖ %s\n' "$1" "$2"; }

# --- режим --deep: один адрес, повторы, RST против таймаута ------------------
#
# Зачем отдельно от основного прогона: для адреса-кандидата в «белой» /24 важно
# не «доступен/недоступен», а ПОЧЕМУ: rc=7 (RST) — это фильтр ТСПУ, rc=28
# (таймаут) — скорее нет слушателя. Основной прогон этого не различает.
# Дисциплина: только с телефона, Wi-Fi и VPN выключены, контроль в начале и конце.
# Классификация одного подключения: копит счётчики через eval-переменные
# (bash не умеет возвращать несколько значений).
classify_rc() { # classify_rc <rc> <префикс>
    local rc="$1" p="$2"
    case "$rc" in
        0 | 60 | 92) eval "${p}_tls=\$((${p}_tls + 1))" ;;
        35)          eval "${p}_hs=\$((${p}_hs + 1))" ;;
        7)           eval "${p}_rst=\$((${p}_rst + 1))" ;;
        28)          eval "${p}_to=\$((${p}_to + 1))" ;;
        *)           eval "${p}_other=\$((${p}_other + 1))" ;;
    esac
}

# Одно подключение: печатает rc и копит счётчики. Тихо — для пакетного режима.
probe_once() { # probe_once <host> <port> <префикс> [quiet]
    local host="$1" port="$2" p="$3" quiet="${4:-}"
    local rc=0
    curl -sk -o /dev/null -m "$TIMEOUT" "https://${host}:${port}" 2>/dev/null || rc=$?
    classify_rc "$rc" "$p"
    [[ "$quiet" == "quiet" ]] || printf '    rc=%s\n' "$rc"
    return 0
}

# Проверка адреса БЕЗ слушателя: rc=28 (тишина) = L3 пропускает, rc=7 = блок.
# Именно так проверяется «белая» /24, не поднимая на ней сервер.
check_quiet_addr() { # check_quiet_addr <IP>
    local ip="$1" q_tls=0 q_hs=0 q_rst=0 q_to=0 q_other=0 attempt
    for ((attempt = 1; attempt <= REPEAT; attempt++)); do
        probe_once "$ip" 443 q quiet
    done
    printf '%-16s ' "$ip"
    if [[ "$q_rst" -gt 0 && "$q_to" -eq 0 && "$q_tls" -eq 0 && "$q_hs" -eq 0 ]]; then
        printf '❌ БЛОК (RST): адрес отброшен фильтром\n'
    elif [[ "$q_to" -gt 0 && "$q_rst" -eq 0 ]]; then
        printf '✅ ПРОХОДИТ (тишина = нет слушателя, L3 пропускает)\n'
    elif [[ "$q_tls" -gt 0 || "$q_hs" -gt 0 ]]; then
        printf '⚠️  на адресе кто-то есть (rc TOS/TLS) — для проверки /24 бери пустой\n'
    else
        printf '❓ смешанно (RST=%d, тишина=%d) — повтори\n' "$q_rst" "$q_to"
    fi
}

# Режим --pool: проверить пул входов (основной + резерв) и сказать, какой годится.
# Формат файла: по строке на вход — "метка IP/CIDR", # — комментарий.
#   main 203.0.113.10
#   cold 91.240.86.0/24
# Для адреса проверяется он сам (--deep), для подсети — пустые соседи (--check-range):
# так видно, работает ли «эффект соседа» и есть ли куда переехать, когда основной выгорит.
check_pool() { # check_pool <файл>
    local file="$1" line label target
    [[ -f "$file" ]] || { echo "Файл не найден: $file" >&2; return 2; }
    echo "Проверка пула входов: ${file}"
    echo "Контроль: ${CONTROL_URL}"
    echo "======================================================================"
    local ready=0 total=0
    while read -r line; do
        line="${line%%#*}"
        # shellcheck disable=SC2086
        set -- $line
        [[ $# -ge 2 ]] || continue
        label="$1"; target="$2"; total=$((total + 1))
        echo
        if [[ "$target" == */* ]]; then
            check_range "$target"
            ready=$((ready + 1))
        else
            probe_deep "$target" 443
            ready=$((ready + 1))
        fi
    done < "$file"
    echo "======================================================================"
    echo "Проверено входов: ${total}. Не забудьте: нужен минимум ОДИН резервный адрес"
    echo "в другой /24 у второго хостера — без него цикл замены занимает сутки."
}

# Пакетный режим --check-range: проверить N адресов из одной /24 (по умолчанию
# «.1», «.77», «.254» — крайние и середина; сервисы там обычно не стоят).
check_range() { # check_range <a.b.c.0/24>
    local cidr="$1" base suffix
    # 91.240.86.0/24 → 91.240.86: убираем «/24», затем два последних октета.
    base="${cidr%%/*}"         # 91.240.86.0
    base="${base%.*}"          # 91.240.86 — три октета, из них собираем адреса
    if [[ "$(printf '%s' "$base" | tr -cd '.' | wc -c | tr -d ' ')" != "2" ]]; then
        echo "Ошибка: ожидается CIDR вида 91.240.86.0/24 (получено: ${cidr})" >&2
        return 2
    fi
    echo "Проверяю /24 ${base}.0/24 — по ${REPEAT} попытки на адрес"
    echo "Контроль: ${CONTROL_URL}"
    echo "----------------------------------------------------------------------"
    local ok=0 bad=0
    for suffix in 1 77 254; do
        check_quiet_addr "${base}.${suffix}"
    done
    echo "----------------------------------------------------------------------"
    echo "Как читать: «ПРОХОДИТ» хотя бы на одном пустом адресе — вся /24, скорее"
    echo "всего, пропускается (фильтр по подсети). «БЛОК» на всех — подсеть закрыта."
    echo "Контроль в конце: ${CONTROL_URL}"
    run_timeout "$((TIMEOUT + 2))" curl -sS -o /dev/null -m "$TIMEOUT" "$CONTROL_URL" \
        >/dev/null 2>&1 && pass "контроль после" "интернет есть" \
        || warn "контроль после" "контроль пропал — замер недостоверен"
}

probe_deep() {
    local host="$1" port="$2" attempt rc tls_ok=0 handshake=0 rst=0 timeout=0 other=0
    echo "Глубокий замер: ${host}:${port}, попыток: ${REPEAT}"
    echo "Контроль: ${CONTROL_URL}"
    echo "----------------------------------------------------------------------"

    local curl_args=(-sS -o /dev/null -m "$TIMEOUT")
    # Контроль «до»: если сеть мертва, любой rc на цели бессмыслен.
    if run_timeout "$((TIMEOUT + 2))" curl "${curl_args[@]}" "$CONTROL_URL" >/dev/null 2>&1; then
        pass "контроль до" "интернет есть"
    else
        warn "контроль до" "контрольный хост не ответил — результат цели недостоверен"
    fi

    for ((attempt = 1; attempt <= REPEAT; attempt++)); do
        rc=0
        curl -sk -o /dev/null -m "$TIMEOUT" "https://${host}:${port}" 2>/dev/null || rc=$?
        # rc=0/60/92 — TLS состоялся (60 = сертификат, 92 = HTTP/2): адрес точно
        # проходит. rc=35 (обрыв на рукопожатии) — НЕОДНОЗНАЧНО: так же выглядит
        # и перехват, и блок без RST, и медленный слушатель. В «прошло» не идёт.
        case "$rc" in
            0 | 60 | 92) tls_ok=$((tls_ok + 1)) ;;
            35) handshake=$((handshake + 1)) ;;
            7)  rst=$((rst + 1)) ;;
            28) timeout=$((timeout + 1)) ;;
            *)  other=$((other + 1)) ;;
        esac
        printf '    попытка %d/%d: rc=%s\n' "$attempt" "$REPEAT" "$rc"
    done

    echo "----------------------------------------------------------------------"
    printf 'Итог %s:%s → TLS состоялся: %d · обрыв на рукопожатии (rc=35): %d · RST (rc=7, фильтр): %d · таймаут (rc=28): %d · прочее: %d\n' \
        "$host" "$port" "$tls_ok" "$handshake" "$rst" "$timeout" "$other"

    if [[ "$tls_ok" -gt 0 ]]; then
        echo "ВЕРДИКТ: адрес ПРОХОДИТ — TCP и TLS состоялись. Можно брать."
    elif [[ "$handshake" -gt 0 && "$rst" -eq 0 && "$timeout" -eq 0 ]]; then
        echo "ВЕРДИКТ: НЕОДНОЗНАЧНО — TCP есть, TLS обрывается (rc=35)."
        echo "          Так выглядят и блок без RST, и перехват, и кривой слушатель."
        echo "          Подними на адресе рабочий TLS и повтори; без этого не брать."
    elif [[ "$rst" -gt 0 && "$timeout" -eq 0 ]]; then
        echo "ВЕРДИКТ: адрес ЗАБЛОКИРОВАН (RST от фильтра). Не брать."
    elif [[ "$timeout" -gt 0 && "$rst" -eq 0 ]]; then
        echo "ВЕРДИКТ: тишина. Либо нет слушателя, либо блок без RST."
        echo "          Подними на адресе TLS-слушатель и повтори — иначе вывод неоднозначен."
    else
        echo "ВЕРДИКТ: смешанная картина — режим переключается по вышкам. Повтори замер."
    fi

    if run_timeout "$((TIMEOUT + 2))" curl "${curl_args[@]}" "$CONTROL_URL" >/dev/null 2>&1; then
        pass "контроль после" "интернет есть"
    else
        warn "контроль после" "контроль пропал — замер шёл во время смены режима"
    fi
}

if [[ -n "$POOL_FILE" ]]; then
    check_pool "$POOL_FILE"
    exit 0
fi

if [[ -n "$CHECK_RANGE" || -n "$RANGE_LIST" ]]; then
    if [[ -n "$RANGE_LIST" ]]; then
        [[ -f "$RANGE_LIST" ]] || { echo "Файл не найден: $RANGE_LIST" >&2; exit 2; }
        while read -r cidr; do
            [[ -z "$cidr" || "$cidr" == \#* ]] && continue
            check_range "$cidr"
            echo
        done < "$RANGE_LIST"
    else
        check_range "$CHECK_RANGE"
    fi
    exit 0
fi

if [[ "$DEEP" -eq 1 ]]; then
    IFS=',' read -r -a deep_ports <<< "$DEEP_PORTS"
    for p in "${deep_ports[@]}"; do
        p="$(echo "$p" | tr -d ' ')"
        [[ -n "$p" ]] || continue
        probe_deep "$HOST" "$p"
        echo
    done
    exit 0
fi

# --- 0. шапка ---------------------------------------------------------------

echo "LTE-замер: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "Оператор : ${LTE_OPERATOR:-не указан (задайте LTE_OPERATOR=MTS и т.п.)}"
echo "Регион   : ${LTE_REGION:-не указан}"
echo "Нода     : ${HOST}  SNI: ${SNI:-нет}"
echo "Порты    : ${PORTS}   UDP: ${UDP_PORT}"
echo "----------------------------------------------------------------------"

# --- 1. контроль ------------------------------------------------------------

if run_timeout "$TIMEOUT" curl -sS -o /dev/null -m "$TIMEOUT" "$CONTROL_URL" 2>/dev/null; then
    pass "1. Контроль (интернет)" "$CONTROL_URL отвечает"
else
    fail "1. Контроль (интернет)" "нет доступа даже к $CONTROL_URL — сеть мертва или нужен другой контрольный URL"
fi

# --- 2. TCP-порты -----------------------------------------------------------

IFS=',' read -r -a port_list <<< "$PORTS"
for p in "${port_list[@]}"; do
    p="$(echo "$p" | tr -d ' ')"
    [[ -z "$p" ]] && continue
    # Сначала nc (чистый таймаут), иначе bash /dev/tcp под таймаутом.
    if command -v nc >/dev/null 2>&1; then
        if run_timeout "$TIMEOUT" nc -z -w "$TIMEOUT" "$HOST" "$p" >/dev/null 2>&1; then
            pass "2. TCP ${p}" "соединение установлено"
        else
            fail "2. TCP ${p}" "нет соединения: RST/тишина — L4-фильтр по порту или нода недоступна"
        fi
    elif run_timeout "$TIMEOUT" bash -c "exec 3<>/dev/tcp/${HOST}/${p}" >/dev/null 2>&1; then
        pass "2. TCP ${p}" "соединение установлено (/dev/tcp)"
    else
        fail "2. TCP ${p}" "нет соединения: RST/тишина — L4-фильтр по порту или нода недоступна"
    fi
done

# --- 3-4. TLS с SNI и без SNI ----------------------------------------------

# Проверка соответствия сертификата: для IP — -verify_ip, для имени — -verify_hostname.
if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$HOST" == *:* ]]; then
    VERIFY_ARG="-verify_ip"; VERIFY_VAL="$HOST"
else
    VERIFY_ARG="-verify_hostname"; VERIFY_VAL="$HOST"
fi

if ! command -v openssl >/dev/null 2>&1; then
    skip "3. TLS с SNI" "openssl не найден"
    skip "4. TLS без SNI" "openssl не найден"
else
    TLS_PORT="${port_list[0]%% *}"

    if [[ -n "$SNI" ]]; then
        out="$(run_timeout "$TIMEOUT" openssl s_client -connect "${HOST}:${TLS_PORT}" \
               -servername "$SNI" -brief </dev/null 2>&1)"
        if grep -qiE "Protocol version|Verification" <<<"$out"; then
            pass "3. TLS с SNI" "хендшейк прошёл на ${HOST}:${TLS_PORT} (SNI=${SNI})"
        else
            fail "3. TLS с SNI" "хендшейк не прошёл — SNI-фильтр или блок по IP"
        fi
    else
        skip "3. TLS с SNI" "не задан --sni"
    fi

    # Пустой SNI: ключевая проверка. Проходит — значит доступен вариант §2.3 (IP-сертификат).
    out="$(run_timeout "$TIMEOUT" openssl s_client -connect "${HOST}:${TLS_PORT}" \
           -noservername "$VERIFY_ARG" "$VERIFY_VAL" -brief </dev/null 2>&1)"
    if grep -qiE "Protocol version" <<<"$out"; then
        if grep -qiE "Verification: OK" <<<"$out"; then
            pass "4. TLS без SNI" "хендшейк прошёл, сертификат валиден для ${VERIFY_VAL}"
        elif grep -qiE "verify error|self.signed|unable to verify" <<<"$out"; then
            pass "4. TLS без SNI" "хендшейк прошёл (сертификат не подтверждён — для теста ок)"
        else
            pass "4. TLS без SNI" "хендшейк прошёл"
        fi
    else
        warn "4. TLS без SNI" "хендшейк не прошёл — либо на порту нет TLS-инбаунда, либо фильтр режет и пустой SNI"
    fi
fi

# --- 5. QUIC / UDP 443 ------------------------------------------------------

if curl --version 2>/dev/null | grep -qi "HTTP3"; then
    if run_timeout "$TIMEOUT" curl -sS --http3-only -o /dev/null -m "$TIMEOUT" \
        "https://${HOST}/" 2>/dev/null; then
        pass "5. QUIC (443/udp)" "QUIC работает — Hysteria2/TUIC имеют смысл"
    else
        fail "5. QUIC (443/udp)" "QUIC не проходит: блокировка QUIC → ставка на TCP-транспорты"
    fi
else
    skip "5. QUIC (443/udp)" "curl без HTTP/3 (нужен curl с --http3-only)"
fi

# --- 6. UDP-порт ------------------------------------------------------------

# UDP сам по себе не подтверждает доступность: важно, придёт ли ICMP port-unreachable
# (пакет дошёл, порт закрыт) или будет тишина (фильтр либо сервис молчит).
# udp_probe_python <host> <port> [dns] — с "dns" отправляет настоящий DNS-запрос.
udp_probe_python() {
    python3 - "$1" "$2" "${3:-}" "$TIMEOUT" <<'PY' 2>/dev/null
import socket, struct, sys
host, port, mode, tmo = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
if mode == "dns":
    # DNS-запрос A example.com, recursion desired
    payload = (b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
               b"\x07example\x03com\x00\x00\x01\x00\x01")
else:
    payload = b"\x00" * 32
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(tmo)
try:
    s.sendto(payload, (host, port))
    s.recvfrom(2048)
    print("reply")
except socket.timeout:
    print("timeout")
except ConnectionRefusedError:
    print("refused")
except OSError as e:
    print("refused" if getattr(e, "errno", None) == 111 else "timeout")
PY
}

if command -v python3 >/dev/null 2>&1; then
    case "$(udp_probe_python "$HOST" "$UDP_PORT")" in
        reply)   pass "6. UDP ${UDP_PORT}" "пришёл ответ — UDP-сервис на порту живой" ;;
        refused) fail "6. UDP ${UDP_PORT}" "ICMP port-unreachable: пакет дошёл, порт закрыт (UDP не блокируется)" ;;
        *)       warn "6. UDP ${UDP_PORT}" "тишина: либо фильтр, либо на порту нет UDP-сервиса — сравнить с контролем ниже" ;;
    esac

    # Контроль: заведомо живой UDP-сервис (DNS 1.1.1.1:53). Если и он молчит —
    # значит сеть глушит ICMP/ответы и проверка выше неинформативна.
    case "$(udp_probe_python "1.1.1.1" 53 dns)" in
        reply)   pass "7. UDP-контроль (1.1.1.1:53)" "UDP в этой сети в принципе проходит" ;;
        *)       warn "7. UDP-контроль (1.1.1.1:53)" "контроль не отвечает (ICMP/UDP глушатся) — результат п.6 неинформативен" ;;
    esac
else
    skip "6. UDP ${UDP_PORT}" "нет python3 (для UDP-проверки нужен он; nc -u не показателен)"
fi

# --- итог -------------------------------------------------------------------

cat <<'EOF'
----------------------------------------------------------------------
Как читать результат (подробно — docs/LTE-ВАРИАНТЫ-2026-10.md, §10):

  2 ❌ по всем портам      → блок по IP/подсети (слой 1): менять ноду/IP, не протокол
  2 ✅, 3 ❌               → фильтр по SNI: включить fragment (§6.3) или пустой SNI (§2.3)
  4 ✅ при 3 ❌            → рабочий путь: TLS-инбаунд на IP-сертификате (§5.2)
  5 ❌                     → QUIC зарезан: Hysteria2/TUIC не вариант, ставка на TCP (§3.2)
  6 ❌ при 5 ✅            → нестандартный UDP-порт заглушён: слушать UDP на 443 (§4.1)

Отдельно (руками, из Termux на телефоне):
  • AmneziaWG: на сервере `awg show` — нет пакетов = порт/адрес;
    endpoint меняется, а `latest handshake` пуст = форма I1 (§3.1.1).
  • «Сибирское» ограничение: инструмент dpi-ch, колонка Siberian (§7.2).
  • Скорость до/после 60 секунд: троттлинг видно как падение до 2–5 Кбит/с.
EOF
