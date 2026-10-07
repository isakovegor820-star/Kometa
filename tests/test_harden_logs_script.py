"""Тесты скрипта гигиены логов (``scripts/harden_logs.sh``) — без сети и без панели.

Зачем тест, а не памятка. Скрипт правит ГЛОБАЛЬНЫЙ шаблон Xray панели
(``setting.xrayTemplateConfig``) и sniffing всех VLESS-инбаундов узла. Политика
конфиденциальности обещает «не храним историю посещений и DNS-запросы»
(app/legal_texts.py §2.3, docs/legal/ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ.md), и это
обещание держится ровно на двух вещах:

  * в шаблоне стоит ``"access": "none"`` и ``"dnsLog": false`` — иначе Xray
    пишет журнал доступа, то есть историю посещений;
  * ``sniffing.routeOnly = true`` — иначе разобранный домен подменяет адрес
    назначения, а не только участвует в маршрутизации.

Плюс две дорогие ошибки, которые тест ловит заранее:

  * «DRY-RUN», который на самом деле ходит в панель, — прямой путь испортить
    боевой узел. Поэтому каждый прогон идёт с подменённым ``curl``: любой
    настоящий запрос оставил бы файл-маркер (``CURL_LOG``);
  * запись без бэкапа. Позиция бэкапа в тексте скрипта сравнивается с позицией
    ``api_post /panel/setting/update`` — бэкап обязан быть раньше.

Машина разработчика — macOS, панели и ноды здесь нет: скрипт проверяется
статически и прогоном на шиме ``curl``, который отвечает как 3x-ui. Ни один
тест не поднимает панель и не ходит в сеть.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "harden_logs.sh"

BASH = shutil.which("bash") or "/bin/bash"

# Панель-заглушка: порт 9 (discard) закрыт, поэтому ЛЮБОЙ настоящий запрос
# провалился бы — и тест «dry-run не ходит в сеть» был бы красным.
FAKE_PANEL = "http://127.0.0.1:9"
FAKE_TOKEN = "test-token"

# Штатный шаблон 3x-ui: секции log нет ничего, кроме loglevel — то есть
# access-лог не выключен (internal/web/service/config.json).
STOCK_TEMPLATE = {
    "log": {"loglevel": "warning"},
    "inbounds": [],
    "outbounds": [
        {"protocol": "freedom", "settings": {}, "tag": "direct"},
        {"protocol": "blackhole", "settings": {}, "tag": "blocked"},
    ],
    "routing": {
        "domainStrategy": "AsIs",
        "rules": [
            {"inboundTag": ["api"], "outboundTag": "api", "type": "field"},
            {"ip": ["geoip:private"], "outboundTag": "blocked", "type": "field"},
            {"outboundTag": "blocked", "protocol": ["bittorrent"], "type": "field"},
        ],
    },
}

# Целевая секция log (ТЗ): access=none, loglevel=warning, dnsLog=false, error="".
TARGET_LOG = {"access": "none", "loglevel": "warning", "dnsLog": False, "error": ""}

SNIFFING_OFF = json.dumps(
    {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": False,
     "routeOnly": False},
    ensure_ascii=False,
)
SNIFFING_ON = json.dumps(
    {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": False,
     "routeOnly": True},
    ensure_ascii=False,
)

# Список инбаундов как его отдаёт 3x-ui: sniffing — JSON-СТРОКА внутри JSON.
# id=2 не VLESS (его sniffing не трогаем), id=4 уже routeOnly:true (пропуск).
INBOUNDS = {
    "success": True,
    "obj": [
        {
            "id": 1,
            "remark": "Kometa-Reality-443",
            "protocol": "vless",
            "port": 443,
            "settings": '{"clients":[],"decryption":"none"}',
            "streamSettings": '{"network":"tcp","security":"reality"}',
            "sniffing": SNIFFING_OFF,
            "clientStats": [{"id": 1, "email": "kometa@test", "up": 10, "down": 20}],
        },
        {
            "id": 2,
            "remark": "Kometa-AWG",
            "protocol": "amneziawg",
            "port": 990,
            "settings": "{}",
            "streamSettings": "{}",
            "sniffing": SNIFFING_OFF,
        },
        {
            "id": 3,
            "remark": "Kometa-XHTTP",
            "protocol": "vless",
            "port": 8443,
            "settings": '{"clients":[]}',
            "streamSettings": '{"network":"xhttp"}',
            "sniffing": SNIFFING_OFF,
        },
        {
            "id": 4,
            "remark": "Kometa-Reality-2",
            "protocol": "vless",
            "port": 8444,
            "settings": '{"clients":[]}',
            "streamSettings": '{"network":"tcp","security":"reality"}',
            "sniffing": SNIFFING_ON,
        },
    ],
}

# Переменные, которые могут прилететь из окружения разработчика и подменить
# параметры прогона: их всегда вычищаем перед запуском.
SCRIPT_ENV_VARS = (
    "PANEL_URL", "PANEL_TOKEN", "BACKUP_FILE", "INBOUNDS_BACKUP_FILE", "XUI_BIN",
    "CURL_LOG", "CURL_STATE", "CURL_POSTED", "CURL_SETTINGS", "FAKE_MODE",
    "CURL_INBOUNDS", "CURL_INBOUND_STATE", "CURL_INBOUND_POSTED",
)


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def clean_env(**overrides: str) -> dict:
    """Окружение без наших переменных + то, что нужно конкретному тесту."""
    env = dict(os.environ)
    for name in SCRIPT_ENV_VARS:
        env.pop(name, None)
    env.update(overrides)
    return env


def run(*args: str, env: dict | None = None, path_prefix: Path | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Запустить скрипт через bash (git-бит не нужен) и собрать вывод целиком."""
    env = dict(env if env is not None else clean_env())
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def output(result: subprocess.CompletedProcess) -> str:
    """stdout + stderr: предупреждения скрипты пишут в stderr, и это нормально."""
    return f"{result.stdout}\n{result.stderr}"


