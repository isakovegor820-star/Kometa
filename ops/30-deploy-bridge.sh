#!/usr/bin/env bash
# =============================================================================
#  Kometa — День 0, шаг 6: «мост» — RU-VPS с достижимым адресом → выход за рубеж
# =============================================================================
#  Зачем отдельный скрипт, если есть install_node.sh + add_cascade_exit.sh.
#  Тот путь — про панель 3x-ui и про подписку на много клиентов. На Дне 0
#  клиентов трое, и нужен ровно один результат: три ссылки, каждая работает,
#  ядро v26.9.30, служба active. Панель здесь — лишняя сущность: лишний порт,
#  лишний логин, лишняя причина «не поднялось». Поэтому мост ставится
#  автономным Xray: конфиг в /usr/local/etc/xray/config.json, служба xray.
#
#  Схема:
#
#      телефон (Android/iOS/macOS, LTE или Wi-Fi)
#         │  TCP/443, VLESS + Reality + Vision, SNI = разрешённый домен
#         ▼
#      [МОСТ] RU-VPS с достижимым адресом          ← ЭТОТ скрипт
#         │  outbound «exit»: VLESS + Reality + Vision
#         ▼
#      [ВЫХОД] зарубежный сервер → интернет
#
#  Что делает скрипт (идемпотентно, повторный запуск безопасен):
#    1. ставит ядро Xray закреплённой версии (по умолчанию v26.9.30) —
#       официальным установщиком либо из локального архива (--xray-zip, для
#       случая «у моста нет доступа к GitHub»);
#    2. выключает IPv6 (приёмка требует пустой `ip -6 addr show scope global`);
#    3. генерирует ключи Reality (x25519) и shortId; ПРИ ПОВТОРНОМ ЗАПУСКЕ
#       КЛЮЧИ ПЕРЕИСПОЛЬЗУЮТСЯ из конфига и из файла-спутника — иначе у всех
#       трёх клиентов отвалились бы уже розданные ссылки;
#    4. пишет конфиг: инбаунд VLESS+Reality на 443 (N клиентов), outbound на
#       выход, блокировка приватных адресов, ПОСЛЕДНИМ правилом — всё на выход;
#    5. проверяет конфиг (`xray run -test`) ДО перезапуска, делает бэкап,
#       перезапускает службу и требует is-active = active;
#    6. печатает по ссылке на каждого клиента и ведёт реестр users.csv
#       (имя, UUID, дата выдачи, платформа, оператор, статус, дата отзыва);
#    7. подрезает MSS для транзитного TCP (LTE: MTU 1280 → MSS 1240) — без этого
#       «сайты висят» при живом SSH (docs/LTE-ВАРИАНТЫ-2026-10.md §6.4);
#    8. печатает готовые команды для выходной ноды (allowlist «только мой мост»).
#
#  Отзыв ключа (второй UUID — не трогая остальных): убрать его из --client-uuid
#  и запустить скрипт снова. Конфиг изменится → xray перезапустится → ссылка
#  отозванного перестанет работать, в реестре он станет «отозван».
#
#  Чего скрипт НЕ делает:
#    * не трогает выходную ноду и не ходит в панель;
#    * не выдаёт одну ссылку на всех: дубли UUID — ошибка (--client-uuid без
#      своего UUID у каждого клиента запрещён);
#    * не отправляет ничего наружу: ни телеметрии, ни «проверок», ни писем;
#    * не публикует адрес и ссылки — печатает их только в консоль;
#    * не ставит fp=chrome/safari/ios (эвристика июня 2026 их помечает) —
#      по умолчанию firefox;
#    * в --dry-run не пишет НИЧЕГО и не делает ни одного сетевого запроса.
#
#  Зависимости: bash, jq (обязательно), openssl (shortId), curl (только
#  установка из сети), unzip (только --xray-zip), systemd, iptables (MSS).
#  Запускать от root: `bash /root/30-deploy-bridge.sh ...` (копия на мосте).
#
#  Пример (боевой, как в плане Дня 0):
#      bash /root/30-deploy-bridge.sh \
#        --exit-address <EXIT_ADDRESS> --exit-port <EXIT_PORT> \
#        --exit-uuid <EXIT_UUID> --exit-pubkey <EXIT_PUBKEY> \
#        --exit-sni <EXIT_SNI> --exit-shortid <EXIT_SHORTID> \
#        --client-uuid <UUID_1> --client-uuid <UUID_2> --client-uuid <UUID_3>
#
#  Пример (с реестром и предпросмотром):
#      bash 30-deploy-bridge.sh --dry-run --exit-address 203.0.113.9 ... \
#        --client "<UUID_1>,Иван,Android/Happ,МТС" \
#        --client "<UUID_2>,Пётр,iOS/Streisand,Билайн" \
#        --client "<UUID_3>,Аня,macOS/v2rayN,МегаФон"
#
#  Код возврата: 0 — мост поднят и active; 1 — не поднялся (подробности выше).
# =============================================================================

set -uo pipefail

PROG="$(basename "$0")"
DEFAULT_XRAY_VERSION="v26.9.30"
CONFIG_DEFAULT="/usr/local/etc/xray/config.json"
XRAY_BIN_DEFAULT="/usr/local/bin/xray"
REGISTRY_DEFAULT="/root/users.csv"
UUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
SID_RE='^[0-9a-fA-F]{0,16}$'

# ------------------------------------------------------------------ значения --
EXIT_ADDRESS=""
EXIT_PORT=""
EXIT_UUID=""
EXIT_PUBKEY=""
EXIT_SNI=""
EXIT_SHORTID=""
EXIT_FP="firefox"
EXIT_FLOW="xtls-rprx-vision"
ALLOW_EXIT_HOSTNAME=0

BRIDGE_ADDRESS=""
BRIDGE_PORT=443
BRIDGE_SNI=""
BRIDGE_SNI_EXTRA=""
BRIDGE_PUBKEY=""
BRIDGE_PRIVKEY=""
BRIDGE_SHORTID=""
MIN_CLIENT_VER="0.0.0"

XRAY_VERSION="$DEFAULT_XRAY_VERSION"
XRAY_VERSION_ACTUAL=""
XRAY_BIN=""
XRAY_ZIP=""
NO_INSTALL=0
ALLOW_OTHER_VERSION=0
ALLOW_NON_ROOT=0

CONFIG="$CONFIG_DEFAULT"
REGISTRY="$REGISTRY_DEFAULT"
NO_REGISTRY=0

DIRECT_RU=0
DIRECT_FILE=""
LTE_MTU=1280
DO_MSS=1
KEEP_IPV6=0
HARDEN_FIREWALL=0
SSH_PORT=22

DRY_RUN=0
LINKS_ONLY=0
ROTATE_KEYS=0
ROTATE_SHORTID=0

# Клиенты — четыре параллельных массива (bash 3.2 на macOS не знает
# ассоциативных массивов, а скрипт должен прогоняться в тестах и на маке).
C_UUIDS=()
C_NAMES=()
C_PLATFORMS=()
C_OPERATORS=()

TMPFILES=()
REG_ACTIVE=0
REG_REVOKED=0
REG_ADDED=0
REG_REVOKED_NOW=""

# --------------------------------------------------------------------- вывод --
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
note() { printf '%s\n' "$*"; }

box() {
    printf '\n%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
}

have() { command -v "$1" >/dev/null 2>&1; }

# Временный файл: mktemp есть и на мосте, и на маке; путь запоминаем, чтобы
# гарантированно убрать за собой.
tmp_file() {
    local f
    f="$(mktemp 2>/dev/null || printf '%s/kometa-bridge.%s.%s' "${TMPDIR:-/tmp}" "$$" "${RANDOM:-0}")"
    TMPFILES+=("$f")
    printf '%s' "$f"
}

cleanup() {
    local f
    for f in ${TMPFILES[@]+"${TMPFILES[@]}"}; do
        [[ -n "$f" ]] && rm -rf "$f" 2>/dev/null
    done
    return 0
}
trap cleanup EXIT

