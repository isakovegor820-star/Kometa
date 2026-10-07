#!/usr/bin/env bash
# =============================================================================
#  Kometa — DNS-туннель (dnstt): СЕРВЕРНАЯ часть на VPS под Ubuntu/Debian
# =============================================================================
#  Зачем. В режиме «белых списков» у абонента отрезано почти всё, кроме DNS:
#  запросы к рекурсивным резолверам (UDP/53) обычно остаются живыми. DNS-туннель
#  превращает такой резолвер в транспорт: клиент заворачивает TCP-поток в
#  DNS-запросы, резолвер по делегированию отдаёт их нашему серверу, а сервер
#  разворачивает поток в локальный сервис (sshd или SOCKS5).
#
#  Что делает ЭТОТ скрипт (серверная часть dnstt, машина — Ubuntu/Debian):
#    1. печатает план: что скачать, какие ключи создать, какой юнит systemd
#       положить, какие правила iptables добавить, что прописать в DNS;
#    2. по умолчанию НЕ делает ничего: dry-run не ходит в сеть (ни curl, ни dig)
#       и не пишет в систему — это главная гарантия безопасности;
#    3. с --apply: ставит бинарник, создаёт пользователя и ключи, кладёт юнит,
#       заводит трафик с 53 на внутренний порт, включает и запускает сервис;
#    4. с --check-dns: только проверяет делегирование домена (dig) и выходит.
#
#  Почему порт 53 не занимаем сами. На Ubuntu/Debian 53 обычно держит
#  systemd-resolved (127.0.0.53:53), и драться с ним за порт не нужно. Сервер
#  слушает НЕпривилегированный UDP-порт (по умолчанию 5300), а трафик,
#  пришедший на 53, заводится туда правилом iptables -t nat PREROUTING
#  -j REDIRECT — ровно так рекомендует первоисточник (bamsoftware.com/software/
#  dnstt/). Порт ≥1024 обязателен ещё и потому, что юнит работает без
#  capabilities (CapabilityBoundingSet=) и привилегированный порт занять не смог бы.
#
#  Домен. Нужны две записи (пример: домен сервиса example.com):
#     A     tns.example.com  → <IP сервера>      адрес нашего сервера
#     NS    t.example.com    → tns.example.com   делегирование поддомена нам
#  Распространение NS — до 24 часов (кеш родительской зоны), поэтому «сразу не
#  работает» — нормально; --check-dns показывает, что уже видно снаружи.
#  ВАЖНО: tns.example.com НЕ должен быть поддоменом t.example.com — вся зона
#  t.example.com отдана под полезную нагрузку туннеля. Скрипт это проверяет.
#
#  Ключи. dnstt-server -gen-key -privkey-file server.key -pubkey-file server.pub.
#  Клиенту нужен ТОЛЬКО server.pub; server.key не покидает сервер никогда.
#
#  Режимы (--mode). У dnstt нет флага «режим»: режим — это адрес, на который
#  сервер форвардит развёрнутый поток (первоисточник, раздел «How to set it up»):
#     ssh   → 127.0.0.1:22 (sshd). Подходит мобильным приложениям: HTTP Injector,
#             HTTP Custom, DarkTunnel (Android), HTTP Injector (iOS) — они ходят
#             в локальный конец туннеля как в SSH-сервер;
#     socks → 127.0.0.1:1080 (Dante SOCKS5). Полный прокси без SSH-учётки.
#  Dante скрипт НЕ ставит (это отдельный пакет dante-server) — только печатает
#  требование к нему и предупреждает, если на 1080 никто не слушает.
#
#  Идемпотентность. Повторный запуск:
#     * ключи и юнит уже есть → «уже настроено, не перегенерирую», выход 0;
#     * перегенерация ключей — только явным --force, и старые ключи при этом
#       уезжают в бэкап-каталог: иначе уже розданный клиентам server.pub
#       перестанет подходить и все клиенты отвалятся;
#     * правило iptables проверяется через -C и не дублируется;
#     * существующий юнит без --force не перезаписывается.
#
#  Про WhiteDNS. WhiteDNS (наш корпус, 1.6.4) работает на движках StormDNS /
#  CottenDns — это ДРУГОЙ протокол. К серверу dnstt он не подключится, и
#  dnstt-клиент не подключится к серверу StormDNS/CottenDns. Предупреждение
#  печатается в конце каждого запуска.
#
#  Примеры:
#     # 1. Посмотреть план (ни сети, ни записи в систему):
#     ./scripts/add_dns_tunnel.sh --domain t.example.com \
#       --ns-host tns.example.com --server-ip 203.0.113.7
#
#     # 2. Со своим бинарником и его суммой (сервер без внешнего интернета):
#     ... --binary /root/dnstt-server --sha256 <64 hex>
#
#     # 3. Проверить только делегирование домена:
#     ... --check-dns
#
#     # 4. Реально развернуть (root, Ubuntu/Debian):
#     sudo ./scripts/add_dns_tunnel.sh --domain t.example.com \
#       --ns-host tns.example.com --server-ip 203.0.113.7 --apply
#
#     # 5. Режим полного прокси (на 127.0.0.1:1080 должен слушать Dante):
#     sudo ./scripts/add_dns_tunnel.sh ... --mode socks --apply
#
#  Зависимости: bash, sha256sum, systemctl, iptables (ip6tables — если есть),
#  curl (только с --apply и только когда нет --binary), dig (опционально —
#  проверка делегирования), getent (проверка пользователя). Без python/pip.
#  Базовые утилиты системы (useradd, chmod, mv, mkdir, date) отдельно не
#  проверяются — они есть в любом Ubuntu/Debian.
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------- параметры -------
DOMAIN="${DOMAIN:-}"
NS_HOST="${NS_HOST:-}"
SERVER_IP="${SERVER_IP:-}"
MODE="${MODE:-ssh}"
MTU="${MTU:-1232}"
PORT="${PORT:-5300}"
SSH_PORT="${SSH_PORT:-22}"
BINARY="${BINARY:-}"
SHA256="${SHA256:-}"
KEY_DIR="${KEY_DIR:-/etc/dnstt}"
SERVICE_USER="${SERVICE_USER:-dnstt}"
SERVICE_NAME="${SERVICE_NAME:-dnstt-server}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"   # env: для тестов и нестандартных систем
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
IFACE="${IFACE:-}"                            # сузить правило NAT до интерфейса
DNSTT_BASE_URL="${DNSTT_BASE_URL:-https://dnstt.network}"

# Встроенный Dante SOCKS5 в режиме socks (по первоисточнику — 127.0.0.1:1080).
SOCKS_ADDR="127.0.0.1:1080"
# Диапазон MTU из первоисточника: по умолчанию 1232, допустимо 512–1400.
MTU_MIN=512
MTU_MAX=1400
MTU_MOBILE_MAX=1200
DNS_PORT=53

APPLY=0
CHECK_DNS=0
FORCE=0
SERVICE_GROUP="${SERVICE_USER}"

