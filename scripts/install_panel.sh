#!/usr/bin/env bash
# =============================================================================
#  Kometa — установка панели 3x-ui на ЧИСТЫЙ сервер (Ubuntu 24.04 / Debian 12+)
# =============================================================================
#  Что делает скрипт:
#    1) проверяет root, ОС, архитектуру и доступ в интернет;
#    2) обновляет пакеты и ставит зависимости: curl, certbot, ufw, jq, sqlite3;
#    3) ставит 3x-ui ОФИЦИАЛЬНЫМ установщиком из репозитория MHSanaei/3x-ui;
#    4) настраивает firewall (ufw): SSH, порт панели, 80/443/tcp, UDP AmneziaWG;
#    5) печатает: как включить TLS, как получить API-токен панели и что вписать
#       в .env проекта Kometa.
#
#  Идемпотентность: повторный запуск НЕ переустанавливает панель (только
#  проверяет зависимости и правила firewall). Принудительно — флаг --reinstall.
#
#  Запуск:   sudo bash scripts/install_panel.sh
#  Примеры:  sudo bash scripts/install_panel.sh --port 54321 --awg-port 51820
#            sudo bash scripts/install_panel.sh --no-firewall --listen-ip 127.0.0.1
#
#  ВАЖНО: скрипт не хранит секретов. Логин/пароль панели и API-токен печатаются
#  в консоль и (силами официального установщика) сохраняются в /etc/x-ui/install-result.env
#  с правами 600 — этот файл читает только root.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------- константы ---
XUI_INSTALL_URL="${XUI_INSTALL_URL:-https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh}"
XUI_DIR="/usr/local/x-ui"
XUI_BIN="${XUI_DIR}/x-ui"
XUI_INSTALL_RESULT="/etc/x-ui/install-result.env"

PANEL_PORT="${PANEL_PORT:-54321}"          # порт веб-панели
PANEL_USER="${PANEL_USER:-kometa_admin}"   # логин в панель
PANEL_PASSWORD="${PANEL_PASSWORD:-}"       # пусто = сгенерировать случайный
PANEL_BASE_PATH="${PANEL_BASE_PATH:-}"     # пусто = сгенерировать случайный
PANEL_SUB_PORT="${PANEL_SUB_PORT:-2096}"   # порт сервиса подписок самой панели
AWG_PORT="${AWG_PORT:-51820}"              # UDP-порт под AmneziaWG
WEB_PORT="${WEB_PORT:-8080}"              # порт веб-слоя бота (ссылка /sub/<token>)
LISTEN_IP="${LISTEN_IP:-}"                 # '' = панель слушает все адреса
DO_FIREWALL=1
FORCE_REINSTALL=0

# ------------------------------------------------------------------- вывод ----
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_OFF=""
fi

log()   { printf '%s[•]%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()    { printf '%s[✓]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
err()   { printf '%s[✗]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
die()   { err "$*"; exit 1; }

box() {
    printf '\n%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════"
    printf '%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"
    printf '%s%s%s\n' "$C_BOLD" "══════════════════════════════════════════════════════════════" "$C_OFF"
}

usage() {
    cat <<'EOF'
Установка панели 3x-ui для проекта Kometa.

Использование:
  sudo bash scripts/install_panel.sh [флаги]

Флаги:
  --port N            порт веб-панели (по умолчанию 54321)
  --username NAME     логин в панель (по умолчанию kometa_admin)
  --password PASS     пароль (по умолчанию — случайный, будет напечатан)
  --web-base-path P   секретный путь панели (по умолчанию — случайный)
  --awg-port N        UDP-порт AmneziaWG, который открыть в firewall (51820)
  --sub-port N        порт сервиса подписок панели (2096; 0 = не открывать)
  --web-port N        порт веб-слоя бота для /sub/<token> (8080; 0 = не открывать)
  --listen-ip IP      на каком адресе слушать панель (например 127.0.0.1)
  --no-firewall       не трогать ufw
  --reinstall         переустановить панель поверх текущей (обновление)
  -h, --help          эта справка

Переменные окружения PANEL_PORT / PANEL_USER / PANEL_PASSWORD / AWG_PORT и т.д.
действуют так же, как флаги.
EOF
}

# --------------------------------------------------------------- утилиты ------
have() { command -v "$1" >/dev/null 2>&1; }

rand_alnum() { # rand_alnum <длина> — случайная строка (hex, криптостойкая)
    local len="${1:-20}"
    # "|| true" — страховка от SIGPIPE/pipefail: пустой результат не должен ронять скрипт.
    openssl rand -hex "$((len / 2 + 2))" 2>/dev/null | cut -c1-"$len" || true
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

require_root() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Запусти от root:  sudo bash scripts/install_panel.sh"
}

check_os() {
    [[ -r /etc/os-release ]] || die "Не найден /etc/os-release — не понимаю, что это за система."
    # shellcheck disable=SC1091
    . /etc/os-release
    local id
    id="$(lower "${ID:-}")"
    case "$id" in
        ubuntu | debian | linuxmint | pop) ok "ОС: ${PRETTY_NAME:-$id}" ;;
        *) die "Поддерживаются Ubuntu 24.04 и Debian. Обнаружено: ${PRETTY_NAME:-$id}" ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64 | aarch64 | arm64) ok "Архитектура: $(uname -m)" ;;
        *) die "Архитектура $(uname -m) официальным установщиком 3x-ui не поддерживается." ;;
    esac
    have apt-get || die "Не найден apt-get — это не Debian-подобная система."
}