def flat(text: str) -> str:
    """Текст без пробелов — payload печатается pretty-JSON, сравнивать так проще."""
    return re.sub(r"\s+", "", text)


@pytest.fixture
def curl_shim(tmp_path: Path):
    """Подменённый curl: отвечает как панель 3x-ui и ничего не отправляет в сеть.

    Режимы через FAKE_MODE:
      * normal       — настройки из CURL_SETTINGS, запись «сохраняется» в
                       CURL_STATE, инбаунды из CURL_INBOUNDS/…_STATE, update
                       инбаунда применяется к состоянию (проверка после записи
                       видит результат);
      * get405       — GET /panel/setting/all отвечает отказом (в живом 3x-ui
                       эндпоинт объявлен как POST) — проверяем фолбэк;
      * empty        — xrayTemplateConfig пуст — понятная ошибка-подсказка;
      * updatefail   — панель отклоняет шаблон;
      * dropwrite    — success, но шаблон не сохранён (проверка после записи
                       обязана это поймать);
      * inbounddrop  — то же для update инбаунда.
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "curl"
    shim.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "${CURL_LOG}"\n'
        'url=""; data=""; method="GET"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    case "$1" in\n'
        '        -X) method="$2"; shift 2; continue ;;\n'
        '        -d) data="$2"; shift 2; continue ;;\n'
        '        -H|--max-time) shift 2; continue ;;\n'
        '        http://*|https://*) url="$1" ;;\n'
        '    esac\n'
        '    shift\n'
        'done\n'
        'case "$url" in\n'
        '  */panel/setting/all)\n'
        '      if [ "$method" = "GET" ] && [ "${FAKE_MODE:-normal}" = "get405" ]; then\n'
        '          printf \'{"success":false,"msg":"405 method not allowed"}\'\n'
        '      elif [ "${FAKE_MODE:-normal}" = "empty" ]; then\n'
        '          printf \'{"success":true,"obj":{"xrayTemplateConfig":""}}\'\n'
        '      elif [ -f "${CURL_STATE}" ]; then\n'
        '          jq -n --rawfile tpl "${CURL_STATE}" \'{success:true,obj:{xrayTemplateConfig:$tpl}}\'\n'
        '      else\n'
        '          cat "${CURL_SETTINGS}"\n'
        '      fi\n'
        '      ;;\n'
        '  */panel/setting/update)\n'
        '      case "${FAKE_MODE:-normal}" in\n'
        '        updatefail)\n'
        '            printf \'{"success":false,"msg":"invalid xray config"}\' ;;\n'
        '        dropwrite)\n'
        '            printf "%s" "$data" > "${CURL_POSTED}"\n'
        '            printf \'{"success":true,"msg":""}\' ;;\n'
        '        *)\n'
        '            printf "%s" "$data" > "${CURL_POSTED}"\n'
        '            printf "%s" "$data" | jq -r ".xrayTemplateConfig" > "${CURL_STATE}"\n'
        '            printf \'{"success":true,"msg":""}\' ;;\n'
        '      esac\n'
        '      ;;\n'
        '  */panel/api/inbounds/list)\n'
        '      if [ -f "${CURL_INBOUND_STATE}" ]; then\n'
        '          cat "${CURL_INBOUND_STATE}"\n'
        '      else\n'
        '          cat "${CURL_INBOUNDS}"\n'
        '      fi\n'
        '      ;;\n'
        '  */panel/api/inbounds/update/*)\n'
        '      inid="${url##*/}"\n'
        '      printf "%s\\t%s\\n" "$inid" "$data" >> "${CURL_INBOUND_POSTED}"\n'
        '      if [ "${FAKE_MODE:-normal}" = "inbounddrop" ]; then\n'
        '          printf \'{"success":true,"msg":""}\'\n'
        '      else\n'
        '          src="${CURL_INBOUNDS}"\n'
        '          [ -f "${CURL_INBOUND_STATE}" ] && src="${CURL_INBOUND_STATE}"\n'
        '          jq --argjson upd "$data" --argjson id "$inid" \\\n'
        '             \'.obj = [.obj[] | if .id == $id then (. + $upd) else . end]\' \\\n'
        '             "$src" > "${CURL_INBOUND_STATE}.new" \\\n'
        '            && mv "${CURL_INBOUND_STATE}.new" "${CURL_INBOUND_STATE}"\n'
        '          printf \'{"success":true,"msg":""}\'\n'
        '      fi\n'
        '      ;;\n'
        '  *)\n'
        '      printf \'{"success":false,"msg":"unexpected url in shim: %s"}\' "$url"\n'
        '      ;;\n'
        'esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    log = tmp_path / "curl.log"
    state = tmp_path / "panel-state.json"
    posted = tmp_path / "posted.json"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "success": True,
                "obj": {
                    "xrayTemplateConfig": json.dumps(STOCK_TEMPLATE, ensure_ascii=False, indent=2),
                    "webPort": 2053,
                    "webBasePath": "/",
                    "tgBotEnable": False,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    inbounds = tmp_path / "inbounds.json"
    inbounds.write_text(json.dumps(INBOUNDS, ensure_ascii=False), encoding="utf-8")
    inbound_state = tmp_path / "inbounds-state.json"
    inbound_posted = tmp_path / "inbounds-posted.log"
    return {
        "dir": shim_dir,
        "log": log,
        "state": state,
        "posted": posted,
        "settings": settings,
        "inbounds": inbounds,
        "inbound_state": inbound_state,
        "inbound_posted": inbound_posted,
        "tmp": tmp_path,
    }


def shim_env(shim: dict, **overrides: str) -> dict:
    """Окружение с шимом curl в PATH: ни один запуск не выйдет в сеть."""
    env = clean_env(
        PANEL_URL=FAKE_PANEL,
        PANEL_TOKEN=FAKE_TOKEN,
        CURL_LOG=str(shim["log"]),
        CURL_STATE=str(shim["state"]),
        CURL_POSTED=str(shim["posted"]),
        CURL_SETTINGS=str(shim["settings"]),
        CURL_INBOUNDS=str(shim["inbounds"]),
        CURL_INBOUND_STATE=str(shim["inbound_state"]),
        CURL_INBOUND_POSTED=str(shim["inbound_posted"]),
    )
    env["PATH"] = f"{shim['dir']}{os.pathsep}{env.get('PATH', '')}"
    env.update(overrides)
    return env


def settings_calls(shim: dict) -> list[str]:
    """Строки вызовов curl из лога шима (пусто, если лога нет)."""
    if not shim["log"].exists():
        return []
    return shim["log"].read_text(encoding="utf-8").splitlines()


def inbound_updates(shim: dict) -> list[dict]:
    """Разобранные тела POST /panel/api/inbounds/update/{id}: id + payload."""
    path = shim["inbound_posted"]
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        inid, _, raw = line.partition("\t")
        out.append({"id": inid, "payload": json.loads(raw)})
    return out


# --- 1. Синтаксис и базовые требования ------------------------------------


def test_bash_syntax_ok():
    """`bash -n` — скрипт запускают на ноде, где нет ни линтера, ни pytest."""
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_set_euo_pipefail(script_text: str):
    """`set -euo pipefail` обязателен: иначе ошибка curl/jq уходит в тишину."""
    assert re.search(r"^set -euo pipefail$", script_text, re.M)


def test_only_jq_curl_diff_dependencies(script_text: str):
    """Зависимости — jq (всегда) и curl (только с --apply); diff опционален.

    Никаких python/openssl: скрипт запускают на голой ноде.
    """
    used = set(re.findall(r"^\s*have ([A-Za-z0-9_.-]+)", script_text, re.M))
    assert used <= {"jq", "curl", "diff"}, f"лишние зависимости: {used}"
    assert re.search(r'have jq \|\| die', script_text)
    assert re.search(r'have curl \|\| die "Нужен curl для --apply\."', script_text)
    assert "openssl" not in script_text
    assert "python" not in script_text and "pip " not in script_text


# --- 2. --help ------------------------------------------------------------


def test_help_exits_zero_without_panel_and_documents_flags():
    """--help работает без PANEL_URL и без панели: это первое, что читают."""
    result = run("--help", env=clean_env())
    assert result.returncode == 0, output(result)
    text = output(result)
    flags = [
        "--panel-url", "--panel-token", "--fix-sniffing", "--only-sniffing",
        "--inbound-tag", "--backup-file", "--inbounds-backup-file",
        "--apply", "--dry-run", "-h, --help", "PANEL_URL", "PANEL_TOKEN",
    ]
    missing = [flag for flag in flags if flag not in text]
    assert not missing, f"в --help нет флагов: {missing}"
    assert "DRY-RUN" in text and "--apply" in text
    assert "routeOnly" in text, "нужно объяснить, что делает --fix-sniffing"
    assert "x-ui setting -h" in text, "про логи панели должно быть сказано в справке"


# --- 3. Обязательные параметры -------------------------------------------


def test_missing_panel_url_fails_clearly():
    """Без PANEL_URL — понятная ошибка, а не пустой payload и не «тихий» успех."""
    result = run(env=clean_env(PANEL_TOKEN=FAKE_TOKEN))
    assert result.returncode != 0
    text = output(result)
    assert "PANEL_URL" in text, text
    assert "--panel-url" in text, "подсказка должна называть и флаг, и переменную"


def test_missing_panel_token_fails_clearly():
    """Токен тоже обязателен: без него запрос к API бессмыслен."""
    result = run(env=clean_env(PANEL_URL=FAKE_PANEL))
    assert result.returncode != 0
    text = output(result)
    assert "PANEL_TOKEN" in text
    assert "--panel-token" in text


# --- 4. Dry-run: payload есть, сети нет ----------------------------------


def test_curl_shim_control(curl_shim):
    """Контроль: подменённый curl действительно вызывается и пишет маркер.

    Без этой проверки «маркера нет» означало бы «шим не работает», и тест про
    отсутствие сети был бы зелёным всегда.
    """
    shim = curl_shim
    probe = subprocess.run(
        ["curl", "-sS", f"{FAKE_PANEL}/panel/setting/all"],
        env=shim_env(shim),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0
    assert shim["log"].exists(), "шим не сработал — тесты на сеть были бы слепыми"
    assert "/panel/setting/all" in shim["log"].read_text(encoding="utf-8")


def test_dry_run_prints_payload_and_never_touches_network(curl_shim):
    """Главная гарантия: по умолчанию скрипт печатает план, но не ходит в панель."""
    shim = curl_shim
    backup = shim["tmp"] / "backup.json"
    inbounds_backup = shim["tmp"] / "backup-inbounds.json"
    result = run(
        "--fix-sniffing",
        "--backup-file", str(backup),
        "--inbounds-backup-file", str(inbounds_backup),
        env=shim_env(shim),
        path_prefix=shim["dir"],
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "DRY-RUN" in text
    assert "Сетевых запросов нет" in text, "нужен явный маркер отсутствия сети"
    assert not shim["log"].exists(), f"скрипт всё-таки вызвал curl: {shim['log'].read_text()}"
    assert not backup.exists(), "в dry-run бэкап шаблона не создаётся"
    assert not inbounds_backup.exists(), "в dry-run бэкап инбаундов не создаётся"


def test_dry_run_payload_has_target_log_section(curl_shim):
    """Целевая секция log: access=none, loglevel=warning, dnsLog=false, error=""."""
    shim = curl_shim
    result = run(env=shim_env(shim), path_prefix=shim["dir"])
    text = output(result)
    body = flat(text)
    assert result.returncode == 0, text
    assert '"access":"none"' in body, "access-лог Xray обязан быть выключен"
    assert '"loglevel":"warning"' in body
    assert '"dnsLog":false' in body, "DNS-запросы логироваться не должны"
    assert '"error":""' in body, "пустой error = stderr/systemd journal, без файла"
    assert "DIFF" in text, "правку глобального шаблона без diff не проверить"
    assert "ИДЕМПОТЕНТНОСТЬ" in text


def test_dry_run_with_fix_sniffing_prints_plan_without_network(curl_shim):
    """--fix-sniffing в dry-run показывает, что станет со sniffing, без сети."""
    shim = curl_shim
    result = run("--fix-sniffing", env=shim_env(shim), path_prefix=shim["dir"])
    text = output(result)
    body = flat(text)
    assert result.returncode == 0, text
    assert "--fix-sniffing" in text and "routeOnly" in text
    assert '"routeOnly":false' in body and '"routeOnly":true' in body, "нужно «было → стало»"
    assert "/panel/api/inbounds/update/{id}" in text or "/panel/api/inbounds/update" in text
    assert not shim["log"].exists(), "dry-run не должен ходить в панель"
    assert "SNIFFING" in text


def test_script_declares_fix_sniffing_and_routeonly(script_text: str):
    """Флаг --fix-sniffing и правка routeOnly — в самом скрипте, а не в комментарии."""
    assert "--fix-sniffing" in script_text
    assert "sniffing.routeOnly" in script_text or "routeOnly" in script_text
    assert ".routeOnly = true" in script_text, "правка должна быть именно в jq-фильтре"
    assert "protocol" in script_text and "vless" in script_text


# --- 5. Бэкап и запись ----------------------------------------------------


def test_backup_is_written_before_update(script_text: str):
    """Бэкап — обязателен и делается ДО записи в панель (иначе откатывать нечего)."""
    assert "--backup-file" in script_text
    assert "backup-xray-logs-" in script_text, "дефолтное имя бэкапа — из ТЗ"
    assert "backup-inbounds-sniffing-" in script_text
    assert '> "$BACKUP_FILE"' in script_text
    backup_at = script_text.index('> "$BACKUP_FILE"')
    update_at = script_text.index("api_post /panel/setting/update")
    assert backup_at < update_at, "бэкап должен сниматься до POST /panel/setting/update"
    assert "только с --apply" in script_text


def test_apply_writes_log_section_and_creates_backup(curl_shim):
    """--apply: бэкап исходного шаблона, POST на /panel/setting/update, проверка."""
    shim = curl_shim
    backup = shim["tmp"] / "backup-xray-logs-test.json"
    result = run(
        "--apply", "--backup-file", str(backup),
        env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text

    # 1) бэкап содержит ИСХОДНЫЙ шаблон (до правки)
    assert backup.exists(), "бэкап обязателен до записи"
    assert json.loads(backup.read_text(encoding="utf-8")) == STOCK_TEMPLATE

    # 2) запись ушла именно на /panel/setting/update и содержит целевую секцию log
    log = shim["log"].read_text(encoding="utf-8")
    assert "/panel/setting/update" in log
    posted = json.loads(shim["posted"].read_text(encoding="utf-8"))
    tpl = posted["xrayTemplateConfig"]
    assert isinstance(tpl, str), "xrayTemplateConfig в панели — JSON-СТРОКА"
    parsed = json.loads(tpl)
    assert parsed["log"] == TARGET_LOG
    # 3) остальной шаблон не потерян
    assert parsed["outbounds"] == STOCK_TEMPLATE["outbounds"]
    assert parsed["routing"] == STOCK_TEMPLATE["routing"]
    # 4) отправляется ПОЛНЫЙ объект настроек, а не одно поле
    assert posted["webPort"] == 2053
    assert posted["webBasePath"] == "/"
    # 5) после записи скрипт перечитывает конфиг и подтверждает
    assert "Подтверждено" in text
    assert "Бэкап" in text


def test_apply_second_run_says_already_and_does_not_write(curl_shim):
    """Идемпотентность: второй --apply печатает «уже настроено» и ничего не пишет."""
    shim = curl_shim
    args = ("--apply", "--backup-file", str(shim["tmp"] / "b-idem.json"))
    first = run(*args, env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"])
    assert first.returncode == 0, output(first)

    shim["log"].unlink(missing_ok=True)
    second = run(*args, env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"])
    text = output(second)
    assert second.returncode == 0, text
    assert "уже настроено" in text, "про уже настроенную секцию надо сказать явно"
    calls = settings_calls(shim)
    assert not any("/panel/setting/update" in line for line in calls), (
        f"повторный запуск всё-таки писал шаблон: {calls}"
    )
    # Проверка существующего значения действительно читает панель.
    assert any("/panel/setting/all" in line for line in calls)


def test_apply_update_failure_is_reported(curl_shim):
    """Панель отклонила шаблон — внятная ошибка, путь к бэкапу и готовая команда отката."""
    shim = curl_shim
    backup = shim["tmp"] / "b-fail.json"
    result = run(
        "--apply", "--backup-file", str(backup),
        env=shim_env(shim, FAKE_MODE="updatefail"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "invalid xray config" in text
    assert str(backup) in text, "нужно назвать файл бэкапа для отката"
    assert backup.exists(), "бэкап снимается до записи — даже если запись провалилась"
    # Откат печатается СРАЗУ: до блока «КАК ПРОВЕРИТЬ» выполнение не дойдёт.
    assert "ОТКАТ" in text
    assert "--rawfile" in text and "/panel/setting/update" in text, "нужна готовая команда отката"


def test_apply_confirms_log_section_after_write(curl_shim):
    """Панель ответила success, но шаблон не сохранён — это ошибка, а не «готово»."""
    shim = curl_shim
    backup = shim["tmp"] / "b-drop.json"
    result = run(
        "--apply", "--backup-file", str(backup),
        env=shim_env(shim, FAKE_MODE="dropwrite"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "секция log не совпадает с целевой" in text or "не совпадает" in text
    assert "Откат" in text and "journalctl -u x-ui -n 50" in text
    assert "ОТКАТ" in text and "--rawfile" in text, "команда отката — сразу, а не «см. п. 7»"


def test_read_falls_back_to_post_all(curl_shim):
    """Живой 3x-ui объявляет /panel/setting/all как POST — скрипт не должен падать."""
    shim = curl_shim
    result = run(
        "--apply", "--backup-file", str(shim["tmp"] / "b-405.json"),
        env=shim_env(shim, FAKE_MODE="get405"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "POST /panel/setting/all" in text, "фолбэк должен быть назван в логе"
    lines = [ln for ln in settings_calls(shim) if "/panel/setting/all" in ln]
    assert any("-X POST" not in ln for ln in lines), "первичное чтение — GET"
    assert any("-X POST" in ln for ln in lines), "фолбэк — POST"


def test_apply_without_template_hints_install_node(curl_shim):
    """Пустой xrayTemplateConfig — понятная ошибка с подсказкой про install_node.sh."""
    shim = curl_shim
    result = run(
        "--apply", "--backup-file", str(shim["tmp"] / "b-empty.json"),
        env=shim_env(shim, FAKE_MODE="empty"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "xrayTemplateConfig" in text
    assert "install_node.sh" in text, "подсказка должна вести к установке ноды"
    assert not shim["posted"].exists(), "при пустом шаблоне писать в панель нельзя"


# --- 6. Sniffing: точечная правка инбаундов -------------------------------


def test_apply_fix_sniffing_updates_only_vless(curl_shim):
    """Правятся только VLESS-инбаунды с routeOnly != true; остальные не трогаются."""
    shim = curl_shim
    backup = shim["tmp"] / "b-inbounds.json"
    result = run(
        "--fix-sniffing", "--apply",
        "--backup-file", str(shim["tmp"] / "b-tpl.json"),
        "--inbounds-backup-file", str(backup),
        env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text

    updates = inbound_updates(shim)
    ids = sorted(u["id"] for u in updates)
    assert ids == ["1", "3"], f"обновить надо только vless с routeOnly != true: {ids}"
    for upd in updates:
        payload = upd["payload"]
        sniffing = payload["sniffing"]
        assert isinstance(sniffing, str), "в 3x-ui sniffing — JSON-строка"
        parsed = json.loads(sniffing)
        assert parsed["routeOnly"] is True
        assert parsed["enabled"] is True, "остальные поля sniffing сохраняются"
        assert parsed["destOverride"] == ["http", "tls", "quic"]
        assert "clientStats" not in payload, "read-only статистику в update не шлём"
        assert payload["settings"] == '{"clients":[],"decryption":"none"}' or payload["settings"]

    # Инбаунд-не-VLESS (AmneziaWG) и уже настроенный (id=4) не обновлялись.
    assert "id=2" in text and "не VLESS" in text
    assert "id=4" in text and "routeOnly уже true" in text

    # Бэкап списка инбаундов снят ДО первой правки и содержит исходный sniffing.
    assert backup.exists()
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert json.loads(saved["obj"][0]["sniffing"])["routeOnly"] is False
    assert "Подтверждено" in text, "после записи список перечитывается"


def test_apply_fix_sniffing_is_idempotent(curl_shim):
    """Второй прогон: правок нет, ни одного POST на update инбаунда."""
    shim = curl_shim
    args = (
        "--fix-sniffing", "--apply",
        "--backup-file", str(shim["tmp"] / "b-idem-tpl.json"),
        "--inbounds-backup-file", str(shim["tmp"] / "b-idem-inb.json"),
    )
    first = run(*args, env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"])
    assert first.returncode == 0, output(first)
    assert len(inbound_updates(shim)) == 2

    shim["inbound_posted"].unlink(missing_ok=True)
    second = run(*args, env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"])
    text = output(second)
    assert second.returncode == 0, text
    assert "Правок нет" in text
    assert inbound_updates(shim) == [], "повторный запуск не должен писать инбаунды"


def test_only_sniffing_does_not_touch_template(curl_shim):
    """--only-sniffing: шаблон логов не читается и не пишется, sniffing правится."""
    shim = curl_shim
    result = run(
        "--only-sniffing", "--apply",
        "--backup-file", str(shim["tmp"] / "b-only.json"),
        "--inbounds-backup-file", str(shim["tmp"] / "b-only-inb.json"),
        env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text
    calls = settings_calls(shim)
    assert not any("/panel/setting/update" in line for line in calls), "шаблон писать нельзя"
    assert not any("/panel/setting/all" in line for line in calls), "шаблон даже не читаем"
    assert any("/panel/api/inbounds/update/" in line for line in calls)
    assert "не трогаю (--only-sniffing)" in text
    assert not (shim["tmp"] / "b-only.json").exists()


def test_inbound_tag_limits_sniffing_scope(curl_shim):
    """--inbound-tag ограничивает правку одним remark (можно повторять)."""
    shim = curl_shim
    result = run(
        "--fix-sniffing", "--apply",
        "--inbound-tag", "Kometa-Reality-443",
        "--backup-file", str(shim["tmp"] / "b-tag.json"),
        "--inbounds-backup-file", str(shim["tmp"] / "b-tag-inb.json"),
        env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text
    ids = sorted(u["id"] for u in inbound_updates(shim))
    assert ids == ["1"], f"--inbound-tag должен ограничить правку: {ids}"
    assert "не подходит под --inbound-tag" in text


def test_sniffing_verify_detects_lost_write(curl_shim):
    """Панель ответила success, но routeOnly не сохранился — это ошибка с откатом."""
    shim = curl_shim
    backup = shim["tmp"] / "b-lost-inb.json"
    result = run(
        "--fix-sniffing", "--apply",
        "--backup-file", str(shim["tmp"] / "b-lost-tpl.json"),
        "--inbounds-backup-file", str(backup),
        env=shim_env(shim, FAKE_MODE="inbounddrop"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "routeOnly" in text and "ожидалось true" in text
    assert str(backup) in text, "нужен путь к бэкапу списка инбаундов"
    assert backup.exists()
    assert "ОТКАТ SNIFFING" in text and "--data-binary" in text, "нужна команда отката по id"


# --- 7. Эндпоинты и блок «КАК ПРОВЕРИТЬ» ---------------------------------


def test_uses_required_endpoints(script_text: str):
    """Чтение/запись — ровно те эндпоинты 3x-ui, что заявлены в ТЗ."""
    for endpoint in (
        "/panel/setting/all",
        "/panel/setting/update",
        "/panel/api/inbounds/list",
        "/panel/api/inbounds/update",
    ):
        assert endpoint in script_text, f"нет эндпоинта {endpoint}"
    assert "obj.xrayTemplateConfig" in script_text
    assert '"Authorization: Bearer ${PANEL_TOKEN}"' in script_text


def test_has_how_to_check_block(script_text: str):
    """В конце — «КАК ПРОВЕРИТЬ»: рестарт, journalctl, каталоги логов, порты."""
    assert "КАК ПРОВЕРИТЬ" in script_text
    assert "systemctl restart x-ui" in script_text
    assert "journalctl -u x-ui -n 50" in script_text
    assert "ls -la /var/log/x-ui/ 2>/dev/null" in script_text
    assert "ss -tlnp" in script_text
    # как убедиться, что access-лог больше не растёт
    assert "access.log" in script_text
    assert "stat -c" in script_text or "не растёт" in script_text
    # сначала конфиг, потом обещание
    assert "ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ" in script_text
    assert "ТОЛЬКО ПОСЛЕ" in script_text


def test_panel_logs_are_hint_not_invented_flags(script_text: str):
    """Про логи панели — только подсказка: справка читается, флаги не выдумываются.

    Скрипт не знает, какие ключи есть в конкретной сборке 3x-ui (у 3.3.1 и 3.9.0
    справка разная, а у части сборок ключей про логи нет вовсе), поэтому
    единственные допустимые вызовы — `setting -h` и `help`, а строки про логи
    печатаются из реального вывода grep'ом.
    """
    # Справка действительно читается — и только она.
    assert re.search(r'"\$bin"\s+setting\s+-h', script_text)
    assert re.search(r'"\$bin"\s+help', script_text)
    # Ни один «полезный» флаг панели не выполняется: их нет как команд.
    for invented in ("setting -logLevel", "setting -logPath", "setting -logFile",
                     "setting -log level", "setting -log-dir"):
        assert invented not in script_text, f"выдуманный флаг панели: {invented}"
    # Ключи про логи только ищутся в выводе справки (grep-шаблон + сам grep).
    assert "-logLevel" in script_text
    assert "grep -Ei" in script_text
    # Отсутствие ключей и отсутствие бинаря — честная подсказка, а не молчание.
    assert "проверь вручную" in script_text
    assert "бинарь панели не найден" in script_text.lower() or "Бинарь панели не найден" in script_text
    assert "Настройки → Панель" in script_text or "Настройки → Панель/Xray" in script_text
    assert "НЕ выдумывай" in script_text


def test_failure_without_panel_binary_still_prints_manual_hint():
    """На машине без панели (как здесь) блок про её логи всё равно печатается."""
    result = run(env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN))
    text = output(result)
    assert result.returncode == 0, text
    assert "СЛУЖЕБНЫЕ ЛОГИ ПАНЕЛИ" in text
    assert "проверь вручную" in text
    assert "ls -la /var/log/x-ui/ 2>/dev/null" in text


def _fake_xui(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Фальшивый бинарь панели: пишет свои аргументы в лог и печатает справку."""
    binary = tmp_path / "x-ui"
    log = tmp_path / "xui.log"
    binary.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "${XUI_LOG}"\n' + body,
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, log