IPV4_RE='^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$'
FQDN_RE='^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'
USER_RE='^[a-z_][a-z0-9_-]*$'
UNIT_NAME_RE='^[A-Za-z0-9_.@-]+$'
SHA256_RE='^[0-9a-f]{64}$'

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

# Временный файл без обязательной зависимости от mktemp (путь предсказуемый
# и убираемый; в dry-run не вызывается вообще — там нечего создавать).
tmp_file() {
    mktemp 2>/dev/null || printf '%s/kometa-dnstt.%s.%s' "${TMPDIR:-/tmp}" "$$" "${RANDOM:-0}"
}

usage() {
    cat <<'EOF'
Серверная часть DNS-туннеля dnstt на VPS (Ubuntu/Debian): бинарник, ключи,
systemd-юнит, заведение трафика с UDP/53 на внутренний порт, проверка
делегирования домена.

По умолчанию — DRY-RUN: скрипт печатает план (бинарник, ключи, юнит systemd,
правила iptables, записи DNS, команду клиенту), но НЕ ходит в сеть (ни curl,
ни dig) и НИЧЕГО не пишет в систему.
Установка — только с явным --apply. Проверка делегирования — с --check-dns.

Использование:
  ./scripts/add_dns_tunnel.sh --domain t.example.com --ns-host tns.example.com \
    --server-ip 203.0.113.7 [флаги]

Обязательно:
  --domain DOMAIN     зона туннеля, которую делегируем серверу (t.example.com).
                      Именно её клиент указывает последним аргументом.
  --ns-host HOST      имя сервера с A-записью (tns.example.com). НЕ должно быть
                      поддоменом --domain: вся зона --domain отдана туннелю.
  --server-ip IP      публичный IP сервера (по нему проверяется делегирование:
                      dig +short A $NS_HOST должен вернуть этот адрес).

Параметры туннеля:
  --mode ssh|socks    режим сервера (по умолчанию ssh):
                        ssh   — форвард в 127.0.0.1:<--ssh-port> (sshd), клиенты
                                подключаются SSH-приложениями (HTTP Injector,
                                HTTP Custom, DarkTunnel на Android; HTTP Injector
                                на iOS);
                        socks — форвард в 127.0.0.1:1080, где должен слушать
                                Dante SOCKS5 (полный прокси; Dante ставится
                                отдельно, скрипт его не устанавливает).
  --mtu N             MTU туннеля, 512–1400 (по умолчанию 1232). В ограниченных
                      мобильных сетях рекомендуют 512–1200: ошибка «requester
                      payload size ... too small» лечится снижением MTU.
  --port N            внутренний UDP-порт сервера, ≥1024 (по умолчанию 5300).
                      Порт 53 не занимаем: там systemd-resolved, а трафик на 53
                      заводится правилом iptables NAT PREROUTING REDIRECT.
  --ssh-port N        порт sshd для режима ssh (по умолчанию 22).

Бинарник и ключи:
  --binary PATH       не скачивать, взять готовый dnstt-server (например, принесён
                      на сервер вручную). С --apply он копируется в /usr/local/bin.
  --sha256 HEX        ожидаемая sha256 бинарника (64 hex). Без неё печатается
                      предупреждение «сумма не проверена»: сборки берутся с
                      зеркала dnstt.network (неофициальные), и для боевого
                      сервера сумму указывать обязательно. Список сумм:
                      https://dnstt.network/SHA256SUMS
  --key-dir DIR       каталог ключей (по умолчанию /etc/dnstt).
                      server.key — приватный (0600, не покидает сервер),
                      server.pub — публичный (его отдаём клиентам).
  --user NAME         системный пользователь сервиса (по умолчанию dnstt).
  --service-name NAME имя systemd-юнита (по умолчанию dnstt-server).

Режимы работы:
  --check-dns         только проверить делегирование (dig +short NS $DOMAIN,
                      dig +short A $NS_HOST) и выйти. Требует dig; без него
                      печатается подсказка про пакет bind9-dnsutils.
  --apply             реально установить: пользователь, бинарник, ключи, юнит,
                      iptables, systemctl enable --now. Требует root.
  --dry-run           явный dry-run (то же поведение, что и по умолчанию).
  --force             перегенерировать ключи и перезаписать юнит, даже если они
                      уже есть (старые ключи уезжают в бэкап-каталог).
  -h, --help          эта справка.

Переменные окружения: DOMAIN, NS_HOST, SERVER_IP, MODE, MTU, PORT, SSH_PORT,
BINARY, SHA256, KEY_DIR, SERVICE_USER, SERVICE_NAME, UNIT_DIR, BIN_DIR, IFACE,
DNSTT_BASE_URL. IFACE (например eth0) сужает правило NAT до одного интерфейса;
без него правило ставится на все интерфейсы, кроме lo.

Клиент (что делать после установки сервера):
    dnstt-client -udp <SERVER_IP>:53 [-mtu 512] -pubkey-file server.pub \
      t.example.com 127.0.0.1:7000
  Клиенту уходит ТОЛЬКО публичный ключ server.pub (server.key не покидает
  сервер). В режиме ssh приложения настраиваются как SSH к 127.0.0.1:7000 —
  Android: HTTP Injector, HTTP Custom, DarkTunnel; iOS: HTTP Injector.
  Не поднимается — снижай MTU (--mtu 512): ошибка «requester payload size ...
  too small» лечится именно этим.

Что печатается в конце: блок «КАК ПРОВЕРИТЬ» (systemctl status, journalctl -u,
ss -ulnp, dig @<server-ip>) и предупреждение о несовместимости с WhiteDNS.

Зависимости: bash, sha256sum, systemctl, iptables (ip6tables — если есть),
curl (только с --apply и только без --binary), dig (опционально), getent.

ВАЖНО: WhiteDNS (движки StormDNS/CottenDns) с этим сервером НЕ совместим —
либо dnstt + SSH-приложения у клиентов, либо StormDNS/CottenDns отдельно.
EOF
}

# --------------------------------------------------------------- утилиты ------
need_value() { # <флаг> <$#>: понятная ошибка вместо «parameter null or not set»
    [[ "$2" -ge 2 ]] || die "Флаг $1 требует значение (см. --help)."
}

is_fqdn() { [[ "$1" =~ $FQDN_RE ]]; }
is_ipv4() { [[ "$1" =~ $IPV4_RE ]]; }
is_ipv6() { [[ "$1" == *:* && "$1" =~ ^[0-9A-Fa-f:.]+$ ]]; }

lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

forward_target() {
    case "$MODE" in
        ssh)   printf '127.0.0.1:%s' "$SSH_PORT" ;;
        socks) printf '%s' "$SOCKS_ADDR" ;;
    esac
}

bin_path() { # куда встанет серверный бинарник
    if [[ -n "$BINARY" ]]; then
        printf '%s' "$BINARY"
    else
        printf '%s/dnstt-server' "$BIN_DIR"
    fi
}