check_internet() {
    log "Проверяю доступ в интернет..."
    if have curl; then
        curl -fsS --max-time 15 -o /dev/null "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh" \
            || die "Нет доступа к raw.githubusercontent.com. Проверь сеть/DNS на сервере."
    fi
    ok "Интернет есть."
}

apt_install() { # apt_install <пакет...>
    local missing=()
    local p
    for p in "$@"; do
        dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        ok "Пакеты уже стоят: $*"
        return 0
    fi
    log "Устанавливаю пакеты: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        "${missing[@]}"
}

update_packages() {
    log "Обновляю список пакетов (apt-get update)..."
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq
    log "Обновляю установленные пакеты (apt-get upgrade)... это может занять пару минут."
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
        apt-get upgrade -y -qq \
        -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"
    ok "Система обновлена."
}

install_dependencies() {
    # curl/openssl — установка и генерация ключей; ufw — firewall; certbot — TLS;
    # jq — разбор ответов API панели в наших скриптах; sqlite3 — проверка и бэкап БД.
    apt_install ca-certificates curl openssl ufw certbot jq sqlite3 cron
    update-ca-certificates >/dev/null 2>&1 || true
}

# ------------------------------------------------------------ установка -------
panel_installed() { [[ -x "$XUI_BIN" ]]; }
panel_running()   { systemctl is-active --quiet x-ui 2>/dev/null; }