def test_panel_logs_keys_are_read_from_real_help(tmp_path: Path):
    """Если в справке панели ключи про логи есть — печатаются именно они.

    Ключи не выдумываются: скрипт вызывает только `setting -h` и показывает
    строки из её вывода. Никаких «полезных» флагов он при этом не выполняет.
    """
    binary, xlog = _fake_xui(
        tmp_path,
        "printf 'Usage: x-ui setting [options]\\n'\n"
        "printf '  -logLevel string  log level for the panel (debug|info|warning|error)\\n'\n"
        "printf '  -logPath string   path to the panel log file\\n'\n"
        "printf '  -port int         panel port\\n'\n",
    )
    result = run(env=clean_env(
        PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN,
        XUI_BIN=str(binary), XUI_LOG=str(xlog),
        CURL_LOG=str(tmp_path / "curl.log"),
    ))
    text = output(result)
    assert result.returncode == 0, text
    assert "-logLevel string" in text, "строка из справки должна быть напечатана как есть"
    assert "-logPath string" in text
    assert "применяй ТОЛЬКО их" in text
    assert xlog.read_text(encoding="utf-8").splitlines() == ["setting -h"], (
        "панель можно спрашивать только справкой — никаких выдуманных флагов"
    )
    assert not (tmp_path / "curl.log").exists(), "логи панели читаются локально, без сети"


def test_panel_logs_without_keys_honest_ui_hint(tmp_path: Path):
    """Ключей про логи в справке нет — честная подсказка про UI, без выдумок."""
    binary, xlog = _fake_xui(
        tmp_path,
        "printf 'Usage: x-ui setting [options]\\n'\n"
        "printf '  -port int   panel port\\n'\n",
    )
    result = run(env=clean_env(
        PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN,
        XUI_BIN=str(binary), XUI_LOG=str(xlog),
        CURL_LOG=str(tmp_path / "curl.log"),
    ))
    text = output(result)
    assert result.returncode == 0, text
    assert "ключей про логи НЕ нашлось" in text
    assert "UI панели" in text, "подсказка должна вести в UI, а не в выдуманный флаг"
    assert "проверь вручную" in text
    assert xlog.read_text(encoding="utf-8").splitlines() == ["setting -h"]