resolve_download_url() { # → URL сборки под текущую архитектуру; пусто, если нет
    local arch=""
    arch="$(uname -m 2>/dev/null || printf 'unknown')"
    case "$arch" in
        x86_64 | amd64)  printf '%s/dnstt-server-linux-amd64' "$DNSTT_BASE_URL" ;;
        aarch64 | arm64) printf '%s/dnstt-server-linux-arm64' "$DNSTT_BASE_URL" ;;
        *) : ;;
    esac
}

sha_state() { # человекочитаемое состояние суммы
    if [[ -n "$SHA256" ]]; then
        printf '%s' "$SHA256"
    else
        printf 'НЕ ЗАДАНА — сумма не проверена'
    fi
}

# ------------------------------------------------------------ валидация -------
validate_params() {
    [[ -n "$DOMAIN" ]] || die "Обязателен --domain: зона туннеля (например t.example.com) — её делегируем серверу NS-записью."
    [[ -n "$NS_HOST" ]] || die "Обязателен --ns-host: имя сервера с A-записью (например tns.example.com) — на него указывает NS-запись зоны --domain."
    [[ -n "$SERVER_IP" ]] || die "Обязателен --server-ip: публичный IP сервера — без него нечем проверить делегирование (dig +short A \$NS_HOST должен вернуть этот адрес)."

    DOMAIN="$(lower "$DOMAIN")"
    NS_HOST="$(lower "$NS_HOST")"

    is_fqdn "$DOMAIN" || die "--domain «${DOMAIN}» не похож на домен. Нужен поддомен вида t.example.com (не IP и не одна метка)."
    is_fqdn "$NS_HOST" || die "--ns-host «${NS_HOST}» не похож на домен. Нужно имя вида tns.example.com."

    if [[ "$NS_HOST" == *".${DOMAIN}" ]]; then
        die "--ns-host «${NS_HOST}» лежит ВНУТРИ зоны --domain «${DOMAIN}». Вся эта зона отдана под полезную нагрузку туннеля, поэтому сервер имён не может в ней находиться: возьми имя вне зоны (например tns.example.com для зоны t.example.com)."
    fi

    if is_ipv4 "$SERVER_IP"; then
        :
    elif is_ipv6 "$SERVER_IP"; then
        warn "--server-ip задан IPv6-адресом: A-запись и dig +short A проверяются по IPv4. Убедись, что у ${NS_HOST} есть и AAAA, и что ip6tables заводит трафик на ${PORT}."
    else
        die "--server-ip «${SERVER_IP}» не похож на IP-адрес (ожидается IPv4, например 203.0.113.7)."
    fi

    case "$MODE" in
        ssh | socks) : ;;
        *) die "--mode «${MODE}» не поддерживается: выбери ssh (форвард в sshd) или socks (Dante SOCKS5 на ${SOCKS_ADDR})." ;;
    esac

    [[ "$MTU" =~ ^[0-9]+$ ]] || die "--mtu должен быть числом (512–${MTU_MAX}), получено: ${MTU}"
    if [[ "$MTU" -lt "$MTU_MIN" || "$MTU" -gt "$MTU_MAX" ]]; then
        die "--mtu ${MTU} вне диапазона ${MTU_MIN}–${MTU_MAX}. Слишком маленький MTU не соберёт ответ резолвера, слишком большой — не пролезет в мобильной сети."
    fi
    if [[ "$MTU" -gt "$MTU_MOBILE_MAX" ]]; then
        warn "MTU ${MTU} больше ${MTU_MOBILE_MAX}: в ограниченных мобильных сетях рекомендуют 512–${MTU_MOBILE_MAX}. Если клиент падает с «requester payload size ... too small» — снижай MTU (вплоть до 512)."
    fi

    [[ "$PORT" =~ ^[0-9]+$ ]] || die "--port должен быть числом (внутренний UDP-порт), получено: ${PORT}"
    if [[ "$PORT" -eq "$DNS_PORT" ]]; then
        die "--port 53 занять нельзя: там systemd-resolved (127.0.0.53:53). Внутренний порт — непривилегированный (по умолчанию 5300), а трафик с 53 заводится правилом iptables REDIRECT."
    fi
    if [[ "$PORT" -lt 1024 || "$PORT" -gt 65535 ]]; then
        die "--port ${PORT} вне диапазона 1024–65535: юнит работает без capabilities (CapabilityBoundingSet=) и привилегированный порт занять не сможет."
    fi

    [[ "$SSH_PORT" =~ ^[0-9]+$ && "$SSH_PORT" -ge 1 && "$SSH_PORT" -le 65535 ]] \
        || die "--ssh-port должен быть числом 1–65535, получено: ${SSH_PORT}"

    if [[ -n "$SHA256" ]]; then
        SHA256="$(lower "$SHA256" | tr -d '[:space:]')"
        [[ "$SHA256" =~ $SHA256_RE ]] \
            || die "--sha256 «${SHA256}» не похожа на sha256: нужно 64 hex-символа (взять из ${DNSTT_BASE_URL}/SHA256SUMS)."
    else
        warn "--sha256 не задана: сумма не проверена. Сборки берутся с зеркала ${DNSTT_BASE_URL} (неофициальные; оригинал — bamsoftware.com/software/dnstt/). Для боевого сервера укажи сумму из ${DNSTT_BASE_URL}/SHA256SUMS."
    fi

    if [[ -n "$BINARY" ]]; then
        if [[ ! -f "$BINARY" ]]; then
            if [[ "$APPLY" -eq 1 ]]; then
                die "--binary: файл не найден: ${BINARY}. Принеси серверный бинарник dnstt-server (linux-amd64/arm64) и укажи путь."
            fi
            warn "--binary ${BINARY}: файла пока нет (для dry-run это нормально; с --apply скрипт на этом остановится)."
        fi
    fi

    [[ "$SERVICE_USER" =~ $USER_RE ]] || die "--user «${SERVICE_USER}» не похож на имя системного пользователя."
    [[ "$SERVICE_NAME" =~ $UNIT_NAME_RE ]] || die "--service-name «${SERVICE_NAME}» содержит недопустимые символы (разрешены буквы, цифры, . _ @ -)."
    [[ "$KEY_DIR" == /* ]] || die "--key-dir должен быть абсолютным путём, получено: ${KEY_DIR}"
    [[ "$UNIT_DIR" == /* ]] || die "UNIT_DIR должен быть абсолютным путём, получено: ${UNIT_DIR}"

    UNIT_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"
    KEY_FILE="${KEY_DIR}/server.key"
    PUB_FILE="${KEY_DIR}/server.pub"
    BIN_PATH="$(bin_path)"
    SERVICE_GROUP="$SERVICE_USER"
}

# ------------------------------------------------------- состояние на диске --
keys_present() { [[ -s "$KEY_FILE" && -s "$PUB_FILE" ]]; }
unit_present() { [[ -f "$UNIT_FILE" ]]; }
is_configured() { keys_present && unit_present; }

# ------------------------------------------------------------- systemd --------
emit_unit() {
    local target=""
    target="$(forward_target)"
    cat <<EOF
[Unit]
Description=Kometa dnstt DNS-туннель (${DOMAIN})
Documentation=https://dnstt.network
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
ExecStart=${BIN_PATH} -udp :${PORT} -mtu ${MTU} -privkey-file ${KEY_FILE} ${DOMAIN} ${target}
Restart=always
RestartSec=3
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF
}

# ------------------------------------------------------------- iptables -------
# Порт 53 занят systemd-resolved, поэтому входящий UDP/53 заводим REDIRECT'ом
# на непривилегированный порт сервера (первоисточник, раздел «Tunnel server setup»).
nat_spec() { # <C|-I>: проверка или вставка правила
    local op="$1"
    if [[ -n "$IFACE" ]]; then
        printf -- '-t nat %s PREROUTING -i %s -p udp --dport %s -j REDIRECT --to-ports %s' \
            "$op" "$IFACE" "$DNS_PORT" "$PORT"
    else
        printf -- '-t nat %s PREROUTING ! -i lo -p udp --dport %s -j REDIRECT --to-ports %s' \
            "$op" "$DNS_PORT" "$PORT"
    fi
}

input_spec() { # <C|-I>: разрешить пришедший на внутренний порт UDP
    local op="$1"
    printf -- '%s INPUT -p udp --dport %s -j ACCEPT' "$op" "$PORT"
}

# --------------------------------------------------------------- печать -------
print_summary() {
    local mode_note="" bin_note="" url=""
    if [[ "$MODE" == "ssh" ]]; then
        mode_note="ssh — форвард в 127.0.0.1:${SSH_PORT} (sshd), клиенты — SSH-приложения"
    else
        mode_note="socks — форвард в ${SOCKS_ADDR} (Dante SOCKS5), полный прокси"
    fi
    if [[ -n "$BINARY" ]]; then
        bin_note="готовый: ${BINARY}"
    else
        url="$(resolve_download_url)"
        if [[ -n "$url" ]]; then
            bin_note="скачать: ${url}"
        else
            bin_note="неизвестная архитектура — нужен --binary"
        fi
    fi

    box "KOMETA • DNS-ТУННЕЛЬ (dnstt): СЕРВЕРНАЯ ЧАСТЬ"
    printf '  зона туннеля     : %s\n' "$DOMAIN"
    printf '  сервер имён      : %s (A → %s)\n' "$NS_HOST" "$SERVER_IP"
    printf '  режим            : %s\n' "$mode_note"
    printf '  внутренний порт  : %s/udp (трафик с %s заводит iptables REDIRECT)\n' "$PORT" "$DNS_PORT"
    printf '  MTU              : %s\n' "$MTU"
    printf '  бинарник         : %s\n' "$bin_note"
    printf '  путь установки   : %s/dnstt-server\n' "$BIN_DIR"
    printf '  sha256           : %s\n' "$(sha_state)"
    printf '  ключи            : %s, %s\n' "$KEY_FILE" "$PUB_FILE"
    printf '  юнит systemd     : %s (пользователь %s)\n' "$UNIT_FILE" "$SERVICE_USER"
    printf '  сервис           : %s\n' "$SERVICE_NAME"
    printf '  режим запуска    : %s\n' \
        "$( [[ "$APPLY" -eq 1 ]] && printf 'APPLY (устанавливаю в систему)' || printf 'DRY-RUN (ничего не делаю)' )"
}

print_dns_plan() {
    box "DNS: ЧТО ДОЛЖНО БЫТЬ В ЗОНЕ (сделать в панели регистратора)"
    cat <<EOF
  A     ${NS_HOST}   →  ${SERVER_IP}
  NS    ${DOMAIN}    →  ${NS_HOST}
  AAAA  ${NS_HOST}   →  <IPv6 сервера, если есть>
        Без AAAA часть резолверов уйдёт по IPv6 в никуда — тогда нужен и
        ip6tables-вариант правила (см. ниже).

  Проверка (расхождение NS занимает до 24 часов — это нормально):
    dig +short NS ${DOMAIN}          # ожидаем ${NS_HOST}
    dig +short A  ${NS_HOST}         # ожидаем ${SERVER_IP}
    dig @${SERVER_IP} ${DOMAIN} NS   # сервер отвечает как авторитативный (после запуска)

  Почему ${NS_HOST} не может лежать внутри ${DOMAIN}: вся эта зона отдана под
  полезную нагрузку туннеля, и скрипт такую конфигурацию отклоняет.
EOF
}

print_keys_plan() {
    box "КЛЮЧИ И ЮНИТ SYSTEMD"
    cat <<EOF
  Ключи (создаются только на сервере и только с --apply):
      $(bin_path) -gen-key -privkey-file ${KEY_FILE} -pubkey-file ${PUB_FILE}
      chmod 0600 ${KEY_FILE}    # приватный: не покидает сервер
      chmod 0644 ${PUB_FILE}    # публичный: его и только его отдаём клиентам

  каталог ключей : ${KEY_DIR} (владелец ${SERVICE_USER}, 0750)
  юнит           : ${UNIT_FILE}
  пользователь   : ${SERVICE_USER} (системный, без домашнего каталога и shell)
  запуск         : systemctl daemon-reload && systemctl enable --now ${SERVICE_NAME}
  уже настроено? : ключи и юнит проверяются ДО установки — если оба на месте,
                   скрипт скажет «уже настроено, не перегенерирую» и выйдет 0
                   (перегенерация — только с --force, старые ключи в бэкап).

  --- ${UNIT_FILE} ---
EOF
    emit_unit
    printf '\n'
}

print_iptables_plan() {
    box "IPTABLES: ТРАФИК С UDP/${DNS_PORT} → ВНУТРЕННИЙ ПОРТ ${PORT}"
    cat <<EOF
  Порт ${DNS_PORT} не занимаем: на Ubuntu/Debian его держит systemd-resolved
  (127.0.0.53:${DNS_PORT}), а сервису нужен непривилегированный порт. Входящий
  UDP/${DNS_PORT} заводится REDIRECT'ом на ${PORT}:

    iptables $(nat_spec -I)
    iptables $(input_spec -I)

  IPv6 (если у сервера есть AAAA):
    ip6tables -t nat -I PREROUTING ! -i lo -p udp --dport ${DNS_PORT} -j REDIRECT --to-ports ${PORT}
    ip6tables -I INPUT -p udp --dport ${PORT} -j ACCEPT

  Правила ставятся идемпотентно: сначала iptables -t nat -C ..., и только если
  правила нет — вставка. Дублей при повторном запуске не появляется.
  Правила не переживают перезагрузку сами по себе: сохрани их
  (iptables-save > /etc/iptables/rules.v4, пакет iptables-persistent) либо
  перезапусти этот скрипт с --apply после ребута.
  Интерфейс: $( [[ -n "$IFACE" ]] && printf 'только %s (IFACE)' "$IFACE" || printf 'все, кроме lo (сузить можно переменной IFACE=eth0)' )
  Проверка: iptables -t nat -S PREROUTING | grep ${DNS_PORT}
EOF
}

print_download_plan() {
    local url=""
    url="$(resolve_download_url)"
    box "БИНАРНИК: ПЛАН СКАЧИВАНИЯ"
    if [[ -n "$BINARY" ]]; then
        cat <<EOF
  Скачивание не нужно: --binary ${BINARY}
  С --apply скрипт проверит sha256 (если задана), убедится, что бинарник
  запускается ('--version', при неудаче '-h') и что это именно серверная сборка
  (в справке видно -gen-key / -privkey-file), затем положит его в
  ${BIN_DIR}/dnstt-server.
EOF
    else
        cat <<EOF
  архитектура хоста : $(uname -m 2>/dev/null || printf 'неизвестна')
  URL               : $( [[ -n "$url" ]] && printf '%s' "$url" || printf '<нет сборки: на dnstt.network только linux-amd64 и linux-arm64 — нужен --binary>' )
  куда              : ${BIN_DIR}/dnstt-server
  sha256            : $(sha_state)
  список сумм       : ${DNSTT_BASE_URL}/SHA256SUMS

  Команды (выполняются ТОЛЬКО с --apply):
      curl -fsSL --max-time 300 -o /tmp/dnstt-server '${url}'
      sha256sum /tmp/dnstt-server      # сверить со строкой из SHA256SUMS
      mv /tmp/dnstt-server ${BIN_DIR}/dnstt-server && chmod 0755 ${BIN_DIR}/dnstt-server
      ${BIN_DIR}/dnstt-server --version   # проверка запуска

  Зеркало ${DNSTT_BASE_URL} — НЕОФИЦИАЛЬНЫЕ сборки (оригинал: David Fifield,
  bamsoftware.com/software/dnstt/, публичное достояние). Поэтому --sha256 для
  боевого сервера обязателен, а при возможности лучше собрать из исходников.
EOF
    fi
}

print_client_hints() {
    box "КЛИЕНТЫ: ${DOMAIN}"
    cat <<EOF
  Клиенту нужен ТОЛЬКО публичный ключ ${PUB_FILE} (server.pub).
  Приватный ${KEY_FILE} не покидает сервер никогда.

  Базовая команда клиента (пример с IP ${SERVER_IP}):
    dnstt-client -udp ${SERVER_IP}:53 -pubkey-file server.pub ${DOMAIN} 127.0.0.1:7000

  127.0.0.1:7000 — локальный конец туннеля: в него и ходит приложение.

  MTU: по умолчанию 1232. В ограниченных мобильных сетях ставь меньше —
  снижение MTU лечит ошибку «requester payload size ... too small»:
    dnstt-client -udp ${SERVER_IP}:53 -mtu 512 -pubkey-file server.pub ${DOMAIN} 127.0.0.1:7000
  (в части клиентов флаг пишут длинно: --mtu 512; допустимый диапазон 512–1400.)
EOF
    case "$MODE" in
        ssh)
            cat <<EOF

  Режим ssh (сервер форвардит в 127.0.0.1:${SSH_PORT} — sshd):
    на телефоне поднимаем dnstt-client (или встроенный в приложение DNS-туннель),
    а само приложение настраиваем как SSH к локальному концу туннеля:
        Android : HTTP Injector, HTTP Custom, DarkTunnel
        iOS     : HTTP Injector
        SSH host 127.0.0.1, порт 7000, логин/пароль — учётная запись на этом VPS.
    Полный интернет получается, когда приложение умеет SSH-туннель (динамический
    SOCKS через ssh -D); без этого туннель даст только SSH.
EOF
            ;;
        socks)
            cat <<EOF

  Режим socks (сервер форвардит в ${SOCKS_ADDR} — Dante SOCKS5):
    приложения ходят в SOCKS5 127.0.0.1:7000 (локальный конец туннеля) — это
    полный прокси, SSH-учётка не нужна. Dante ставится отдельно (пакет
    dante-server) и слушает ${SOCKS_ADDR}; скрипт его НЕ устанавливает.
    Если на ${SOCKS_ADDR} никто не слушает, туннель поднимется, но приложения
    не получат интернет: проверь ss -tlnp | grep 1080.
EOF
            ;;
    esac
}

dry_run_preview() {
    box "DRY-RUN: ЧТО БУДЕТ СДЕЛАНО С --apply"
    cat <<EOF
  1. проверю, что запуск от root, и что есть systemctl, iptables,
     $( [[ -n "$BINARY" ]] && printf 'sha256sum и getent' || printf 'sha256sum, getent и curl' );
  2. создам системного пользователя $(printf '%s' "$SERVICE_USER") (если его ещё нет);
  3. $( [[ -n "$BINARY" ]] && printf 'возьму готовый бинарник %s' "$BINARY" || printf 'скачаю %s' "$(resolve_download_url)" )
     → ${BIN_DIR}/dnstt-server; сверю sha256 ($(sha_state)) и проверю запуск;
  4. создам ключи: ${BIN_DIR}/dnstt-server -gen-key -privkey-file ${KEY_FILE} -pubkey-file ${PUB_FILE};
  5. положу юнит ${UNIT_FILE} с ужесточением (NoNewPrivileges, PrivateTmp,
     ProtectSystem=strict, ProtectHome, свой пользователь, Restart=always);
  6. добавлю правила iptables для UDP/${DNS_PORT} → ${PORT} (с проверкой -C, без дублей);
  7. проверю делегирование домена через dig (если dig есть) и статус сервиса.

  Ключи и юнит НЕ перезаписываются, если уже есть: «уже настроено, не
  перегенерирую» (перегенерация — только с --force).
EOF
    echo
    log "Сетевых запросов нет: ни curl, ни dig. В систему ничего не записано."
    log "Ничего не изменено. Для реального развёртывания добавь --apply."
}

print_already_configured() {
    box "УЖЕ НАСТРОЕНО"
    cat <<EOF
  ключи : ${KEY_FILE}, ${PUB_FILE}
  юнит  : ${UNIT_FILE}

  Серверная часть уже развёрнута — уже настроено, не перегенерирую: иначе
  розданный клиентам server.pub перестал бы подходить и все они отвалились бы.

  Что делать дальше:
    * ничего не делать — конфигурация на месте (проверки ниже);
    * перегенерировать ключи (новый server.pub нужно раздать клиентам):
      --force (старые ключи уедут в ${KEY_DIR}/backup-<дата>);
    * перезаписать юнит после смены --mtu/--port/--mode: --force.
EOF
}

print_how_to_check() {
    box "КАК ПРОВЕРИТЬ (руками на сервере)"
    cat <<EOF
  1. Сервис жив и включён в автозапуск:
       systemctl status ${SERVICE_NAME} --no-pager
       systemctl is-enabled ${SERVICE_NAME}
     Логи (там же видно «requester payload size ... too small» — это про MTU):
       journalctl -u ${SERVICE_NAME} -n 50 --no-pager
       journalctl -u ${SERVICE_NAME} -f

  2. UDP-порт слушается (именно ${PORT}, не ${DNS_PORT}):
       ss -ulnp | grep ${PORT}
     Ожидаем ${BIN_PATH} на :${PORT}. Порт ${DNS_PORT} при этом остаётся у
     systemd-resolved — так и задумано: трафик заводит правило NAT.

  3. Правило REDIRECT на месте и считает пакеты:
       iptables -t nat -S PREROUTING | grep ${DNS_PORT}
       iptables -t nat -L PREROUTING -n -v

  4. Делегирование домена (снаружи и с самого сервера):
       dig +short NS ${DOMAIN}            # ожидаем ${NS_HOST}
       dig +short A  ${NS_HOST}           # ожидаем ${SERVER_IP}
       dig @${SERVER_IP} ${DOMAIN} NS +short
     Пусто у NS — делегирование ещё не разошлось (до 24 часов) либо NS указывает
     не на ${NS_HOST}. Быстрая проверка: ${0##*/} --check-dns ...

  5. Ключи и права:
       ls -l ${KEY_DIR}
       sha256sum ${PUB_FILE}
     Клиенту отдаём ТОЛЬКО ${PUB_FILE}; ${KEY_FILE} — не покидает сервер.

  6. С телефона (без этого серверная часть бесполезна):
       * в клиенте указываем домен ${DOMAIN} и публичный ключ;
       * не поднимается или рвётся — снижаем MTU до 512 (--mtu 512): ошибка
         «requester payload size ... too small» лечится именно этим;
       * DNS-туннель даёт 1–6 Мбит/с и обрывы около 10 минут: это резерв для
         текста, SSH и Telegram, а не для видео;
       * если не работает даже на MTU 512 — проверь, не перехватывает ли
         оператор внешний UDP/${DNS_PORT} и не режет ли большие ответы резолвер.
EOF
}