install_panel() {
    if panel_installed && [[ "$FORCE_REINSTALL" -eq 0 ]]; then
        ok "Панель 3x-ui уже установлена ($("$XUI_BIN" -v 2>/dev/null || echo 'версия неизвестна'))."
        log "Переустановка пропущена (идемпотентность). Нужно обновить — запусти с флагом --reinstall."
        return 0
    fi

    if panel_installed; then
        warn "Панель уже есть — запускаю установщик повторно (обновление/переустановка)."
        warn "База и настройки в /etc/x-ui сохраняются."
    fi

    [[ -n "$PANEL_PASSWORD" ]] || PANEL_PASSWORD="$(rand_alnum 24)"
    [[ -n "$PANEL_BASE_PATH" ]] || PANEL_BASE_PATH="$(rand_alnum 18)"
    [[ ${#PANEL_BASE_PATH} -ge 18 ]] || die "PANEL_BASE_PATH должен быть не короче 18 символов."

    log "Ставлю 3x-ui официальным установщиком (неинтерактивный режим)..."
    log "Источник: ${XUI_INSTALL_URL}"
    if ! XUI_NONINTERACTIVE=1 \
        XUI_USERNAME="$PANEL_USER" \
        XUI_PASSWORD="$PANEL_PASSWORD" \
        XUI_PANEL_PORT="$PANEL_PORT" \
        XUI_WEB_BASE_PATH="$PANEL_BASE_PATH" \
        XUI_SSL_MODE=none \
        bash <(curl -Ls "$XUI_INSTALL_URL"); then
        die "Официальный установщик 3x-ui завершился с ошибкой. Смотри вывод выше."
    fi

    panel_installed || die "После установки не найден ${XUI_BIN}. Что-то пошло не так."
    systemctl enable x-ui >/dev/null 2>&1 || true
    systemctl restart x-ui
    ok "Панель 3x-ui установлена и запущена."
}

apply_extra_settings() {
    # listenIP применяем только если явно попросили (иначе можно потерять доступ).
    if [[ -n "$LISTEN_IP" ]]; then
        log "Панель будет слушать только ${LISTEN_IP} (доступ через SSH-туннель)."
        "$XUI_BIN" setting -listenIP "$LISTEN_IP" >/dev/null
    fi

    if ! systemctl is-active --quiet x-ui; then
        warn "Служба x-ui не активна — пробую запустить ещё раз."
        systemctl restart x-ui || true
    fi
    sleep 2
    if panel_running; then
        ok "Служба x-ui: active (порт ${PANEL_PORT})."
    else
        warn "Служба x-ui не запустилась. Смотри: systemctl status x-ui; journalctl -u x-ui -n 50"
    fi
}

# ------------------------------------------------------------- firewall -------
firewall_allow() { # firewall_allow <порт> <протокол> <комментарий>
    local port="$1" proto="$2" comment="$3"
    ufw allow "${port}/${proto}" comment "$comment" >/dev/null 2>&1 \
        || warn "Не удалось добавить правило ufw: ${port}/${proto}"
}

ssh_ports() {
    local ports="22"
    if have sshd; then
        local detected
        detected="$(sshd -T 2>/dev/null | awk '/^port /{print $2}' | sort -u | tr '\n' ' ' || true)"
        [[ -n "${detected// /}" ]] && ports="$detected"
    fi
    # Порт текущего SSH-подключения — страховка от самоблокировки.
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        local cur
        cur="$(printf '%s' "$SSH_CONNECTION" | awk '{print $4}')"
        case " $ports " in *" $cur "*) ;; *) ports="$ports $cur" ;; esac
    fi
    printf '%s' "$ports"
}

setup_firewall() {
    if [[ "$DO_FIREWALL" -eq 0 ]]; then
        warn "Настройка firewall пропущена (--no-firewall). Убедись сам, что нужные порты открыты."
        return 0
    fi
    have ufw || { warn "ufw не установлен — пропускаю настройку firewall."; return 0; }

    log "Настраиваю ufw..."
    ufw default deny incoming  >/dev/null 2>&1 || true
    ufw default allow outgoing >/dev/null 2>&1 || true

    local p
    for p in $(ssh_ports); do
        firewall_allow "$p" tcp "SSH"
        ok "Открыт SSH: ${p}/tcp"
    done

    firewall_allow "$PANEL_PORT" tcp "3x-ui panel"
    ok "Открыт порт панели: ${PANEL_PORT}/tcp"

    firewall_allow 80 tcp "HTTP (certbot / ACME)"
    firewall_allow 443 tcp "HTTPS + VLESS Reality"
    ok "Открыты 80/tcp и 443/tcp"

    firewall_allow "$AWG_PORT" udp "AmneziaWG"
    ok "Открыт UDP под AmneziaWG: ${AWG_PORT}/udp"

    if [[ "$WEB_PORT" != "0" ]]; then
        firewall_allow "$WEB_PORT" tcp "Kometa subscription web"
        ok "Открыт порт ссылки-подписки бота: ${WEB_PORT}/tcp"
    fi
    if [[ "$PANEL_SUB_PORT" != "0" && "$PANEL_SUB_PORT" != "$PANEL_PORT" ]]; then
        firewall_allow "$PANEL_SUB_PORT" tcp "3x-ui subscription service"
        ok "Открыт порт подписок панели: ${PANEL_SUB_PORT}/tcp"
    fi

    ufw --force enable >/dev/null 2>&1 || warn "ufw enable вернул ошибку — проверь: ufw status"
    ufw --force reload >/dev/null 2>&1 || true
    ok "Firewall включён. Текущие правила:"
    ufw status numbered | sed 's/^/    /'
}

# ---------------------------------------------------------------- итоги -------
detect_public_ip() {
    local ip=""
    ip="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    [[ -n "$ip" ]] || ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    printf '%s' "${ip:-IP_СЕРВЕРА}"
}

print_next_steps() {
    local ip; ip="$(detect_public_ip)"
    local scheme="http"
    local url="${scheme}://${ip}:${PANEL_PORT}/${PANEL_BASE_PATH}"

    box "ПАНЕЛЬ 3x-ui ГОТОВА"
    cat <<EOF
  Адрес панели : ${url}
  Логин        : ${PANEL_USER}
  Пароль       : ${PANEL_PASSWORD}
  Порт         : ${PANEL_PORT}
  Web base path: ${PANEL_BASE_PATH}

  Эти же данные официальный установщик сохранил в ${XUI_INSTALL_RESULT} (права 600, только root).
  Если пароль выше пустой — значит панель уже была установлена ранее, и пароль остался прежним.
EOF

    box "1. ВКЛЮЧИТЬ TLS НА ПАНЕЛИ (рекомендуется)"
    cat <<EOF
  Вариант А (проще): открой панель в браузере → Settings → SSL Certificate →
  выбери "Let's Encrypt (domain)" и укажи домен панели. Нужен домен, указывающий на ${ip}.

  Вариант Б (certbot из консоли, домен уже указывает на этот сервер):
    certbot certonly --standalone -d panel.example.com --agree-tos -m you@example.com --non-interactive
    ${XUI_BIN} setting -webCert /etc/letsencrypt/live/panel.example.com/fullchain.pem \\
                             -webCertKey /etc/letsencrypt/live/panel.example.com/privkey.pem
    systemctl restart x-ui
  После этого адрес панели станет https://panel.example.com:${PANEL_PORT}/${PANEL_BASE_PATH}
EOF

    box "2. ПОЛУЧИТЬ API-ТОКЕН ДЛЯ БОТА"
    cat <<EOF
  ${XUI_BIN} setting -getApiToken -tokenName kometa-bot -tokenScope admin

  Токен печатается ОДИН РАЗ (в базе хранится только его хеш) — сразу скопируй его в .env.
  Повторный вызов с тем же -tokenName ВЫПУСКАЕТ НОВЫЙ токен и старый перестаёт работать
  (это же и есть ротация токена при утечке).

  Альтернатива через браузер: Settings → API Tokens → Create (Scope: admin).

  Скоупы: admin — полный доступ (нужен боту), node-sync — только синхронизация с мастер-панелью,
  monitor — только чтение статуса.
EOF

    box "3. ЧТО ВПИСАТЬ В .env ПРОЕКТА"
    cat <<EOF
  PANEL_TYPE=xui
  PANEL_URL=${scheme}://${ip}:${PANEL_PORT}/${PANEL_BASE_PATH}
  PANEL_TOKEN=<токен из шага 2>
  PANEL_INBOUND_IDS=<ID инбаундов через запятую: создать их — scripts/install_node.sh>
  PANEL_SUB_BASE=${scheme}://${ip}:${PANEL_SUB_PORT}/sub/
  PUBLIC_BASE_URL=http://${ip}:${WEB_PORT}

  Если включил TLS — замени ${scheme}:// на https:// и подставь домен вместо IP.
EOF

    box "4. БЕЗОПАСНОСТЬ"
    cat <<EOF
  • Панель нужна только тебе. Самый надёжный вариант — закрыть её порт в интернет:
      ${XUI_BIN} setting -listenIP 127.0.0.1
      ufw delete allow ${PANEL_PORT}/tcp
    и ходить в панель через SSH-туннель:
      ssh -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} root@${ip}
      затем открыть http://127.0.0.1:${PANEL_PORT}/${PANEL_BASE_PATH}
  • Вход по SSH — только по ключу: PasswordAuthentication no в /etc/ssh/sshd_config.
  • Пароль панели — длинный и уникальный (сгенерированный выше), нигде не публикуй.
  • Fail2ban панели включается установщиком 3x-ui автоматически.
  • Резервные копии: scripts/backup.sh (ставит в cron или запускай вручную).
EOF

    printf '\n%sДальше:%s создай инбаунды — sudo bash scripts/install_node.sh\n\n' "$C_BOLD" "$C_OFF"
}

