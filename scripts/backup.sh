#!/usr/bin/env bash
# =============================================================================
#  Kometa — резервное копирование
# =============================================================================
#  Что попадает в архив:
#    1) база бота: data/kometa.db (SQLite) или дамп PostgreSQL, если задан DB_URL;
#       плюс текстовый SQL-дамп — его можно прочитать глазами и восстановить
#       даже без sqlite3;
#    2) база панели 3x-ui: /etc/x-ui/x-ui.db (или ./panel-data/x-ui.db для
#       Docker-профиля panel) и её конфиги;
#    3) MANIFEST.txt — что внутри, размеры и результат проверки целостности.
#
#  Проверка целостности: sqlite3 '<файл>' 'PRAGMA integrity_check' → должно быть "ok".
#  Хранение: 14 дней (--keep-days N), старые архивы удаляются.
#
#  Запуск:   bash scripts/backup.sh            # бэкап в ./backups
#            bash scripts/backup.sh --keep-days 30 --out /mnt/backups
#
#  ВНИМАНИЕ: архив содержит секреты (API-токен панели, токен бота).
#  Скрипт ставит на каталог backups права 700, а на архив — 600.
#
#  Автоматизация (cron, каждый день в 04:30):
#    30 4 * * * cd /root/kometa && bash scripts/backup.sh >> /var/log/kometa-backup.log 2>&1
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------- настройки ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
DATA_DIR="${DATA_DIR:-${ROOT_DIR}/data}"
BACKUP_DIR="${BACKUP_DIR:-${ROOT_DIR}/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
WITH_PANEL=1
USE_RSYNC=1
WORK_DIR=""   # глобальная: нужна обработчику EXIT для уборки временного каталога

XUI_DB_NATIVE="/etc/x-ui/x-ui.db"
XUI_DB_DOCKER="${ROOT_DIR}/panel-data/x-ui.db"
XUI_INSTALL_RESULT="/etc/x-ui/install-result.env"

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