print_whitedns_warning() {
    box "ВАЖНО: WHITEDNS / STORMDNS / COTTENDNS С ЭТИМ СЕРВЕРОМ НЕ СОВМЕСТИМЫ"
    cat <<'EOF'
  WhiteDNS (наш корпус, 1.6.4) работает на движках StormDNS / CottenDns — это
  ДРУГОЙ протокол, чем dnstt. Клиент WhiteDNS к серверу dnstt не подключится,
  а dnstt-клиент не подключится к серверу StormDNS/CottenDns: разные форматы
  запросов, разные ключи, разная серверная часть.

  Поэтому выбор ровно один из двух, смешивать нельзя:
    * ЛИБО этот скрипт (dnstt): сервер dnstt-server, а клиентам — SSH-приложения
      (Android: HTTP Injector, HTTP Custom, DarkTunnel; iOS: HTTP Injector) либо
      dnstt-client там, где его можно запустить;
    * ЛИБО StormDNS/CottenDns разворачиваются ОТДЕЛЬНО: свой домен, свой сервер,
      свои ключи — и тогда работают клиенты WhiteDNS.

  Один домен и один NS-набор под два протокола не встанут: делегирование у зоны
  одно, а протоколы несовместимы.
EOF
}

# ------------------------------------------------------- проверка DNS ---------
check_delegation() { # → 0, если делегирование выглядит настроенным
    local ns_raw="" ns_list="" a_raw="" a_list="" aaaa_raw="" ok_ns=0 ok_a=0 direct=""

    if ! have dig; then
        warn "dig не найден — проверить делегирование нечем."
        warn "Поставь пакет:  apt install -y bind9-dnsutils   (Ubuntu/Debian; в старых релизах пакет назывался dnsutils)."
        warn "Без dig развёртывание работать будет, но NS/A до старта сервиса не проверить — это самая частая причина «туннель не поднимается»."
        return 1
    fi

    log "Запрос 1/3: dig +short NS ${DOMAIN}   (ожидаем ${NS_HOST})"
    ns_raw="$(dig +short NS "$DOMAIN" 2>/dev/null || true)"
    ns_list="$(printf '%s\n' "$ns_raw" | tr -d '\r' | sed 's/\.$//' | tr 'A-Z' 'a-z' | sed '/^$/d' | sort -u | tr '\n' ' ' || true)"
    if [[ -z "$ns_list" ]]; then
        warn "NS для ${DOMAIN} не видно: делегирование не настроено или ещё не распространилось (до 24 часов)."
    else
        case " $ns_list " in
            *" $NS_HOST "*) ok "NS ${DOMAIN} → ${ns_list}"; ok_ns=1 ;;
            *) warn "NS ${DOMAIN} указывает на «${ns_list}», а ожидалось ${NS_HOST}. Проверь NS-запись именно в родительской зоне ${DOMAIN#*.} и что она указывает на ${NS_HOST}; свежие изменения расходятся до 24 часов." ;;
        esac
    fi

    log "Запрос 2/3: dig +short A ${NS_HOST}   (ожидаем ${SERVER_IP})"
    a_raw="$(dig +short A "$NS_HOST" 2>/dev/null || true)"
    a_list="$(printf '%s\n' "$a_raw" | tr -d '\r' | sed '/^$/d' | sort -u | tr '\n' ' ' || true)"
    if [[ -z "$a_list" ]]; then
        warn "A-записи для ${NS_HOST} не видно: она не создана или ещё не распространилась."
    else
        case " $a_list " in
            *" $SERVER_IP "*) ok "A ${NS_HOST} → ${a_list}"; ok_a=1 ;;
            *) warn "A ${NS_HOST} → «${a_list}», а ожидался ${SERVER_IP}. Если сервер за NAT — поправь --server-ip на реальный внешний адрес." ;;
        esac
    fi

    if [[ "$ok_a" -eq 0 ]]; then
        aaaa_raw="$(dig +short AAAA "$NS_HOST" 2>/dev/null || true)"
        if [[ -n "$aaaa_raw" ]]; then
            warn "У ${NS_HOST} есть AAAA ($(printf '%s' "$aaaa_raw" | tr '\n' ' ')): IPv6-резолверы пойдут именно туда — заведи ip6tables-правило (см. план), иначе часть клиентов не подключится."
        fi
    fi

    log "Запрос 3/3: dig @${SERVER_IP} ${DOMAIN} NS   (сервер отвечает как авторитативный)"
    direct="$(dig +short "@${SERVER_IP}" "$DOMAIN" NS 2>/dev/null || true)"
    if [[ -n "$direct" ]]; then
        ok "Сервер отвечает по своему адресу: $(printf '%s' "$direct" | tr '\n' ' ')"
    else
        warn "Сервер ${SERVER_IP} не ответил как авторитативный для ${DOMAIN} — это нормально, если он ещё не запущен; после --apply проверь снова."
    fi

    [[ "$ok_ns" -eq 1 && "$ok_a" -eq 1 ]]
}