# ------------------------------------------------------------------- main -----
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port)          PANEL_PORT="${2:?--port требует значение}"; shift 2 ;;
            --username)      PANEL_USER="${2:?--username требует значение}"; shift 2 ;;
            --password)      PANEL_PASSWORD="${2:?--password требует значение}"; shift 2 ;;
            --web-base-path) PANEL_BASE_PATH="${2:?--web-base-path требует значение}"; shift 2 ;;
            --awg-port)      AWG_PORT="${2:?--awg-port требует значение}"; shift 2 ;;
            --sub-port)      PANEL_SUB_PORT="${2:?--sub-port требует значение}"; shift 2 ;;
            --web-port)      WEB_PORT="${2:?--web-port требует значение}"; shift 2 ;;
            --listen-ip)     LISTEN_IP="${2:?--listen-ip требует значение}"; shift 2 ;;
            --no-firewall)   DO_FIREWALL=0; shift ;;
            --reinstall)     FORCE_REINSTALL=1; shift ;;
            -h | --help)     usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    box "KOMETA • УСТАНОВКА ПАНЕЛИ 3x-ui"
    require_root
    check_os
    check_internet
    update_packages
    install_dependencies
    install_panel
    apply_extra_settings
    setup_firewall
    print_next_steps
}

main "$@"