# ------------------------------------------------------------------ справка ---
usage() {
    cat <<'EOF'
Kometa · День 0 · шаг 6: мост «RU-адрес → зарубежный выход» на автономном Xray.

Мост принимает клиентов (VLESS + Reality + Vision, TCP/443) и целиком уводит
их трафик на зарубежный выход. Панель не нужна: конфиг и служба xray.

Использование:
  bash 30-deploy-bridge.sh --exit-address <IP> --exit-port <PORT> \
      --exit-uuid <UUID_ВЫХОДА> --exit-pubkey <PUBLIC_KEY_ВЫХОДА> \
      --exit-sni <SNI_ВЫХОДА> --exit-shortid <SHORT_ID_ВЫХОДА> \
      --client-uuid <UUID_1> --client-uuid <UUID_2> --client-uuid <UUID_3>

По умолчанию скрипт ПРИМЕНЯЕТ изменения на хосте. Предпросмотр без изменений:
  ... --dry-run      # ничего не пишет, в сеть не ходит, systemctl не зовёт

Выход (обязательно):
  --exit-address IP     адрес зарубежного выхода. Только IP: имя заставило бы
                        мост резолвить его самому (лишний DNS на мосте).
  --exit-port N         порт выхода (обычно 443)
  --exit-uuid UUID      UUID клиента моста на выходе
  --exit-pubkey KEY     publicKey Reality выхода (x25519, 43 символа base64url)
  --exit-sni DOMAIN     serverName Reality выхода (домен маскировки)
  --exit-shortid SID    shortId Reality выхода (hex, до 16 символов; пусто — без)
  --exit-fp FP          uTLS-отпечаток (по умолчанию firefox; chrome/safari/ios
                        в эвристике июня 2026 помечены)
  --exit-flow FLOW      flow клиента на выходе (по умолчанию xtls-rprx-vision)
  --allow-exit-hostname разрешить в --exit-address домен, а не IP (не советуем)

Клиенты (минимум один):
  --client-uuid UUID    добавить клиента с этим UUID (повторяемый)
  --client "UUID[,Имя[,Платформа[,Оператор]]]"
                        клиент вместе с данными для реестра users.csv
                        (пример: "1111...-....,Иван,Android/Happ,МТС")
  Дубли UUID запрещены: одна ссылка на всех — это одна точка отказа и один
  «отозванный» на всех.

Мост:
  --bridge-address IP   адрес моста, который попадёт в ссылки (по умолчанию —
                        определяется по маршруту к 1.1.1.1, без внешних сервисов)
  --bridge-port N       порт моста (по умолчанию 443; другой порт в режиме
                        белых списков почти наверняка не пройдёт)
  --bridge-sni DOMAIN   SNI/домен маскировки моста (по умолчанию — как у выхода;
                        при повторном запуске берётся из существующего конфига)
  --bridge-sni-extra L  запасные SNI через запятую (ротация без перевыпуска)
  --bridge-pubkey KEY   publicKey моста вручную (иначе из файла-спутника/ядра)
  --min-client-ver V    minClientVer Reality (по умолчанию 0.0.0 = без минимума;
                        пусто — поле не писать)
  --rotate-keys         перевыпустить ключи Reality моста (ВСЕ ссылки изменятся)
  --rotate-shortid      перевыпустить shortId моста (все ссылки изменятся)
  --direct-ru           РФ-трафик — напрямую с моста (geoip:ru + geosite:ru).
                        Тогда мост сам резолвит РФ-домены: на мосте появится
                        трафик 53. Для Дня 0 по умолчанию ВЫКЛЮЧЕНО — весь
                        трафик уходит на выход, и порт 53 на мосте молчит.
  --direct-file FILE    свой список «напрямую» (по строке: geosite:/geoip:/CIDR)

Ядро и служба:
  --xray-version V      версия ядра (по умолчанию v26.9.30)
  --xray-bin PATH       путь к ядру (по умолчанию /usr/local/bin/xray)
  --no-install          не ставить ядро, использовать существующее
  --xray-zip FILE       поставить ядро из локального архива (нет GitHub)
  --allow-other-version не считать ошибкой другую версию ядра
  --allow-non-root      отключить проверку root (только прогоны/тесты, не мост)
  --config PATH         путь конфига (по умолчанию /usr/local/etc/xray/config.json)

Реестр:
  --registry PATH       путь к реестру (по умолчанию /root/users.csv)
  --no-registry         не вести реестр

Прочее:
  --lte-mtu N           MTU для расчёта MSS (по умолчанию 1280; домашним 1420)
  --no-mss              не настраивать MSS-clamp
  --keep-ipv6           не выключать IPv6 (по умолчанию выключается)
  --harden-firewall     включить ufw: только SSH и порт моста (по умолчанию
                        скрипт лишь показывает правила и не трогает фаервол)
  --ssh-port N          SSH-порт для --harden-firewall и подсказок (по умолчанию 22)
  --dry-run             предпросмотр: ничего не пишет и не ходит в сеть
  --links-only          только напечатать ссылки по существующему конфигу
  -h, --help            эта справка

Пример предпросмотра (безопасен: ни файлов, ни сети):
  bash 30-deploy-bridge.sh --dry-run --exit-address 203.0.113.9 --exit-port 443 \
    --exit-uuid 11111111-2222-3333-4444-555555555555 \
    --exit-pubkey AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
    --exit-sni www.chip.de --exit-shortid 0123456789abcdef \
    --client-uuid 00000000-0000-0000-0000-000000000001 \
    --client-uuid 00000000-0000-0000-0000-000000000002 \
    --client-uuid 00000000-0000-0000-0000-000000000003

Отзыв ключа: убрать UUID из --client-uuid и запустить скрипт снова.
EOF
}

# ------------------------------------------------------------- разбор флагов --
need() { # need <флаг> <число аргументов>
    [[ "$2" -ge 2 ]] || die "$1: не хватает значения (см. --help)"
}

add_client_full() { # add_client_full "UUID[,Имя[,Платформа[,Оператор]]]"
    local raw="$1" u n p o fields
    # Формат разделён запятыми, значит запятых внутри полей быть не может: без
    # этой проверки «Иван,Пётр» в поле имени молча сдвинуло бы всю строку, и в
    # реестр попало бы «платформа = Пётр, оператор = Android/Happ,МТС».
    fields="$(printf '%s' "$raw" | awk -F, '{print NF}')"
    [[ "$fields" -le 4 ]] || die "--client: полей больше четырёх — запятые внутри имени/платформы не поддерживаются, замени их на «;» (получено: «${raw}»)"
    IFS=, read -r u n p o <<<"$raw"
    u="$(trim "$u")"; n="$(trim "$n")"; p="$(trim "$p")"; o="$(trim "$o")"
    [[ -n "$u" ]] || die "--client: первым полем должен быть UUID (получено: «${raw}»)"
    add_client "$u" "$n" "$p" "$o"
}

add_client() { # add_client <uuid> [имя] [платформа] [оператор] — повторный UUID обновляет запись
    local u="$1" n="${2:-}" p="${3:-}" o="${4:-}" i=0
    while [[ $i -lt ${#C_UUIDS[@]} ]]; do
        if [[ "${C_UUIDS[$i]}" == "$u" ]]; then
            # Повтор без новых данных — это почти наверняка попытка выдать одну
            # ссылку нескольким людям. Отдельного клиента не создаём (иначе один
            # отзыв рвал бы всех), но говорим прямо.
            if [[ -z "$n" && -z "$p" && -z "$o" ]]; then
                warn "UUID ${u} указан повторно — это ОДИН клиент, а не два. Разным людям нужны разные UUID: одна ссылка на всех запрещена."
            fi
            [[ -n "$n" ]] && C_NAMES[$i]="$n"
            [[ -n "$p" ]] && C_PLATFORMS[$i]="$p"
            [[ -n "$o" ]] && C_OPERATORS[$i]="$o"
            return 0
        fi
        i=$((i + 1))
    done
    C_UUIDS+=("$u"); C_NAMES+=("$n"); C_PLATFORMS+=("$p"); C_OPERATORS+=("$o")
}

trim() { printf '%s' "$1" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'; }

parse_args() {
    local ARGS=() arg
    for arg in ${1+"$@"}; do
        case "$arg" in
            # --flag=value → --flag value (чтобы длинные значения было удобно
            # копировать и чтобы `--client="..."` с запятыми не ломался)
            --*=*) ARGS+=("${arg%%=*}" "${arg#*=}") ;;
            *)     ARGS+=("$arg") ;;
        esac
    done
    set -- ${ARGS[@]+"${ARGS[@]}"}

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --exit-address)     need "$1" $#; EXIT_ADDRESS="$2"; shift 2 ;;
            --exit-port)        need "$1" $#; EXIT_PORT="$2"; shift 2 ;;
            --exit-uuid)        need "$1" $#; EXIT_UUID="$2"; shift 2 ;;
            --exit-pubkey)      need "$1" $#; EXIT_PUBKEY="$2"; shift 2 ;;
            --exit-sni)         need "$1" $#; EXIT_SNI="$2"; shift 2 ;;
            --exit-shortid)     need "$1" $#; EXIT_SHORTID="$2"; shift 2 ;;
            --exit-fp)          need "$1" $#; EXIT_FP="$2"; shift 2 ;;
            --exit-flow)        need "$1" $#; EXIT_FLOW="$2"; shift 2 ;;
            --allow-exit-hostname) ALLOW_EXIT_HOSTNAME=1; shift ;;
            --client-uuid)      need "$1" $#; add_client "$2"; shift 2 ;;
            --client)           need "$1" $#; add_client_full "$2"; shift 2 ;;
            --bridge-address)   need "$1" $#; BRIDGE_ADDRESS="$2"; shift 2 ;;
            --bridge-port)      need "$1" $#; BRIDGE_PORT="$2"; shift 2 ;;
            --bridge-sni)       need "$1" $#; BRIDGE_SNI="$2"; shift 2 ;;
            --bridge-sni-extra) need "$1" $#; BRIDGE_SNI_EXTRA="$2"; shift 2 ;;
            --bridge-pubkey)    need "$1" $#; BRIDGE_PUBKEY="$2"; shift 2 ;;
            --min-client-ver)   need "$1" $#; MIN_CLIENT_VER="$2"; shift 2 ;;
            --rotate-keys)      ROTATE_KEYS=1; shift ;;
            --rotate-shortid)   ROTATE_SHORTID=1; shift ;;
            --direct-ru)        DIRECT_RU=1; shift ;;
            --direct-file)      need "$1" $#; DIRECT_FILE="$2"; DIRECT_RU=1; shift 2 ;;
            --xray-version)     need "$1" $#; XRAY_VERSION="$2"; shift 2 ;;
            --xray-bin)         need "$1" $#; XRAY_BIN="$2"; shift 2 ;;
            --no-install)       NO_INSTALL=1; shift ;;
            --xray-zip)         need "$1" $#; XRAY_ZIP="$2"; shift 2 ;;
            --allow-other-version) ALLOW_OTHER_VERSION=1; shift ;;
            --allow-non-root)   ALLOW_NON_ROOT=1; shift ;;
            --config)           need "$1" $#; CONFIG="$2"; shift 2 ;;
            --registry)         need "$1" $#; REGISTRY="$2"; shift 2 ;;
            --no-registry)      NO_REGISTRY=1; shift ;;
            --lte-mtu)          need "$1" $#; LTE_MTU="$2"; shift 2 ;;
            --no-mss)           DO_MSS=0; shift ;;
            --keep-ipv6)        KEEP_IPV6=1; shift ;;
            --harden-firewall)  HARDEN_FIREWALL=1; shift ;;
            --ssh-port)         need "$1" $#; SSH_PORT="$2"; shift 2 ;;
            --dry-run)          DRY_RUN=1; shift ;;
            --links-only)       LINKS_ONLY=1; shift ;;
            -h|--help)          usage; exit 0 ;;
            *)                  die "Неизвестный аргумент: «$1» (см. --help)" ;;
        esac
    done
}