check_dns_only() {
    box "ПРОВЕРКА ДЕЛЕГИРОВАНИЯ: ${DOMAIN} → ${NS_HOST} → ${SERVER_IP}"
    if check_delegation; then
        ok "Делегирование выглядит настроенным: NS и A на месте."
        log "Дальше: развернуть сервер (--apply) и проверить клиентом — с телефона и с MTU 512, если сеть капризная."
        exit 0
    fi
    err "Делегирование проверить не удалось (подробности выше)."
    err "Пока NS/A не разошлись, туннель не поднимется ни у одного клиента — это не проблема сервера."
    exit 1
}

# ------------------------------------------------------------ установка -------
require_root() {
    if [[ "${EUID:-1}" -ne 0 ]]; then
        die "--apply требует root: systemd, пользователь и iptables без него не заводятся. Запусти через sudo, а просто посмотреть план можно и без root (dry-run по умолчанию)."
    fi
}

check_runtime_deps() {
    have systemctl || die "Нужен systemctl (systemd). Этот скрипт ставит сервис — на VPS без systemd его нет смысла запускать."
    have iptables  || die "Нужен iptables: без него трафик с UDP/${DNS_PORT} не завести на ${PORT}. Установи: apt install -y iptables."
    have sha256sum || die "Нужен sha256sum (пакет coreutils) — им проверяется сумма бинарника."
    have getent    || die "Нужен getent (пакет libc-bin) — им проверяется существование пользователя ${SERVICE_USER}."
}