usage() {
    cat <<'EOF'
Резервное копирование Kometa (бот + панель 3x-ui).

Использование:
  bash scripts/backup.sh [флаги]

Флаги:
  --out DIR        куда класть архивы (по умолчанию ./backups)
  --keep-days N    сколько дней хранить архивы (по умолчанию 14)
  --no-panel       не трогать базу панели 3x-ui
  --no-rsync       не выгружать архив во внешнее место, даже если задан
                   BACKUP_RSYNC_TARGET в .env
  -h, --help       эта справка
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

env_get() { # значение переменной из .env без выполнения файла
    local key="$1" line
    [[ -f "$ENV_FILE" ]] || return 1
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

human_size() { # человекочитаемый размер файла
    local f="$1"
    if [[ ! -e "$f" ]]; then printf 'нет'; return; fi
    du -h "$f" 2>/dev/null | awk '{print $1}' || printf '?'
}

sha256_of() {
    local f="$1"
    if have sha256sum; then sha256sum "$f" | awk '{print $1}'
    elif have shasum; then shasum -a 256 "$f" | awk '{print $1}'
    else printf 'n/a'; fi
}

# ---------------------------------------------------------------- проверки ----
check_sqlite() { # check_sqlite <файл> → 0 если PRAGMA integrity_check = ok
    local f="$1" res
    have sqlite3 || { warn "sqlite3 не установлен — проверка целостности пропущена (apt-get install -y sqlite3)"; return 0; }
    res="$(sqlite3 "$f" 'PRAGMA integrity_check;' 2>&1 || true)"
    if [[ "$res" == "ok" ]]; then
        ok "Целостность OK: $(basename "$f")"
        return 0
    fi
    err "Проверка целостности НЕ пройдена: ${f} → ${res}"
    return 1
}

copy_sqlite() { # copy_sqlite <источник> <назначение> — согласованная копия живой БД
    local src="$1" dst="$2"
    if have sqlite3; then
        # .backup делает согласованный снимок даже при активных пишущих процессах.
        sqlite3 "$src" ".backup '${dst}'" 2>/dev/null \
            || sqlite3 "$src" "VACUUM INTO '${dst}'" 2>/dev/null \
            || cp -f "$src" "$dst"
    else
        cp -f "$src" "$dst"
    fi
    [[ -s "$dst" ]]
}

# --------------------------------------------------------------- бэкап БД ----
backup_bot_db() { # backup_bot_db <каталог назначения>
    local dest="$1" db_url src
    db_url="$(env_get DB_URL || true)"

    if [[ "$db_url" == postgres* || "$db_url" == postgresql* ]]; then
        log "DB_URL указывает на PostgreSQL — делаю pg_dump..."
        have pg_dump || die "Нужен pg_dump (apt-get install -y postgresql-client)."
        pg_dump --no-owner --no-privileges "$db_url" > "${dest}/kometa-db.sql" \
            || die "pg_dump завершился с ошибкой."
        gzip -f "${dest}/kometa-db.sql"
        ok "Дамп PostgreSQL: kometa-db.sql.gz ($(human_size "${dest}/kometa-db.sql.gz"))"
        return 0
    fi

    src="${DATA_DIR}/kometa.db"
    if [[ ! -f "$src" ]]; then
        warn "База бота не найдена: ${src} — возможно, бот ещё не запускался."
        return 0
    fi
    log "Копирую базу бота ${src}..."
    copy_sqlite "$src" "${dest}/kometa.db" || die "Не удалось скопировать базу бота."
    check_sqlite "${dest}/kometa.db" || die "Копия базы бота повреждена — архив не создаю."

    # Текстовый дамп: читается глазами и восстанавливается без sqlite3.
    if have sqlite3; then
        sqlite3 "$src" .dump 2>/dev/null | gzip -c > "${dest}/kometa-db.sql.gz" || true
    fi
    ok "База бота: kometa.db ($(human_size "${dest}/kometa.db"))"
}

# ------------------------------------------------------------ бэкап панели ----
backup_panel() { # backup_panel <каталог назначения>
    local dest="$1" found=0 db target

    for db in "$XUI_DB_NATIVE" "$XUI_DB_DOCKER"; do
        [[ -f "$db" ]] || continue
        found=1
        # Имена разные: на одном сервере теоретически могут оказаться обе базы
        # (нативная и докерная) — не перезаписываем одну другой.
        if [[ "$db" == "$XUI_DB_NATIVE" ]]; then
            target="${dest}/x-ui.db"
        else
            target="${dest}/x-ui-docker.db"
        fi
        log "Копирую базу панели 3x-ui: ${db}"
        copy_sqlite "$db" "$target" || { warn "Не удалось скопировать ${db}"; continue; }
        check_sqlite "$target" || warn "Копия базы панели повреждена — проверь панель."
        ok "База панели: $(basename "$target") ($(human_size "$target"))"
    done

    # Конфиги панели (что нашлось — то и копируем).
    if [[ -r "$XUI_INSTALL_RESULT" ]]; then
        cp -f "$XUI_INSTALL_RESULT" "${dest}/install-result.env"
        chmod 600 "${dest}/install-result.env"
        found=1
    fi
    if [[ -f /etc/default/x-ui ]]; then
        cp -f /etc/default/x-ui "${dest}/default-x-ui"
        found=1
    fi
    if [[ -f /etc/systemd/system/x-ui.service ]]; then
        cp -f /etc/systemd/system/x-ui.service "${dest}/x-ui.service"
        found=1
    fi

    # Сертификаты панели, если они лежат в стандартном месте.
    if [[ -d /root/cert ]]; then
        mkdir -p "${dest}/cert"
        cp -rf /root/cert/. "${dest}/cert/" 2>/dev/null || true
        found=1
    fi

    if [[ "$found" -eq 0 ]]; then
        warn "Данные панели 3x-ui не найдены (ни ${XUI_DB_NATIVE}, ни ${XUI_DB_DOCKER})."
        warn "Если панель на другом сервере — запусти backup.sh и там."
    fi
}

# ------------------------------------------------------------- манифест -------
write_manifest() { # write_manifest <каталог> <файл>
    local dir="$1" out="$2"
    {
        printf 'Kometa backup\n'
        printf 'created_at : %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
        printf 'hostname   : %s\n' "$(hostname 2>/dev/null || echo '?')"
        printf 'root_dir   : %s\n' "$ROOT_DIR"
        printf '\n--- содержимое ---\n'
        (cd "$dir" && find . -type f -exec ls -l {} \; 2>/dev/null | awk '{print $5, $9}')
        printf '\n--- версии ---\n'
        printf 'bash       : %s\n' "${BASH_VERSION:-?}"
        printf 'sqlite3    : %s\n' "$(sqlite3 --version 2>/dev/null | awk '{print $1}' || echo 'нет')"
        if [[ -x /usr/local/x-ui/x-ui ]]; then
            printf '3x-ui      : %s\n' "$(/usr/local/x-ui/x-ui -v 2>/dev/null || echo '?')"
        fi
        printf '\n--- как восстановить ---\n'
        printf '1) БД бота (SQLite):  останови бота, положи файл на место:\n'
        printf '     docker compose stop bot\n'
        printf '     cp kometa.db data/kometa.db\n'
        printf '     docker compose start bot\n'
        printf '   либо из текстового дампа:\n'
        printf '     gunzip -c kometa-db.sql.gz | sqlite3 data/kometa.db\n'
        printf '2) БД панели:  systemctl stop x-ui && cp x-ui.db /etc/x-ui/x-ui.db && systemctl start x-ui\n'
    } > "$out"
}

# ---------------------------------------------------------------- очистка -----
cleanup_old() {
    local deleted=0 f
    log "Удаляю архивы старше ${KEEP_DAYS} дней..."
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        rm -f "$f"
        warn "Удалён старый архив: $(basename "$f")"
        deleted=$((deleted + 1))
    done <<EOF
$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'kometa-backup-*.tar.gz' -mtime "+${KEEP_DAYS}" 2>/dev/null || true)
EOF
    if [[ "$deleted" -eq 0 ]]; then
        ok "Старых архивов нет."
    else
        ok "Удалено старых архивов: ${deleted}."
    fi
    return 0
}

# ------------------------------------------------------------------ main ------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --out)       BACKUP_DIR="${2:?--out требует значение}"; shift 2 ;;
            --keep-days) KEEP_DAYS="${2:?--keep-days требует значение}"; shift 2 ;;
            --no-panel)  WITH_PANEL=0; shift ;;
            --no-rsync)  USE_RSYNC=0; shift ;;
            -h | --help) usage; exit 0 ;;
            *) die "Неизвестный флаг: $1 (см. --help)" ;;
        esac
    done

    have tar || die "Нужен tar."
    have gzip || die "Нужен gzip."

    local stamp staging archive
    stamp="$(date '+%Y-%m-%d_%H%M%S')"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR" 2>/dev/null || true

    WORK_DIR="$(mktemp -d)"
    trap 'rm -rf "${WORK_DIR:-}"' EXIT
    staging="${WORK_DIR}/kometa-${stamp}"
    mkdir -p "${staging}/panel"

    printf '%sKOMETA • БЭКАП%s  %s\n' "$C_BOLD" "$C_OFF" "$(date '+%Y-%m-%d %H:%M:%S')"

    backup_bot_db "$staging"

    if [[ "$WITH_PANEL" -eq 1 ]]; then
        backup_panel "${staging}/panel"
    else
        warn "Бэкап панели пропущен (--no-panel)."
    fi

    write_manifest "$staging" "${staging}/MANIFEST.txt"

    archive="${BACKUP_DIR}/kometa-backup-${stamp}.tar.gz"
    log "Собираю архив..."
    tar -czf "$archive" -C "$WORK_DIR" "kometa-${stamp}" || die "tar завершился с ошибкой."
    chmod 600 "$archive" 2>/dev/null || true

    tar -tzf "$archive" >/dev/null 2>&1 || die "Архив не читается — что-то пошло не так."
    ok "Архив проверен (tar -tzf): читается."

    cleanup_old

    # Необязательная выгрузка в отдельное место (правило «бэкап не на той же ноде»).
    local rsync_target
    rsync_target="$(env_get BACKUP_RSYNC_TARGET || true)"
    if [[ "$USE_RSYNC" -eq 1 && -n "$rsync_target" ]]; then
        if have rsync; then
            log "Выгружаю архив в ${rsync_target}..."
            if rsync -a --timeout=60 "$archive" "$rsync_target"; then
                ok "Архив выгружен во внешнее место."
            else
                warn "rsync не смог выгрузить архив — локальная копия на месте."
            fi
        else
            warn "rsync не установлен — внешняя выгрузка пропущена (apt-get install -y rsync)."
        fi
    fi

    printf '\n%s══════════════════════════════════════════════════════════════%s\n' "$C_BOLD" "$C_OFF"
    printf '%sГОТОВО%s\n' "$C_BOLD" "$C_OFF"
    printf '  Архив : %s\n' "$archive"
    printf '  Размер: %s\n' "$(human_size "$archive")"
    printf '  SHA256: %s\n' "$(sha256_of "$archive")"
    printf '  Хранение: %s дней (в каталоге %s)\n' "$KEEP_DAYS" "$BACKUP_DIR"
    printf '%s══════════════════════════════════════════════════════════════%s\n\n' "$C_BOLD" "$C_OFF"
}

main "$@"