# ---------------------------------------------------------------- проверки ---
is_ipv4() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

validate_exit_params() {
    [[ -n "$EXIT_ADDRESS" ]] || die "Обязателен --exit-address (IP зарубежного выхода)."
    [[ -n "$EXIT_PORT" ]]    || die "Обязателен --exit-port."
    [[ -n "$EXIT_UUID" ]]    || die "Обязателен --exit-uuid (UUID клиента моста на выходе)."
    [[ -n "$EXIT_PUBKEY" ]]  || die "Обязателен --exit-pubkey (publicKey Reality выхода)."
    [[ -n "$EXIT_SNI" ]]     || die "Обязателен --exit-sni (serverName Reality выхода)."

    if ! is_ipv4 "$EXIT_ADDRESS"; then
        if [[ "$ALLOW_EXIT_HOSTNAME" -eq 1 ]]; then
            warn "--exit-address «${EXIT_ADDRESS}» — не IPv4: мост будет резолвить это имя сам (трафик 53 на мосте)."
        else
            die "--exit-address «${EXIT_ADDRESS}» не похож на IPv4. Имя заставило бы мост резолвить его локально; если это осознанно — добавь --allow-exit-hostname."
        fi
    fi

    [[ "$EXIT_PORT" =~ ^[0-9]+$ ]] || die "--exit-port должен быть числом, получено: «${EXIT_PORT}»"
    [[ "$EXIT_PORT" -ge 1 && "$EXIT_PORT" -le 65535 ]] || die "--exit-port вне диапазона 1..65535: ${EXIT_PORT}"
    [[ "$EXIT_UUID" =~ $UUID_RE ]] || die "--exit-uuid не похож на UUID (8-4-4-4-12 hex): ${EXIT_UUID}"

    case "$EXIT_FLOW" in
        "" | xtls-rprx-vision | xtls-rprx-vision-udp443) : ;;
        *) die "--exit-flow «${EXIT_FLOW}» не поддерживается (xtls-rprx-vision, xtls-rprx-vision-udp443 или пусто)." ;;
    esac
    case "$EXIT_FP" in
        chrome | safari | ios | ios14 | ios15)
            warn "Отпечаток «${EXIT_FP}» в чёрном списке эвристики июня 2026 — рекомендуется firefox/edge/android." ;;
    esac
    case "$EXIT_PUBKEY" in
        *[!A-Za-z0-9_=-]* | "") warn "publicKey выхода «${EXIT_PUBKEY}» выглядит необычно (обычно 43 символа base64url)." ;;
    esac
}