require_curl() {
    have curl || die "Нужен curl для --apply без --binary. Поставь (apt install -y curl) либо принеси бинарник сам: --binary PATH."
}

check_sha256() { # <файл>
    local out="" got=""
    [[ -n "$SHA256" ]] || return 0
    out="$(sha256sum "$1" 2>/dev/null || true)"
    got="${out%% *}"
    if [[ "$got" != "$SHA256" ]]; then
        die "sha256 не совпала: ожидалось ${SHA256}, получено ${got:-<не посчитать>}. Файл НЕ установлен — скачай заново или возьми сумму из ${DNSTT_BASE_URL}/SHA256SUMS."
    fi
    ok "sha256 совпала: ${got}"
}

probe_binary() { # <файл>: убедиться, что это запускаемый dnstt-server, а не мусор
    local bin="$1" out=""
    out="$("$bin" --version 2>&1 || true)"
    case "$out" in
        *-gen-key* | *-privkey-file*) : ;;
        *) out="$("$bin" -h 2>&1 || true)" ;;
    esac
    case "$out" in
        *-gen-key* | *-privkey-file*)
            ok "Бинарник запускается, в справке видно -gen-key/-privkey-file — это серверная сборка."
            ;;
        *dnstt-client*)
            die "Это КЛИЕНТСКИЙ бинарник (dnstt-client), а нужен серверный dnstt-server. Возьми, например, ${DNSTT_BASE_URL}/dnstt-server-linux-amd64."
            ;;
        *)
            die "Бинарник ${bin} не запускается или это не dnstt-server. Вывод: ${out:-<пусто>}. Проверь архитектуру (uname -m → linux-amd64/arm64) и что скачан именно серверный бинарник."
            ;;
    esac
}

download_binary() {
    local url="" tmp=""
    url="$(resolve_download_url)"
    if [[ -z "$url" ]]; then
        die "Не знаю сборку под архитектуру «$(uname -m 2>/dev/null || printf '?')»: на ${DNSTT_BASE_URL} есть только linux-amd64 и linux-arm64. Скачай бинарник сам и передай --binary PATH."
    fi
    if [[ -z "$SHA256" ]]; then
        warn "--sha256 не задана: сумма не проверена (зеркало ${DNSTT_BASE_URL} — неофициальные сборки). Список сумм: ${DNSTT_BASE_URL}/SHA256SUMS."
    fi
    tmp="$(tmp_file)"
    log "Скачиваю ${url}"
    if ! curl -fsSL --max-time 300 -o "$tmp" "$url"; then
        rm -f "$tmp" 2>/dev/null || true
        die "curl не смог скачать ${url}. Проверь интернет на сервере или принеси бинарник вручную (--binary PATH)."
    fi
    [[ -s "$tmp" ]] || die "Скачанный файл пуст: ${url}. Возьми сборку вручную с ${DNSTT_BASE_URL}."
    check_sha256 "$tmp"
    probe_binary "$tmp"
    mkdir -p "$BIN_DIR" || die "Не удалось создать каталог ${BIN_DIR}."
    mv -f "$tmp" "${BIN_DIR}/dnstt-server" || die "Не удалось положить бинарник в ${BIN_DIR}/dnstt-server."
    chmod 0755 "${BIN_DIR}/dnstt-server"
    BIN_PATH="${BIN_DIR}/dnstt-server"
    ok "Бинарник установлен: ${BIN_PATH}"
}

ensure_user() {
    if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        ok "Пользователь ${SERVICE_USER} уже есть — не создаю (идемпотентность)."
    else
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER" \
            || die "Не удалось создать пользователя ${SERVICE_USER} (useradd). Проверь, что запуск от root."
        ok "Создан системный пользователь ${SERVICE_USER} (без домашнего каталога и без shell)."
    fi
    SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || printf '%s' "$SERVICE_USER")"
}

install_keys() {
    local stamp="" backup="" need_gen=0
    if [[ -s "$KEY_FILE" && -s "$PUB_FILE" ]]; then
        if [[ "$FORCE" -eq 1 ]]; then
            stamp="$(date +%Y%m%d-%H%M%S)"
            backup="${KEY_DIR}/backup-${stamp}"
            mkdir -p "$backup" || die "Не удалось создать каталог бэкапа ключей: ${backup}"
            chmod 0700 "$backup" 2>/dev/null || true
            cp -p "$KEY_FILE" "$PUB_FILE" "$backup/" || die "Не удалось сохранить старые ключи в ${backup}. Перегенерация отменена."
            warn "Старые ключи сохранены: ${backup} (розданный ранее server.pub работает только со старым сервером)."
            need_gen=1
        else
            ok "Ключи уже есть — не перегенерирую (--force перегенерирует с бэкапом старых)."
        fi
    else
        need_gen=1
    fi

    if [[ "$need_gen" -eq 1 ]]; then
        mkdir -p "$KEY_DIR" || die "Не удалось создать каталог ключей ${KEY_DIR}."
        chown "$SERVICE_USER:$SERVICE_GROUP" "$KEY_DIR" 2>/dev/null || true
        chmod 0750 "$KEY_DIR" 2>/dev/null || true
        "$BIN_PATH" -gen-key -privkey-file "$KEY_FILE" -pubkey-file "$PUB_FILE" \
            || die "dnstt-server не смог создать ключи в ${KEY_DIR}. Проверь права и что бинарник серверный."
        [[ -s "$KEY_FILE" && -s "$PUB_FILE" ]] || die "После -gen-key не появились ${KEY_FILE} и/или ${PUB_FILE}."
        ok "Ключи созданы: ${KEY_FILE} и ${PUB_FILE}"
    fi

    chown "$SERVICE_USER:$SERVICE_GROUP" "$KEY_FILE" "$PUB_FILE" 2>/dev/null || true
    chmod 0600 "$KEY_FILE"
    chmod 0644 "$PUB_FILE"
    ok "Права выставлены: ${KEY_FILE} 0600 (только сервер), ${PUB_FILE} 0644 (отдаём клиентам)."
    log "Публичный ключ (его и только его копируем клиенту):"
    cat "$PUB_FILE"
}