validate_bridge_params() {
    [[ "$BRIDGE_PORT" =~ ^[0-9]+$ ]] || die "--bridge-port должен быть числом, получено: «${BRIDGE_PORT}»"
    [[ "$BRIDGE_PORT" -ge 1 && "$BRIDGE_PORT" -le 65535 ]] || die "--bridge-port вне диапазона 1..65535: ${BRIDGE_PORT}"
    [[ "$BRIDGE_PORT" -eq 443 ]] || warn "Мост на порту ${BRIDGE_PORT}: в режиме белых списков проходит только 443. Порт оставлен как просили."

    [[ -n "$BRIDGE_SNI" ]] || BRIDGE_SNI="$EXIT_SNI"
    case "$BRIDGE_SNI" in
        *[!A-Za-z0-9.-]* | "") die "--bridge-sni «${BRIDGE_SNI}» не похож на домен." ;;
    esac

    [[ -n "$MIN_CLIENT_VER" ]] && [[ "$MIN_CLIENT_VER" =~ ^[0-9]+(\.[0-9]+){1,3}$ ]] \
        || [[ -z "$MIN_CLIENT_VER" ]] \
        || die "--min-client-ver «${MIN_CLIENT_VER}» не похож на версию (например 0.0.0)."

    [[ "$LTE_MTU" =~ ^[0-9]+$ ]] || die "--lte-mtu должен быть числом."
    [[ "$LTE_MTU" -ge 576 && "$LTE_MTU" -le 1500 ]] || die "--lte-mtu вне разумного диапазона 576..1500."

    [[ -n "$BRIDGE_SNI_EXTRA" ]] && log "Запасные SNI попадут в serverNames инбаунда: клиенты смогут переключать домен маскировки без перевыпуска инбаунда."

    local i=0
    while [[ $i -lt ${#C_UUIDS[@]} ]]; do
        [[ "${C_UUIDS[$i]}" =~ $UUID_RE ]] || die "Клиент №$((i + 1)): «${C_UUIDS[$i]}» не UUID."
        i=$((i + 1))
    done
}

validate_clients() {
    [[ ${#C_UUIDS[@]} -ge 1 ]] || die "Нужен хотя бы один клиент: --client-uuid <UUID> (или --client \"UUID,Имя,Платформа,Оператор\")."
    [[ ${#C_UUIDS[@]} -le 32 ]] || warn "Клиентов ${#C_UUIDS[@]}: это много для одного моста Дня 0 (запас 3–5)."
}

need_jq() { have jq || die "Нужен jq (на мосте: apt-get install -y jq)."; }

require_root_for_apply() {
    [[ "$DRY_RUN" -eq 1 || "$LINKS_ONLY" -eq 1 ]] && return 0
    if [[ "$ALLOW_NON_ROOT" -eq 1 ]]; then
        warn "Проверка root отключена (--allow-non-root): режим прогонов на машине разработчика, не для моста."
        return 0
    fi
    [[ "$(id -u)" -eq 0 ]] || die "Применение требует root (запускай от root или добавь --dry-run для предпросмотра)."
}

# --------------------------------------------------------------- окружение ---
detect_bridge_address() {
    local ip=""
    if have ip; then
        ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
    fi
    if [[ -z "$ip" ]] && have hostname; then
        ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -1)"
    fi
    if [[ -z "$ip" ]] && have ifconfig; then
        ip="$(ifconfig 2>/dev/null | sed -n 's/.*inet \([0-9.]*\) .*/\1/p' | grep -v '^127\.' | head -1)"
    fi
    printf '%s' "$ip"
}

detect_ssh_port() {
    local p=""
    if [[ -r /etc/ssh/sshd_config ]]; then
        p="$(sed -n 's/^[[:space:]]*Port[[:space:]]\+\([0-9]\+\).*/\1/p' /etc/ssh/sshd_config | head -1)"
    fi
    printf '%s' "${p:-22}"
}

find_xray() {
    if [[ -n "$XRAY_BIN" && -x "$XRAY_BIN" ]]; then printf '%s' "$XRAY_BIN"; return 0; fi
    local p
    for p in "$XRAY_BIN_DEFAULT" /usr/bin/xray /usr/local/xray/xray; do
        [[ -x "$p" ]] && { printf '%s' "$p"; return 0; }
    done
    if have xray; then command -v xray; return 0; fi
    return 1
}

xray_version_of() { # <bin> → vX.Y.Z (пусто, если не разобрал)
    # Формат вывода ядра: «Xray 26.9.30 (Xray, Penetrates Everything.) …» —
    # версия вторым полем. Ловить «после слова Xray» регуляркой нельзя: в
    # строке есть ещё «(Xray, Penetrates Everything.)», и жадный шаблон уходит
    # именно туда, возвращая пустоту.
    local out
    out="$("$1" version 2>/dev/null | head -1 | awk '{print $2}')"
    out="${out#v}"
    if [[ "$out" =~ ^[0-9]+\.[0-9]+ ]]; then printf 'v%s' "$out"; fi
    return 0
}

sha256_file() { # <файл> → хеш
    if have sha256sum; then sha256sum "$1" | awk '{print $1}'
    elif have shasum; then shasum -a 256 "$1" | awk '{print $1}'
    else cksum "$1" | awk '{print $1"-"$2}'
    fi
}

urlenc() { printf '%s' "$1" | jq -sRr @uri 2>/dev/null || printf '%s' "$1"; }

# --------------------------------------------------------- ядро: установка ---
install_xray_from_network() {
    local url="https://github.com/XTLS/Xray-install/raw/main/install-release.sh" tmp
    have curl || die "Нужен curl для установки ядра. Без сети на мосте: скачай архив Xray локально и запусти с --xray-zip <файл>."
    tmp="$(tmp_file)"
    log "Скачиваю официальный установщик Xray…"
    curl -fsSL "$url" -o "$tmp" \
        || die "Не скачал install-release.sh. Если у моста нет доступа к GitHub — скачай архив Xray (Xray-linux-64.zip) на своей машине, перенеси на мост и запусти с --xray-zip /root/xray.zip."
    log "Ставлю Xray ${XRAY_VERSION}…"
    bash "$tmp" install --version "${XRAY_VERSION#v}" \
        || die "Установщик Xray завершился ошибкой (см. его вывод выше)."
    ok "Ядро установлено: ${XRAY_VERSION}"
}

install_xray_from_zip() {
    local zip="$1" dir
    have unzip || die "Для --xray-zip нужен unzip (apt-get install -y unzip)."
    [[ -f "$zip" ]] || die "--xray-zip: файл не найден: ${zip}"
    dir="$(tmp_file).d"
    mkdir -p "$dir"
    unzip -o -q "$zip" -d "$dir" || die "Не распаковал ${zip}."
    [[ -f "$dir/xray" ]] || die "В архиве нет файла xray (нужен Xray-linux-64.zip с bin/xray)."
    mkdir -p "$(dirname "$XRAY_BIN_DEFAULT")" /usr/local/etc/xray /usr/local/share/xray /var/log/xray
    install -m 0755 "$dir/xray" "$XRAY_BIN_DEFAULT" || die "Не положил ядро в ${XRAY_BIN_DEFAULT}."
    local f
    for f in geoip.dat geosite.dat; do
        [[ -f "$dir/$f" ]] && install -m 0644 "$dir/$f" "/usr/local/share/xray/$f"
    done
    write_unit_if_missing
    ok "Ядро установлено из архива: ${zip}"
}

write_unit_if_missing() {
    local unit="/etc/systemd/system/xray.service"
    [[ -f "$unit" ]] && { log "Юнит ${unit} уже есть — не трогаю."; return 0; }
    cat >"$unit" <<'EOF'
[Unit]
Description=Xray Service
Documentation=https://github.com/xtls
After=network.target nss-lookup.target

[Service]
User=root
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartPreventExitStatus=23
LimitNPROC=10000
LimitNOFILE=1000000

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload не сработал."
    ok "Создан юнит ${unit}."
}

# Ставит/находит ядро и кладёт фактическую версию в XRAY_VERSION_ACTUAL.
# Логи идут в stdout, поэтому «печатать версию в stdout» здесь нельзя:
# вызывающий код не должен ловить лог вместе со значением.
ensure_xray() {
    local found version_actual
    if found="$(find_xray)"; then
        XRAY_BIN="$found"
    else
        [[ "$NO_INSTALL" -eq 1 ]] && die "Ядро не найдено, а --no-install запрещает установку."
        if [[ -n "$XRAY_ZIP" ]]; then install_xray_from_zip "$XRAY_ZIP"; else install_xray_from_network; fi
        XRAY_BIN="$(find_xray)" || die "После установки ядро всё ещё не найдено в ${XRAY_BIN_DEFAULT}."
    fi
    systemctl enable xray >/dev/null 2>&1 || true
    version_actual="$(xray_version_of "$XRAY_BIN")"
    [[ -n "$version_actual" ]] || version_actual="(не разобрал вывод)"
    if [[ "$version_actual" != "$XRAY_VERSION" ]]; then
        if [[ "$ALLOW_OTHER_VERSION" -eq 1 ]]; then
            warn "Версия ядра ${version_actual}, ожидалась ${XRAY_VERSION} — продолжаю по --allow-other-version."
        else
            die "Версия ядра ${version_actual}, а нужна ${XRAY_VERSION}. Поставь нужную или добавь --allow-other-version."
        fi
    fi
    XRAY_VERSION_ACTUAL="$version_actual"
}

# ------------------------------------------------------------- ключи Reality --
meta_file() { printf '%s' "${CONFIG%.json}.bridge.json"; }

meta_get() { # meta_get <ключ>
    local f; f="$(meta_file)"
    [[ -f "$f" ]] || return 1
    jq -r --arg k "$1" '.[$k] // empty' "$f" 2>/dev/null
}

# Наш инбаунд из существующего конфига. Именно по тегу, а не «inbounds[0]»:
# официальный установщик Xray кладёт свой демо-конфиг, и первый инбаунд там
# может быть чужим (socks/dokodemo) — тогда ключи и SNI читались бы не оттуда,
# и повторный запуск молча выпустил бы НОВЫЕ ключи, порвав розданные ссылки.
existing_inbound_json() { # existing_inbound_json <файл> → JSON инбаунда (или пусто)
    [[ -f "$1" ]] || return 1
    jq -c '
        if any(.inbounds[]?; .tag == "bridge-in")
        then (.inbounds[] | select(.tag == "bridge-in"))
        else (.inbounds[0] // empty) end' "$1" 2>/dev/null
}

inbound_field() { # inbound_field <файл> <jq-путь внутри инбаунда>
    local json
    json="$(existing_inbound_json "$1")" || return 1
    [[ -n "$json" && "$json" != "null" ]] || return 1
    printf '%s' "$json" | jq -r "$2 // empty" 2>/dev/null
}

derive_public_key() { # derive_public_key <bin> <private> → public
    local out pub
    out="$("$1" x25519 -i "$2" 2>/dev/null)" || return 1
    pub="$(printf '%s\n' "$out" | sed -n '2s/^[^:]*:[[:space:]]*//p')"
    [[ -n "$pub" ]] || return 1
    printf '%s' "$pub"
}

generate_keypair() { # → "PRIVATE PUBLIC"
    local bin out priv pub
    bin="$(find_xray)" || return 1
    out="$("$bin" x25519 2>/dev/null)" || return 1
    priv="$(printf '%s\n' "$out" | sed -n '1s/^[^:]*:[[:space:]]*//p')"
    pub="$(printf '%s\n' "$out" | sed -n '2s/^[^:]*:[[:space:]]*//p')"
    [[ -n "$priv" && -n "$pub" ]] || return 1
    printf '%s %s' "$priv" "$pub"
}

rand_shortid() { openssl rand -hex 8 2>/dev/null || printf '%04x%04x%04x%04x' "$RANDOM" "$RANDOM" "$RANDOM" "$RANDOM"; }

resolve_keys() {
    local keys existing_sni bin

    # 1. Ключи и shortId — из существующего конфига (иначе розданные ссылки
    #    отвалились бы при каждом повторном запуске скрипта).
    if [[ "$ROTATE_KEYS" -eq 0 && -f "$CONFIG" ]]; then
        BRIDGE_PRIVKEY="$(inbound_field "$CONFIG" '.streamSettings.realitySettings.privateKey' || true)"
        [[ -n "$BRIDGE_PRIVKEY" ]] && log "Приватный ключ моста взят из существующего конфига (без --rotate-keys)."
    fi
    if [[ "$ROTATE_SHORTID" -eq 0 && -z "$BRIDGE_SHORTID" ]]; then
        if [[ -f "$CONFIG" ]]; then
            BRIDGE_SHORTID="$(inbound_field "$CONFIG" '.streamSettings.realitySettings.shortIds[0]' || true)"
        fi
        [[ -z "$BRIDGE_SHORTID" ]] && BRIDGE_SHORTID="$(meta_get shortId || true)"
    fi
    # SNI моста: явный флаг → существующий конфиг → SNI выхода.
    if [[ -z "$BRIDGE_SNI" && -f "$CONFIG" ]]; then
        existing_sni="$(inbound_field "$CONFIG" '.streamSettings.realitySettings.serverNames[0]' || true)"
        if [[ -n "$existing_sni" ]]; then
            BRIDGE_SNI="$existing_sni"
            log "SNI моста взят из существующего конфига: ${BRIDGE_SNI}"
        fi
    fi

    # 2. Генерация того, чего нет.
    if [[ -z "$BRIDGE_SHORTID" ]] || [[ "$ROTATE_SHORTID" -eq 1 ]]; then
        BRIDGE_SHORTID="$(rand_shortid)"
        [[ "$ROTATE_SHORTID" -eq 1 ]] && warn "shortId перевыпущен — все ранее розданные ссылки перестанут работать."
    fi
    if [[ -z "$BRIDGE_PRIVKEY" ]] || [[ "$ROTATE_KEYS" -eq 1 ]]; then
        if keys="$(generate_keypair)"; then
            BRIDGE_PRIVKEY="${keys%% *}"
            BRIDGE_PUBKEY="${keys##* }"
            [[ "$ROTATE_KEYS" -eq 1 ]] && warn "Ключи Reality перевыпущены — все ранее розданные ссылки перестанут работать."
        elif [[ "$DRY_RUN" -eq 1 ]]; then
            BRIDGE_PRIVKEY="<PRIVATE_KEY-NO-CORE-IN-DRY-RUN>"
            BRIDGE_PUBKEY="<PUBLIC_KEY-NO-CORE-IN-DRY-RUN>"
            warn "Ядра на этой машине нет: в dry-run ключи показаны заглушкой. На мосте они будут настоящими."
        else
            die "Не удалось сгенерировать ключи x25519 (нет ядра). Проверь ${XRAY_BIN_DEFAULT}."
        fi
    fi

    # 3. Публичный ключ: явный флаг → спутник → вывод из приватного.
    if [[ -z "$BRIDGE_PUBKEY" ]]; then
        BRIDGE_PUBKEY="$(meta_get publicKey || true)"
    fi
    if [[ -z "$BRIDGE_PUBKEY" ]] && [[ "$BRIDGE_PRIVKEY" != \<* ]]; then
        if bin="$(find_xray)"; then
            BRIDGE_PUBKEY="$(derive_public_key "$bin" "$BRIDGE_PRIVKEY" || true)"
        fi
    fi
    if [[ -z "$BRIDGE_PUBKEY" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            BRIDGE_PUBKEY="<PUBLIC_KEY-NO-CORE-IN-DRY-RUN>"
        else
            die "Не могу восстановить publicKey моста. Передай --bridge-pubkey <KEY> или --rotate-keys."
        fi
    fi
}

write_meta() { # спутник: публичные параметры моста, чтобы ссылки можно было перепечатать
    local f; f="$(meta_file)"
    # --config может указывать в ещё не созданный каталог: спутник пишется
    # раньше конфига, поэтому каталог создаём здесь.
    mkdir -p "$(dirname "$f")" 2>/dev/null || true
    jq -nc --arg pub "$BRIDGE_PUBKEY" --arg sid "$BRIDGE_SHORTID" --arg sni "$BRIDGE_SNI" \
        --arg addr "$BRIDGE_ADDRESS" --argjson port "$BRIDGE_PORT" --arg ver "$XRAY_VERSION" \
        --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{v:1, publicKey:$pub, shortId:$sid, serverName:$sni, address:$addr, port:$port, xray:$ver, updatedAt:$at}' >"$f" \
        || die "Не записал файл-спутник ${f}."
    chmod 600 "$f" 2>/dev/null || true
}

# ------------------------------------------------------------------- конфиг ---
build_clients_json() {
    local i=0 rows="" out
    while [[ $i -lt ${#C_UUIDS[@]} ]]; do
        rows="${rows}${C_UUIDS[$i]}"$'\t'"client-$((i + 1))"$'\n'
        i=$((i + 1))
    done
    out="$(printf '%s' "$rows" | sed '/^$/d' | jq -R -s --arg flow "$EXIT_FLOW" '
        split("\n") | map(select(length > 0) | split("\t"))
        | map({ id: .[0], email: (.[1] // ""), flow: $flow })')" || return 1
    printf '%s' "$out"
}

direct_tokens() { # → JSON-массив строк «напрямую»
    local raw=""
    if [[ -n "$DIRECT_FILE" ]]; then
        [[ -r "$DIRECT_FILE" ]] || die "--direct-file: файл не читается: ${DIRECT_FILE}"
        raw="$(tr -d '\r' <"$DIRECT_FILE" | sed -E 's/#.*$//' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | sed '/^$/d')"
        [[ -n "$raw" ]] || die "--direct-file ${DIRECT_FILE}: после удаления комментариев не осталось строк."
    else
        raw="$(printf 'geosite:category-ru\ngeoip:ru\ndomain:ru\n')"
    fi
    printf '%s\n' "$raw" | jq -R -s 'split("\n") | map(select(length > 0))'
}

build_rules_json() {
    local tokens="[]"
    [[ "$DIRECT_RU" -eq 1 ]] && tokens="$(direct_tokens)"
    jq -nc --argjson tokens "$tokens" '
      def ipish:
        startswith("geoip:")
        or test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}(/[0-9]{1,2})?$")
        or test("^[0-9a-fA-F:.]+/[0-9]{1,3}$");
      [ { type:"field", ruleTag:"Kometa-Block-Private", inboundTag:["bridge-in"],
          ip:["geoip:private","169.254.0.0/16","fd00::/8"], outboundTag:"block" } ]
      + (if ($tokens | length) > 0 then
           [ { type:"field", ruleTag:"Kometa-Direct", inboundTag:["bridge-in"], outboundTag:"direct" }
             + (if ($tokens | map(select(ipish | not)) | length) > 0
                then { domain: ($tokens | map(select(ipish | not))) } else {} end)
             + (if ($tokens | map(select(ipish)) | length) > 0
                then { ip: ($tokens | map(select(ipish))) } else {} end) ]
         else [] end)
      + [ { type:"field", ruleTag:"Kometa-Exit", inboundTag:["bridge-in"],
            network:"tcp,udp", outboundTag:"exit" } ]'
}

build_config_json() { # пишет JSON конфига в stdout
    local clients names rules
    clients="$(build_clients_json)" || die "Не собрал список клиентов (jq)."
    [[ -n "$clients" && "$clients" != "[]" ]] || die "Пустой список клиентов — нечего раздавать."

    names="$(printf '%s\n' $BRIDGE_SNI $BRIDGE_SNI_EXTRA \
        | tr ',' '\n' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | sed '/^$/d' \
        | jq -R -s 'split("\n") | map(select(length > 0)) | unique')" \
        || die "Не собрал serverNames."
    rules="$(build_rules_json)" || die "Не собрал правила маршрутизации."

    jq -nc \
        --argjson port "$BRIDGE_PORT" \
        --argjson clients "$clients" \
        --argjson names "$names" \
        --argjson rules "$rules" \
        --arg priv "$BRIDGE_PRIVKEY" \
        --arg sid "$BRIDGE_SHORTID" \
        --arg sni "$BRIDGE_SNI" \
        --arg ehost "$EXIT_ADDRESS" \
        --argjson eport "$EXIT_PORT" \
        --arg euuid "$EXIT_UUID" \
        --arg esni "$EXIT_SNI" \
        --arg epbk "$EXIT_PUBKEY" \
        --arg esid "$EXIT_SHORTID" \
        --arg efp "$EXIT_FP" \
        --arg eflow "$EXIT_FLOW" \
        --arg mcver "$MIN_CLIENT_VER" '
      {
        log: { loglevel: "warning", dnsLog: false, access: "none",
               error: "/var/log/xray/error.log" },
        inbounds: [ {
          tag: "bridge-in", listen: "0.0.0.0", port: $port, protocol: "vless",
          settings: { clients: $clients, decryption: "none" },
          streamSettings: {
            network: "tcp", security: "reality",
            realitySettings: (
              { show: false, xver: 0, target: ($sni + ":443"), serverNames: $names,
                privateKey: $priv, maxClientVer: "", maxTimeDiff: 0,
                shortIds: [ $sid ] }
              + (if $mcver == "" then {} else { minClientVer: $mcver } end)
            )
          },
          sniffing: { enabled: true, destOverride: ["http","tls","quic"], routeOnly: true }
        } ],
        outbounds: [
          { tag: "exit", protocol: "vless",
            settings: { vnext: [ { address: $ehost, port: $eport,
              users: [ ({ id: $euuid, encryption: "none" }
                        + (if $eflow == "" then {} else { flow: $eflow } end)) ] } ] },
            streamSettings: { network: "tcp", security: "reality",
              realitySettings: (
                { serverName: $esni, fingerprint: $efp, publicKey: $epbk, spiderX: "/" }
                + (if $esid == "" then {} else { shortId: $esid } end)) },
            mux: { enabled: false } },
          { tag: "direct", protocol: "freedom", settings: { domainStrategy: "AsIs" } },
          { tag: "block", protocol: "blackhole", settings: {} }
        ],
        routing: { domainStrategy: "AsIs", rules: $rules }
      }'
}

config_test() { # config_test <файл> → 0/1; вывод ядра в stderr при ошибке
    local bin out
    bin="$(find_xray)" || return 1
    out="$("$bin" run -test -config "$1" 2>&1)" && return 0
    out="$("$bin" -test -config "$1" 2>&1)" && return 0
    printf '%s\n' "$out" >&2
    return 1
}

# -------------------------------------------------------------- ссылки/вывод --
client_caption() { # client_caption <i>
    local i="$1" plat="${C_PLATFORMS[$i]:-}" oper="${C_OPERATORS[$i]:-}" name="${C_NAMES[$i]:-}"
    [[ -n "$name" ]] || name="клиент-$((i + 1))"
    if [[ -n "$plat" && -n "$oper" ]]; then printf '%s [%s · %s]' "$name" "$plat" "$oper"
    elif [[ -n "$plat" ]]; then printf '%s [%s]' "$name" "$plat"
    elif [[ -n "$oper" ]]; then printf '%s [%s]' "$name" "$oper"
    else printf '%s' "$name"
    fi
}

link_for() { # link_for <i> → vless://…
    local i="$1" q remark
    q="type=tcp&security=reality&pbk=$(urlenc "$BRIDGE_PUBKEY")&fp=$(urlenc "$EXIT_FP")&sni=$(urlenc "$BRIDGE_SNI")"
    [[ -n "$BRIDGE_SHORTID" ]] && q="${q}&sid=$(urlenc "$BRIDGE_SHORTID")"
    [[ -n "$EXIT_FLOW" ]] && q="${q}&flow=$(urlenc "$EXIT_FLOW")"
    q="${q}&spx=%2F"
    remark="$(printf '%s' "Kometa-${C_NAMES[$i]:-client-$((i + 1))}" | jq -sRr @uri)"
    printf 'vless://%s@%s:%s?%s#%s' "${C_UUIDS[$i]}" "$BRIDGE_ADDRESS" "$BRIDGE_PORT" "$q" "$remark"
}

print_links() {
    local i=0 n=0
    while [[ $i -lt ${#C_UUIDS[@]} ]]; do
        n=$((n + 1))
        printf ' %2d. %s\n' "$n" "$(client_caption "$i")"
        printf '     %s\n' "$(link_for "$i")"
        i=$((i + 1))
    done
}

load_clients_from_config() { # для --links-only: клиенты берутся из конфига моста
    local uuid
    while IFS= read -r uuid; do
        [[ -n "$uuid" ]] || continue
        add_client "$uuid"
    done < <(existing_inbound_json "$CONFIG" | jq -r '.settings.clients[]?.id // empty' 2>/dev/null)
}

enrich_names_from_registry() {
    local r_name r_uuid r_date r_plat r_oper r_status r_rdate i=0
    [[ -f "$REGISTRY" ]] || return 0
    while IFS=, read -r r_name r_uuid r_date r_plat r_oper r_status r_rdate; do
        [[ -n "$r_uuid" && "$r_uuid" != "uuid" ]] || continue
        i=0
        while [[ $i -lt ${#C_UUIDS[@]} ]]; do
            if [[ "${C_UUIDS[$i]}" == "$r_uuid" ]]; then
                [[ -z "${C_NAMES[$i]}" ]] && C_NAMES[$i]="$r_name"
                [[ -z "${C_PLATFORMS[$i]}" ]] && C_PLATFORMS[$i]="$r_plat"
                [[ -z "${C_OPERATORS[$i]}" ]] && C_OPERATORS[$i]="$r_oper"
            fi
            i=$((i + 1))
        done
    done <"$REGISTRY"
}

# ---------------------------------------------------------------- реестр ------
csv_sanitize() { # без запятых/кавычек/переводов строк + защита от формул Excel
    local v="$1"
    v="$(printf '%s' "$v" | tr -d '\r\n"')"
    v="${v//,/;}"
    case "$v" in
        =* | +* | @* | -*) v="'$v" ;;   # CSV-инъекция: Excel выполнит «=1+1»
    esac
    printf '%s' "$v"
}

registry_has_uuid() { # registry_has_uuid <uuid> → 0/1
    [[ -f "$REGISTRY" ]] || return 1
    awk -F, -v u="$1" 'NR > 1 && $2 == u { found = 1 } END { exit !found }' "$REGISTRY" 2>/dev/null
}

registry_update() { # registry_update <куда писать>; ставит REG_ACTIVE/REG_REVOKED/REG_ADDED
    local target="$1" tmp today r_name r_uuid r_date r_plat r_oper r_status r_rdate
    local i=0 found_name found_plat found_oper rev_date
    today="$(date +%F)"
    tmp="$(tmp_file)"
    REG_ACTIVE=0; REG_REVOKED=0; REG_ADDED=0; REG_REVOKED_NOW=""

    {
        printf 'имя,uuid,дата_выдачи,платформа,оператор,статус,дата_отзыва\n'
        if [[ -f "$REGISTRY" ]]; then
            while IFS=, read -r r_name r_uuid r_date r_plat r_oper r_status r_rdate; do
                [[ -n "$r_uuid" && "$r_uuid" != "uuid" ]] || continue
                i=0; found_name=""; found_plat=""; found_oper=""
                while [[ $i -lt ${#C_UUIDS[@]} ]]; do
                    if [[ "${C_UUIDS[$i]}" == "$r_uuid" ]]; then
                        found_name="${C_NAMES[$i]}"; found_plat="${C_PLATFORMS[$i]}"; found_oper="${C_OPERATORS[$i]}"
                        break
                    fi
                    i=$((i + 1))
                done
                if [[ $i -lt ${#C_UUIDS[@]} ]]; then
                    # UUID в новом списке → активен. Имя/платформу/оператора
                    # обновляем, только если они переданы; дата выдачи — первая.
                    [[ -n "$found_name" ]] && r_name="$found_name"
                    [[ -n "$found_plat" ]] && r_plat="$found_plat"
                    [[ -n "$found_oper" ]] && r_oper="$found_oper"
                    [[ -n "$r_date" ]] || r_date="$today"
                    printf '%s,%s,%s,%s,%s,активен,\n' \
                        "$(csv_sanitize "$r_name")" "$r_uuid" "$r_date" \
                        "$(csv_sanitize "$r_plat")" "$(csv_sanitize "$r_oper")"
                    REG_ACTIVE=$((REG_ACTIVE + 1))
                else
                    rev_date="$r_rdate"
                    [[ -n "$rev_date" ]] || rev_date="$today"
                    [[ "$r_status" == "отозван" ]] || REG_REVOKED_NOW="${REG_REVOKED_NOW} ${r_uuid}"
                    printf '%s,%s,%s,%s,%s,отозван,%s\n' \
                        "$(csv_sanitize "$r_name")" "$r_uuid" "$r_date" \
                        "$(csv_sanitize "$r_plat")" "$(csv_sanitize "$r_oper")" "$rev_date"
                    REG_REVOKED=$((REG_REVOKED + 1))
                fi
            done <"$REGISTRY"
        fi
        i=0
        while [[ $i -lt ${#C_UUIDS[@]} ]]; do
            if ! registry_has_uuid "${C_UUIDS[$i]}"; then
                printf '%s,%s,%s,%s,%s,активен,\n' \
                    "$(csv_sanitize "${C_NAMES[$i]}")" "${C_UUIDS[$i]}" "$today" \
                    "$(csv_sanitize "${C_PLATFORMS[$i]}")" "$(csv_sanitize "${C_OPERATORS[$i]}")"
                REG_ACTIVE=$((REG_ACTIVE + 1))
                REG_ADDED=$((REG_ADDED + 1))
            fi
            i=$((i + 1))
        done
    } >"$tmp" || { rm -f "$tmp"; die "Не собрал реестр."; }

    if [[ "$target" == "$REGISTRY" ]]; then
        mkdir -p "$(dirname "$REGISTRY")"
        [[ -f "$REGISTRY" ]] && cp -a "$REGISTRY" "${REGISTRY}.bak" 2>/dev/null
        install -m 0600 "$tmp" "$REGISTRY" || die "Не записал реестр ${REGISTRY}."
        rm -f "$tmp"
        ok "Реестр ${REGISTRY}: активных ${REG_ACTIVE}, отозванных ${REG_REVOKED}, новых ${REG_ADDED}."
    else
        cat "$tmp"
        rm -f "$tmp"
    fi
}

registry_warn_missing_fields() {
    local i=0 missing=0
    while [[ $i -lt ${#C_UUIDS[@]} ]]; do
        if [[ -z "${C_NAMES[$i]}" || -z "${C_PLATFORMS[$i]}" || -z "${C_OPERATORS[$i]}" ]]; then
            missing=$((missing + 1))
            warn "Клиент $((i + 1)) (${C_UUIDS[$i]}): не заполнено «имя/платформа/оператор» — допиши через --client \"${C_UUIDS[$i]},Имя,Платформа,Оператор\" и запусти снова (конфиг не изменится, перезапуска не будет)."
        fi
        i=$((i + 1))
    done
    [[ "$missing" -eq 0 ]] && ok "Реестр заполнен по всем клиентам (имя, платформа, оператор)."
    return 0
}

# ------------------------------------------------------ хост: IPv6, MSS, ufw --
apply_ipv6_off() {
    local f="/etc/sysctl.d/99-kometa-noipv6.conf" global
    printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\nnet.ipv6.conf.lo.disable_ipv6 = 1\n' >"$f" \
        || { warn "Не записал ${f}."; return 0; }
    sysctl --system >/dev/null 2>&1 || sysctl -p "$f" >/dev/null 2>&1 || warn "sysctl не применился."
    if have ip; then
        global="$(ip -6 addr show scope global 2>/dev/null | sed '/^[[:space:]]*$/d')"
        if [[ -n "$global" ]]; then
            warn "IPv6 всё ещё виден:"
            printf '%s\n' "$global" >&2
        else
            ok "IPv6 выключен: ip -6 addr show scope global пуст."
        fi
    else
        warn "Команды ip нет — проверь IPv6 вручную: ip -6 addr show scope global."
    fi
}

mss_rule() { printf '%s -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss %s' "$1" "$2"; }

apply_mss_clamp() {
    local mss4 mss6 bin mss rule removed spec
    mss4=$((LTE_MTU - 40))
    mss6=$((LTE_MTU - 60))
    for spec in "iptables ${mss4}" "ip6tables ${mss6}"; do
        read -r bin mss <<<"$spec"
        have "$bin" || continue
        rule="$(mss_rule "$bin" "$mss")"
        removed=0
        while $rule -D >/dev/null 2>&1; do removed=$((removed + 1)); done
        if $rule -I >/dev/null 2>&1; then
            ok "MSS-clamp ${bin}: MSS ${mss} (MTU ${LTE_MTU}) для транзитного TCP."
        else
            warn "MSS-clamp ${bin}: не применился. Вручную: ${rule} -I"
        fi
    done
}

apply_firewall() {
    local ssh_port="$1"
        # Именно «Status: active»: строка «Status: inactive» тоже содержит
        # подстроку «active», и по ней легко решить, что фаервол включён.
        if ufw status 2>/dev/null | head -1 | grep -q '^Status: active'; then
            if ufw allow "${BRIDGE_PORT}/tcp" >/dev/null 2>&1; then
                ok "ufw: разрешён порт моста ${BRIDGE_PORT}/tcp (SSH ${ssh_port} не трогаю)."
            else
                warn "ufw: не удалось добавить правило для ${BRIDGE_PORT}/tcp."
            fi
        else
            log "ufw установлен, но выключен — правила не меняю (порт ${BRIDGE_PORT} и так открыт, если нет других фильтров)."
        fi
    else
        log "ufw не найден — фаервол не трогаю."
    fi

    if [[ "$HARDEN_FIREWALL" -eq 1 ]]; then
        have ufw || { log "Ставлю ufw…"; (apt-get update -qq && apt-get install -y -qq ufw) >/dev/null 2>&1 || warn "ufw не установился."; }
        if have ufw; then
            ufw allow "${ssh_port}/tcp" >/dev/null 2>&1
            ufw allow "${BRIDGE_PORT}/tcp" >/dev/null 2>&1
            ufw --force enable >/dev/null 2>&1 && ok "ufw включён: только SSH ${ssh_port} и мост ${BRIDGE_PORT}." || warn "ufw не включился."
        fi
    fi
}

# ------------------------------------------------------------ применение -----
write_config_and_restart() { # write_config_and_restart <tmp-config>
    local tmp="$1" old_hash new_hash active backup
    mkdir -p "$(dirname "$CONFIG")" /var/log/xray 2>/dev/null

    log "Проверяю конфиг ядром (xray run -test)…"
    if ! config_test "$tmp"; then
        die "Конфиг не проходит проверку ядром (вывод выше). Ничего не менял: старый конфиг на месте, служба не тронута."
    fi
    ok "Конфиг валиден."

    old_hash=""
    [[ -f "$CONFIG" ]] && old_hash="$(sha256_file "$CONFIG")"
    new_hash="$(sha256_file "$tmp")"

    if [[ -n "$old_hash" && "$old_hash" == "$new_hash" ]]; then
        log "Конфиг не изменился — перезапуск не нужен (ссылки и ключи те же)."
    else        if [[ -f "$CONFIG" ]]; then
            backup="${CONFIG}.bak-$(date +%Y%m%d%H%M%S)"
            cp -a "$CONFIG" "$backup" && log "Бэкап конфига: ${backup}"
            # держим последние 5 бэкапов, чтобы /usr/local/etc/xray не пух
            ls -1t "${CONFIG}".bak-* 2>/dev/null | tail -n +6 | while read -r f; do rm -f "$f"; done
        fi
        install -m 0600 "$tmp" "$CONFIG" || die "Не записал конфиг ${CONFIG}."
        ok "Конфиг записан: ${CONFIG} (0600)"
        log "Перезапускаю xray…"
        systemctl restart xray 2>/dev/null || die "systemctl restart xray завершился ошибкой."
    fi

    active="$(systemctl is-active xray 2>/dev/null || true)"
    if [[ "$active" != "active" && -n "$old_hash" && "$old_hash" == "$new_hash" ]]; then
        # Конфиг тот же, а служба лежит (упала/остановлена) — просто поднять.
        log "Служба не активна, конфиг не менялся — запускаю xray…"
        systemctl start xray 2>/dev/null || true
        active="$(systemctl is-active xray 2>/dev/null || true)"
    fi
    if [[ "$active" != "active" ]]; then
        err "xray не активен (is-active: ${active:-нет ответа})."
        err "--- systemctl status xray ---"
        systemctl status xray --no-pager -l 2>&1 | head -25 >&2 || true
        err "--- journalctl -u xray -n 30 ---"
        journalctl -u xray -n 30 --no-pager 2>&1 >&2 || true
        if [[ -n "$old_hash" && -f "${CONFIG}.bak-"* ]]; then
            warn "Откат: последний бэкап конфига лежит рядом (${CONFIG}.bak-*)."
        fi
        exit 1
    fi
    ok "Служба xray: active."
}

print_exit_hint() {
    note ""
    note "${C_BOLD}Выходная нода: закрыть её для посторонних (приёмка, шаг 8)${C_OFF}"
    note "  # разрешить только мосту, остальным — отказать:"
    note "  ufw allow from ${BRIDGE_ADDRESS} to any port ${EXIT_PORT} proto tcp"
    note "  ufw deny ${EXIT_PORT}/tcp"
    note "  # проверка с ЛЮБОГО другого хоста: nc -zv ${EXIT_ADDRESS} ${EXIT_PORT} → не подключается"
}

print_summary() { # print_summary <режим> <версия-строка>
    local mode="$1" ver="$2" i=0
    box "ИТОГ · мост (${mode})"
    note "Версия ядра: ${ver}"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        note "Служба xray: не проверялась (--dry-run)"
    else
        note "Служба xray: $(systemctl is-active xray 2>/dev/null || printf 'нет ответа')"
    fi
    note "Мост: ${BRIDGE_ADDRESS}:${BRIDGE_PORT} (VLESS + Reality, SNI ${BRIDGE_SNI}, fp ${EXIT_FP})"
    note "Выход: ${EXIT_ADDRESS}:${EXIT_PORT} (SNI ${EXIT_SNI})"
    note "Пользователей: ${#C_UUIDS[@]}"
    if [[ "$DIRECT_RU" -eq 1 ]]; then
        note "РФ-трафик: напрямую с моста (--direct-ru)"
    else
        note "РФ-трафик: тоже на выход (на мосте порт 53 молчит — так проходит проверка утечек)"
    fi
    note ""
    note "${C_BOLD}Ссылки — каждому своя, не публиковать:${C_OFF}"
    print_links
    note ""
    if [[ "$NO_REGISTRY" -eq 1 ]]; then
        note "Реестр: отключён (--no-registry)"
    elif [[ "$DRY_RUN" -eq 1 ]]; then
        note "Реестр ${REGISTRY}: будет записан так (--dry-run ничего не пишет):"
        registry_update "$(tmp_file)"
    else
        note "Реестр: ${REGISTRY} (активных ${REG_ACTIVE}, отозванных ${REG_REVOKED})"
    fi
    if [[ -n "$(trim "$REG_REVOKED_NOW")" ]]; then
        note ""
        note "${C_YELLOW}Отозваны в этом запуске (ссылки больше не работают):${C_OFF}"
        for u in $REG_REVOKED_NOW; do note "  - ${u}"; done
    fi
}

print_next_steps() {
    note ""
    note "${C_BOLD}Дальше — шаги 7–8 (подробно: ops/README.md):${C_OFF}"
    note "  1. Раздать ссылки: Android — Happ (или INCY ≥ 2.7.11), iOS — Streisand/Happ,"
    note "     macOS — v2rayN/Throne с включённым TUN. У каждого своя ссылка."
    note "  2. На телефоне: https://www.cloudflare.com/cdn-cgi/trace → в строке ip= должен быть ${EXIT_ADDRESS}."
    note "     (api.ipify.org и ifconfig.me намеренно не годятся для проверки.)"
    note "  3. Утечки: на мосте tcpdump -ni any port 53 — при серфинге должно быть тихо;"
    note "     ip -6 addr show scope global — пусто."
    note "  4. Скорость ≥ 5 Мбит/с и ≥ 10 минут без обрывов — на каждом устройстве."
    note "  5. Отзыв ключа: убрать UUID из --client-uuid и запустить скрипт снова."
    note ""
    note "${C_YELLOW}Не публиковать адрес и ссылки. Не выдавать одну ссылку на всех (скрипт это запрещает).${C_OFF}"
}

# -------------------------------------------------------------------- main ----
main() {
    local tmp_config ver
    parse_args ${1+"$@"}

    box "Kometa · День 0 · мост RU → зарубежный выход"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "Режим: ПРЕДПРОСМОТР (--dry-run): ничего не пишу, в сеть не хожу, systemctl не зову."
    elif [[ "$LINKS_ONLY" -eq 1 ]]; then
        log "Режим: только ссылки по существующему конфигу (--links-only)."
    else
        log "Режим: ПРИМЕНЕНИЕ (изменения на хосте). Предпросмотр — с --dry-run."
    fi

    require_root_for_apply
    need_jq

    if [[ "$LINKS_ONLY" -eq 1 ]]; then
        [[ -f "$CONFIG" ]] || die "--links-only: нет конфига ${CONFIG}."
        # Адрес: явный флаг → файл-спутник (там адрес на момент развёртывания)
        # → определение по маршруту. Без спутника перепечатка ссылок на другой
        # машине подставила бы чужой адрес.
        [[ -z "$BRIDGE_ADDRESS" ]] && BRIDGE_ADDRESS="$(meta_get address || true)"
        [[ -z "$BRIDGE_ADDRESS" ]] && BRIDGE_ADDRESS="$(detect_bridge_address)"
        [[ -n "$BRIDGE_ADDRESS" ]] || die "--links-only: не определил адрес моста, передай --bridge-address."
        [[ -n "$BRIDGE_PUBKEY" ]] || BRIDGE_PUBKEY="$(meta_get publicKey || true)"
        [[ -n "$BRIDGE_PUBKEY" ]] || die "--links-only: нет publicKey (файл-спутник $(meta_file)); передай --bridge-pubkey."
        [[ -n "$BRIDGE_SNI" ]] || BRIDGE_SNI="$(inbound_field "$CONFIG" '.streamSettings.realitySettings.serverNames[0]' || true)"
        [[ -n "$BRIDGE_SHORTID" ]] || BRIDGE_SHORTID="$(inbound_field "$CONFIG" '.streamSettings.realitySettings.shortIds[0]' || true)"
        BRIDGE_PORT="$(inbound_field "$CONFIG" '.port' || true)"
        [[ -n "$BRIDGE_PORT" ]] || BRIDGE_PORT=443
        load_clients_from_config
        [[ ${#C_UUIDS[@]} -ge 1 ]] || die "--links-only: в конфиге нет клиентов."
        enrich_names_from_registry
        box "Ссылки по конфигу ${CONFIG}"
        note "Мост: ${BRIDGE_ADDRESS}:${BRIDGE_PORT} (SNI ${BRIDGE_SNI})"
        print_links
        return 0
    fi

    validate_exit_params
    validate_bridge_params
    validate_clients
    # Имя/платформа/оператор из прежнего реестра подтягиваются в подписи ссылок:
    # при повторном запуске с одними --client-uuid видно имена, а не «клиент-1».
    # Явные данные из --client при этом не перетираются.
    [[ "$NO_REGISTRY" -eq 0 ]] && enrich_names_from_registry

    if [[ "$DIRECT_RU" -eq 1 ]]; then
        warn "--direct-ru: РФ-домены пойдут напрямую с моста, и мост будет резолвить их сам (на мосте появится трафик 53). Это осознанный выбор для боевой нагрузки; для Дня 0 проверка утечек чище без него."
    fi
    if [[ -n "$DIRECT_FILE" && ! -r "$DIRECT_FILE" ]]; then
        die "--direct-file: файл не читается: ${DIRECT_FILE}"
    fi

    # Адрес моста в ссылки.
    if [[ -z "$BRIDGE_ADDRESS" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            BRIDGE_ADDRESS="<BRIDGE_IP>"
            warn "Адрес моста не задан и в dry-run не угадывается: в ссылках будет <BRIDGE_IP>. В бою передай --bridge-address или запусти на мосте."
        else
            BRIDGE_ADDRESS="$(detect_bridge_address)"
            [[ -n "$BRIDGE_ADDRESS" ]] || die "Не определил адрес моста. Передай --bridge-address <IP>."
            ok "Адрес моста определён: ${BRIDGE_ADDRESS}"
        fi
    fi
    [[ "$BRIDGE_ADDRESS" == "<BRIDGE_IP>" ]] || is_ipv4 "$BRIDGE_ADDRESS" || warn "Адрес моста «${BRIDGE_ADDRESS}» не IPv4 — ссылки будут с доменом."

    # Ядро.
    if [[ "$DRY_RUN" -eq 1 ]]; then
        if ver="$(find_xray)" && ver="$(xray_version_of "$ver")" && [[ -n "$ver" ]]; then
            ok "Ядро на этой машине: ${ver}"
        else
            ver="$XRAY_VERSION"
            log "Ядра на этой машине нет — в предпросмотре беру закреплённую версию ${XRAY_VERSION}."
        fi
    else
        log "Ядро: нужно ${XRAY_VERSION}…"
        ensure_xray
        ver="$XRAY_VERSION_ACTUAL"
    fi

    # Ключи Reality моста.
    resolve_keys
    [[ -n "$BRIDGE_SNI" ]] || BRIDGE_SNI="$EXIT_SNI"

    # Geo-файлы нужны только для geo-правил.
    if [[ "$DIRECT_RU" -eq 1 && -z "$DIRECT_FILE" ]]; then
        if [[ "$DRY_RUN" -eq 0 && ! -f /usr/local/share/xray/geosite.dat ]]; then
            die "--direct-ru: нет /usr/local/share/xray/geosite.dat. Поставь geoip.dat/geosite.dat или задай список доменами через --direct-file."
        fi
    fi

    # Конфиг.
    tmp_config="$(tmp_file)"
    build_config_json >"$tmp_config" || die "Не собрал конфиг (jq)."
    if [[ ! -s "$tmp_config" ]]; then die "Конфиг пуст — сборка не удалась."; fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        box "Конфиг (будет записан в ${CONFIG} при запуске без --dry-run)"
        cat "$tmp_config"
        note ""
        note "Проверка ядром: xray run -test -config <временный файл> — выполняется на мосте перед перезапуском."
        if [[ "$NO_REGISTRY" -eq 0 ]]; then
            note "Реестр: ${REGISTRY} (обновляется при применении; сейчас показан предпросмотр выше)."
        fi
        print_summary "ПРЕДПРОСМОТР, ничего не изменено" "${ver}"
        print_exit_hint
        print_next_steps
        return 0
    fi

    # Применение.
    if [[ "$KEEP_IPV6" -eq 0 ]]; then apply_ipv6_off; else warn "IPv6 не выключаю (--keep-ipv6): в приёмке пункт «ip -6 addr show scope global пусто» не выполнится."; fi
    [[ "$DO_MSS" -eq 1 ]] && apply_mss_clamp
    apply_firewall "$(detect_ssh_port)"
    write_meta
    write_config_and_restart "$tmp_config"

    if [[ "$NO_REGISTRY" -eq 0 ]]; then
        registry_update "$REGISTRY"
        registry_warn_missing_fields
    fi

    print_summary "мост поднят" "${ver}"
    if [[ -n "$(trim "$REG_REVOKED_NOW")" && "$NO_REGISTRY" -eq 0 ]]; then
        ok "Отозванные UUID удалены из инбаунда — их ссылки больше не работают, остальные не тронуты."
    fi
    print_exit_hint
    print_next_steps
    return 0
}

main ${1+"$@"}