install_unit() {
    local tmp=""
    if [[ -f "$UNIT_FILE" && "$FORCE" -eq 0 ]]; then
        ok "Юнит ${UNIT_FILE} уже есть — не перезаписываю (идемпотентность; --force перезапишет)."
        return 0
    fi
    if [[ -f "$UNIT_FILE" ]]; then
        cp -p "$UNIT_FILE" "${UNIT_FILE}.bak-$(date +%Y%m%d-%H%M%S)" \
            || die "Не удалось сохранить прежний юнит ${UNIT_FILE}."
        warn "Прежний юнит сохранён рядом (${UNIT_FILE}.bak-<дата>)."
    fi
    mkdir -p "$UNIT_DIR" || die "Не удалось создать каталог юнитов ${UNIT_DIR}."
    tmp="$(tmp_file)"
    emit_unit > "$tmp" || die "Не удалось записать временный файл юнита."
    chmod 0644 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$UNIT_FILE" || die "Не удалось положить юнит в ${UNIT_FILE}."
    ok "Юнит установлен: ${UNIT_FILE}"
}

apply_iptables() {
    local tool=""
    for tool in iptables ip6tables; do
        if [[ "$tool" == "ip6tables" ]] && ! have ip6tables; then
            warn "ip6tables нет — IPv6-путь (AAAA у ${NS_HOST}) заводить нечем; если IPv6 не используется, это не страшно."
            continue
        fi
        if $tool $(nat_spec -C) 2>/dev/null; then
            ok "${tool}: правило REDIRECT уже есть — не дублирую."
        else
            $tool $(nat_spec -I) || die "${tool} не дал добавить REDIRECT UDP/${DNS_PORT} → ${PORT}. Проверь, что модуль nat доступен (lsmod | grep nat)."
            ok "${tool}: добавлено правило $(nat_spec -I)"
        fi
        if $tool $(input_spec -C) 2>/dev/null; then
            ok "${tool}: разрешение на UDP/${PORT} уже есть."
        else
            $tool $(input_spec -I) || warn "${tool} не дал добавить ACCEPT для UDP/${PORT} — если политика INPUT не DROP, это не критично."
            ok "${tool}: добавлено правило $(input_spec -I)"
        fi
    done
    warn "Правила iptables не сохраняются между перезагрузками: iptables-save > /etc/iptables/rules.v4 (пакет iptables-persistent) либо повторный --apply после ребута."
}

systemd_up() {
    systemctl daemon-reload || die "systemctl daemon-reload не сработал — проверь, что systemd жив (systemctl status)."
    systemctl enable --now "$SERVICE_NAME" \
        || die "systemctl enable --now ${SERVICE_NAME} не сработал. Смотри: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    systemctl restart "$SERVICE_NAME" || true
    ok "Сервис ${SERVICE_NAME} включён в автозапуск и запущен."
}

verify_service() {
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "Сервис ${SERVICE_NAME} активен."
        return 0
    fi
    err "Сервис ${SERVICE_NAME} НЕ активен."
    systemctl status "$SERVICE_NAME" --no-pager -n 20 2>/dev/null || true
    err "Частые причины: занят порт ${PORT}; ключ не читается пользователем ${SERVICE_USER}; бинарник не той архитектуры; опечатка в ${DOMAIN}."
    err "Смотри: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
}

apply_all() {
    require_root
    check_runtime_deps
    if [[ -z "$BINARY" ]]; then
        require_curl
    fi

    box "APPLY: РАЗВЁРТЫВАЮ DNS-ТУННЕЛЬ (dnstt) НА ЭТОМ СЕРВЕРЕ"

    if [[ -n "$BINARY" ]]; then
        BIN_PATH="$BINARY"
        [[ -f "$BIN_PATH" ]] || die "--binary: файл не найден: ${BIN_PATH}."
        check_sha256 "$BIN_PATH"
        probe_binary "$BIN_PATH"
        if [[ "$BIN_PATH" != "${BIN_DIR}/dnstt-server" ]]; then
            mkdir -p "$BIN_DIR" || die "Не удалось создать ${BIN_DIR}."
            cp -f "$BIN_PATH" "${BIN_DIR}/dnstt-server" || die "Не удалось скопировать ${BIN_PATH} в ${BIN_DIR}/dnstt-server."
            chmod 0755 "${BIN_DIR}/dnstt-server"
            ok "Бинарник установлен: ${BIN_DIR}/dnstt-server (из ${BIN_PATH})"
            BIN_PATH="${BIN_DIR}/dnstt-server"
        fi
    else
        download_binary
    fi

    ensure_user
    install_keys
    install_unit
    apply_iptables

    if ! check_delegation; then
        warn "Делегирование пока не подтверждено — это НЕ блокер (NS расходятся до 24 часов). Сервис всё равно поднимаю."
    fi

    systemd_up
    verify_service
    ok "Готово. Дальше — блок «КАК ПРОВЕРИТЬ» и проверка с телефона."
}

# ---------------------------------------------------------------- main --------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --domain)       need_value "$1" $#; DOMAIN="$2"; shift 2 ;;
            --ns-host)      need_value "$1" $#; NS_HOST="$2"; shift 2 ;;
            --server-ip)    need_value "$1" $#; SERVER_IP="$2"; shift 2 ;;
            --mode)         need_value "$1" $#; MODE="$2"; shift 2 ;;
            --mtu)          need_value "$1" $#; MTU="$2"; shift 2 ;;
            --port)         need_value "$1" $#; PORT="$2"; shift 2 ;;
            --ssh-port)     need_value "$1" $#; SSH_PORT="$2"; shift 2 ;;
            --binary)       need_value "$1" $#; BINARY="$2"; shift 2 ;;
            --sha256)       need_value "$1" $#; SHA256="$2"; shift 2 ;;
            --key-dir)      need_value "$1" $#; KEY_DIR="$2"; shift 2 ;;
            --user)         need_value "$1" $#; SERVICE_USER="$2"; shift 2 ;;
            --service-name) need_value "$1" $#; SERVICE_NAME="$2"; shift 2 ;;
            --check-dns)    CHECK_DNS=1; shift ;;
            --apply)        APPLY=1; shift ;;
            --dry-run)      APPLY=0; shift ;;
            --force)        FORCE=1; shift ;;
            -h | --help)    usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    validate_params
    print_summary

    if [[ "$CHECK_DNS" -eq 1 ]]; then
        check_dns_only
    fi

    if is_configured && [[ "$FORCE" -eq 0 ]]; then
        print_already_configured
        print_how_to_check
        print_whitedns_warning
        exit 0
    fi

    print_dns_plan
    print_keys_plan
    print_iptables_plan
    print_download_plan
    print_client_hints

    if [[ "$APPLY" -eq 1 ]]; then
        apply_all
    else
        dry_run_preview
    fi

    print_how_to_check
    print_whitedns_warning
}

main "$@"
