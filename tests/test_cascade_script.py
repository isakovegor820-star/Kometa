"""Тесты скрипта каскада «вход в РФ → зарубежный выход» (без панели и без сети).

Зачем тест, а не памятка. ``scripts/add_cascade_exit.sh`` правит ГЛОБАЛЬНЫЙ
шаблон Xray панели (``setting.xrayTemplateConfig``) — то есть одну строку, от
которой зависит и входной узел, и все его клиенты. Ошибка здесь стоит дороже
всего в проекте:

  * правило «напрямую» (``direct``) для разрешённых РФ-сервисов обязано стоять
    ПЕРВЫМ, а правило на выход — ПОСЛЕДНИМ. Без direct-правила клиентский
    трафик к разрешённым сайтам уходит на выход, и подсеть «выгорает» именно
    на этом (docs/ОБХОД-БЕЛЫХ-СПИСКОВ-ПЛАН.md §5, docs/БЕЛЫЕ-СПИСКИ-ВНЕДРЕНИЕ.md §2.1);
  * повторный запуск не должен плодить второй ``outbound`` и вторые правила —
    поэтому идемпотентность проверяется и по тексту, и живьём через шим curl;
  * запись в панель без бэкапа недопустима, а «DRY-RUN», который на самом деле
    ходит в сеть, — прямой путь испортить боевой вход. Отсюда проверка с
    подменённым ``curl``: любой сетевой вызов оставил бы файл-маркер.

Проверяем статически и прогоном (машина разработчика — macOS, панели и ноды
здесь нет): ``bash -n``, ``--help``, dry-run и apply на шиме ``curl``, который
отвечает как 3x-ui. Ни один тест не поднимает панель и не ходит в сеть.
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
SCRIPT = ROOT / "scripts" / "add_cascade_exit.sh"

BASH = shutil.which("bash") or "/bin/bash"

# Панель-заглушка: порт 9 (discard) закрыт, поэтому ЛЮБОЙ настоящий запрос
# провалился бы — и тест «dry-run не ходит в сеть» был бы красным.
FAKE_PANEL = "http://127.0.0.1:9"
FAKE_TOKEN = "test-token"

EXIT_HOST = "exit.example.com"
EXIT_UUID = "11111111-2222-3333-4444-555555555555"
EXIT_PBK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 43 символа, как x25519
EXIT_SNI = "www.microsoft.com"
EXIT_SID = "0123456789abcdef"

# Аргументы, без которых скрипт не строит payload (аналог --domain у XHTTP).
EXIT_ARGS = [
    "--exit-host", EXIT_HOST,
    "--exit-uuid", EXIT_UUID,
    "--exit-pbk", EXIT_PBK,
    "--exit-sni", EXIT_SNI,
    "--exit-sid", EXIT_SID,
]

# Переменные, которые могут прилететь из окружения разработчика и подменить
# параметры прогона: их всегда вычищаем перед запуском.
SCRIPT_ENV_VARS = (
    "PANEL_URL", "PANEL_TOKEN", "EXIT_HOST", "EXIT_PORT", "EXIT_UUID", "EXIT_PBK",
    "EXIT_SNI", "EXIT_SID", "EXIT_FLOW", "EXIT_FP", "EXIT_REMARK", "DIRECT_FILE",
    "BACKUP_FILE", "DOMAIN_STRATEGY",
    "CURL_LOG", "CURL_STATE", "CURL_POSTED", "CURL_SETTINGS", "FAKE_MODE",
)

# Штатный шаблон 3x-ui (internal/web/service/config.json — те же outbounds,
# служебное правило api и правила blocked). «Было» для diff и содержимое
# xrayTemplateConfig в шиме панели.
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
      * normal  — GET /panel/setting/all отдаёт настройки из CURL_SETTINGS,
                  POST /panel/setting/update «сохраняет» шаблон в CURL_STATE
                  и возвращает success (проверка после записи видит результат);
      * get405  — на GET /panel/setting/all отвечает отказом (в живом 3x-ui
                  эндпоинт объявлен как POST) — проверяем фолбэк;
      * empty   — xrayTemplateConfig пуст — проверяем понятную ошибку-подсказку.
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
        '          jq -n --rawfile tpl "${CURL_STATE}" '
        '\'{success:true,obj:{xrayTemplateConfig:$tpl}}\'\n'
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
    return {
        "dir": shim_dir,
        "log": log,
        "state": state,
        "posted": posted,
        "settings": settings,
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
    )
    env["PATH"] = f"{shim['dir']}{os.pathsep}{env.get('PATH', '')}"
    env.update(overrides)
    return env


# --- 1. Синтаксис и базовые требования ------------------------------------


def test_bash_syntax_ok():
    """`bash -n` — скрипт запускают на ноде, где нет ни линтера, ни pytest."""
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_set_euo_pipefail(script_text: str):
    """`set -euo pipefail` обязателен: иначе ошибка curl/jq уходит в тишину."""
    assert re.search(r"^set -euo pipefail$", script_text, re.M)


def test_no_openssl_and_only_jq_curl_dependencies(script_text: str):
    """Зависимости — jq и curl (curl только с --apply); openssl не нужен.

    Все параметры Reality берутся с выходной ноды флагами, поэтому скрипт
    ничего не генерирует сам. `diff` — необязательный помощник: без него
    печатается разбор по секциям.
    """
    # openssl упомянут только в шапке («не нужен»), но не вызывается:
    assert "openssl rand" not in script_text
    assert not re.search(r"(\$\(|`|\|\s*)openssl\b", script_text)
    used = set(re.findall(r"^\s*have ([A-Za-z0-9_.-]+)", script_text, re.M))
    assert used <= {"jq", "curl", "diff"}, f"лишние зависимости: {used}"
    assert "python" not in script_text and "pip " not in script_text
    # curl проверяется только на ветке --apply, jq — всегда.
    assert re.search(r'have jq \|\| die', script_text)
    assert re.search(r'have curl \|\| die "Нужен curl для --apply\."', script_text)


# --- 2. --help ------------------------------------------------------------


def test_help_exits_zero_and_documents_flags():
    """--help должен работать без PANEL_URL и без панели: это первое, что читают."""
    result = run("--help", env=clean_env())
    assert result.returncode == 0, output(result)
    text = output(result)
    flags = [
        "--panel-url", "--panel-token", "--exit-host", "--exit-port", "--exit-uuid",
        "--exit-pbk", "--exit-sni", "--exit-sid", "--exit-flow", "--exit-fp",
        "--exit-remark", "--direct-file", "--backup-file", "--inbound-tag",
        "--domain-strategy", "--apply", "--dry-run", "--insecure",
        "-h, --help", "PANEL_URL", "PANEL_TOKEN",
    ]
    missing = [flag for flag in flags if flag not in text]
    assert not missing, f"в --help нет флагов: {missing}"
    assert "DRY-RUN" in text and "--apply" in text


# --- 3. Обязательные параметры -------------------------------------------


def test_dry_run_without_panel_url_fails_clearly():
    """Без PANEL_URL — понятная ошибка, а не пустой payload и не «тихий» успех."""
    result = run(*EXIT_ARGS, env=clean_env(PANEL_TOKEN=FAKE_TOKEN))
    assert result.returncode != 0
    text = output(result)
    assert "PANEL_URL" in text, text
    assert "--panel-url" in text, "подсказка должна называть и флаг, и переменную"


def test_dry_run_without_panel_token_fails_clearly():
    """Токен тоже обязателен: без него запрос к API бессмыслен."""
    result = run(*EXIT_ARGS, env=clean_env(PANEL_URL=FAKE_PANEL))
    assert result.returncode != 0
    text = output(result)
    assert "PANEL_TOKEN" in text
    assert "--panel-token" in text


@pytest.mark.parametrize(
    ("flag", "args"),
    [
        ("--exit-host", ["--exit-uuid", EXIT_UUID, "--exit-pbk", EXIT_PBK, "--exit-sni", EXIT_SNI]),
        ("--exit-uuid", ["--exit-host", EXIT_HOST, "--exit-pbk", EXIT_PBK, "--exit-sni", EXIT_SNI]),
        ("--exit-pbk", ["--exit-host", EXIT_HOST, "--exit-uuid", EXIT_UUID, "--exit-sni", EXIT_SNI]),
        ("--exit-sni", ["--exit-host", EXIT_HOST, "--exit-uuid", EXIT_UUID, "--exit-pbk", EXIT_PBK]),
    ],
    ids=["host", "uuid", "pbk", "sni"],
)
def test_missing_exit_param_fails_clearly(flag: str, args: list[str]):
    """Без параметров Reality каскад не собрать: ошибка называет флаг."""
    result = run(*args, env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN))
    assert result.returncode != 0
    text = output(result)
    assert flag in text, text
    assert "Обязателен" in text, text


def test_invalid_uuid_is_rejected():
    """VLESS не примет не-UUID: лучше остановиться до записи в панель."""
    args = list(EXIT_ARGS)
    args[args.index("--exit-uuid") + 1] = "not-a-uuid"
    result = run(*args, env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN))
    assert result.returncode != 0
    assert "UUID" in output(result)


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
    """Главная гарантия: по умолчанию скрипт печатает payload, но не ходит в панель."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS,
        "--backup-file", str(shim["tmp"] / "backup.json"),
        env=shim_env(shim),
        path_prefix=shim["dir"],
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "DRY-RUN" in text
    assert "Сетевых запросов нет" in text, "нужен явный маркер отсутствия сети"
    assert "Kometa-Exit" in text and "routing" in text
    assert not shim["log"].exists(), f"скрипт всё-таки вызвал curl: {shim['log'].read_text()}"
    assert not (shim["tmp"] / "backup.json").exists(), "в dry-run бэкап не создаётся"


def test_dry_run_payload_is_vless_reality_to_exit(curl_shim):
    """В outbound — ровно VLESS + Reality с параметрами выхода и flow Vision."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS,
        "--exit-port", "8443",
        "--exit-remark", "Kometa-Exit",
        env=shim_env(shim),
        path_prefix=shim["dir"],
    )
    text = output(result)
    body = flat(text)
    assert result.returncode == 0, text
    assert '"protocol":"vless"' in body
    assert '"security":"reality"' in body
    assert f'"address":"{EXIT_HOST}"' in body
    assert '"port":8443' in body
    assert f'"id":"{EXIT_UUID}"' in body
    assert '"flow":"xtls-rprx-vision"' in body
    assert f'"publicKey":"{EXIT_PBK}"' in body
    assert f'"serverName":"{EXIT_SNI}"' in body
    assert f'"shortId":"{EXIT_SID}"' in body
    assert '"fingerprint":"firefox"' in body
    assert '"mux":{"enabled":false}' in body, "Vision и mux несовместимы — mux выключен"


def test_dry_run_payload_has_routing_rules(curl_shim):
    """Правила: direct для РФ-списка и «всё остальное» на выход, оба с ruleTag."""
    shim = curl_shim
    result = run(*EXIT_ARGS, env=shim_env(shim), path_prefix=shim["dir"])
    text = output(result)
    body = flat(text)
    assert result.returncode == 0, text
    assert "routing" in text
    assert "geosite:category-ru" in body
    assert "geoip:ru" in body
    assert '"outboundTag":"direct"' in body
    assert '"ruleTag":"Kometa-Direct"' in body
    assert '"ruleTag":"Kometa-Exit-Rule"' in body
    assert '"outboundTag":"Kometa-Exit","network":"tcp,udp"' in body, (
        "последнее правило — ловушка на выход"
    )
    assert "domain:gosuslugi.ru" in body and "domain:sberbank.ru" in body


def test_empty_flow_and_sid_are_omitted(curl_shim):
    """Явные пустые --exit-flow/--exit-sid — это «без flow» и «без shortId»."""
    shim = curl_shim
    result = run(
        "--exit-host", EXIT_HOST, "--exit-uuid", EXIT_UUID,
        "--exit-pbk", EXIT_PBK, "--exit-sni", EXIT_SNI,
        "--exit-flow", "", "--exit-sid", "",
        env=shim_env(shim), path_prefix=shim["dir"],
    )
    text = output(result)
    assert result.returncode == 0, text
    outbound = text.split("OUTBOUND «")[1].split("ПРАВИЛА МАРШРУТИЗАЦИИ")[0]
    assert '"flow"' not in outbound, "пустой flow = ключа быть не должно"
    assert '"shortId"' not in outbound, "пустой shortId = ключа быть не должно"
    assert '"mux"' in outbound
    assert "shortId (--exit-sid) не задан" in text, "об этом надо предупредить"


def test_domain_strategy_is_validated_and_normalized(curl_shim):
    """`--domain-strategy as-is` принимается и нормализуется к форме Xray (AsIs)."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS, "--domain-strategy", "as-is",
        env=shim_env(shim), path_prefix=shim["dir"],
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "domainStrategy: AsIs" in text, "значение нормализовано к форме Xray"
    assert "geoip:ru" in text and "AsIs" in text  # предупреждение о цене AsIs

    bad = run(*EXIT_ARGS, "--domain-strategy", "fakedns",
              env=shim_env(shim), path_prefix=shim["dir"])
    assert bad.returncode != 0
    assert "--domain-strategy" in output(bad)


def test_direct_list_splits_domains_and_ips(curl_shim):
    """geosite:/domain: идут в domain[], geoip:/CIDR — в ip[] (иначе Xray не примет)."""
    shim = curl_shim
    direct = shim["tmp"] / "direct.txt"
    direct.write_text(
        "# комментарий целиком\n"
        "geosite:category-ru   # inline-комментарий\n"
        "domain:example.ru\n"
        "geoip:ru\n"
        "10.0.0.0/8\n"
        "192.168.1.1\n",
        encoding="utf-8",
    )
    result = run(
        *EXIT_ARGS, "--direct-file", str(direct),
        env=shim_env(shim), path_prefix=shim["dir"],
    )
    text = output(result)
    body = flat(text)
    assert result.returncode == 0, text
    rule = body.split('"domain":[')[1].split('"ip":[')[0]
    ips = body.split('"ip":[')[1].split(']')[0]
    assert "geosite:category-ru" in rule and "domain:example.ru" in rule
    assert "комментарий" not in body, "строки-комментарии не должны попадать в правило"
    assert "geoip:ru" in ips and "10.0.0.0/8" in ips and "192.168.1.1" in ips
    assert "domain:example.ru" not in ips


def test_direct_file_without_entries_fails():
    """Пустой список «напрямую» = весь трафик через выход и выгорание подсети."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("# только комментарии\n\n")
        path = fh.name
    try:
        result = run(
            *EXIT_ARGS, "--direct-file", path,
            env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN),
        )
        assert result.returncode != 0
        text = output(result)
        assert "--direct-file" in text
        assert "выгорит" in text or "пуст" in text.lower()
    finally:
        os.unlink(path)


def test_inbound_tag_scopes_exit_rule(curl_shim):
    """--inbound-tag (можно повторять) ограничивает правило-ловушку каскада."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS,
        "--inbound-tag", "Kometa-Reality-443",
        "--inbound-tag", "Kometa-XHTTP",
        env=shim_env(shim),
        path_prefix=shim["dir"],
    )
    text = output(result)
    body = flat(text)
    assert result.returncode == 0, text
    assert '"inboundTag":["Kometa-Reality-443","Kometa-XHTTP"]' in body
    assert "Kometa-Reality-443" in text


def test_dry_run_prints_unified_diff(curl_shim):
    """В dry-run видно «было/стало»: без diff правку глобального шаблона не проверить."""
    shim = curl_shim
    result = run(*EXIT_ARGS, env=shim_env(shim), path_prefix=shim["dir"])
    text = output(result)
    assert result.returncode == 0, text
    assert "DIFF" in text
    if shutil.which("diff"):
        assert "--- было" in text and "+++ стало" in text
        assert "@@" in text
    # Разбор по секциям печатается всегда — он и есть «хотя бы по outbound и rules».
    assert "outbounds было" in text and "outbounds стало" in text
    assert "правила было" in text and "правила стало" in text
    assert "Kometa-Direct" in text.split("правила стало")[1]


def test_defaults_are_the_documented_ones(script_text: str):
    """Дефолты из ТЗ: 443, xtls-rprx-vision, firefox, Kometa-Exit, встроенный список."""
    assert 'EXIT_PORT="${EXIT_PORT:-443}"' in script_text
    assert 'EXIT_FLOW="${EXIT_FLOW:-xtls-rprx-vision}"' in script_text
    assert 'EXIT_FP="${EXIT_FP:-firefox}"' in script_text
    assert 'EXIT_REMARK="${EXIT_REMARK:-Kometa-Exit}"' in script_text
    assert 'DOMAIN_STRATEGY="${DOMAIN_STRATEGY:-IPIfNonMatch}"' in script_text
    for token in ("geosite:category-ru", "geoip:ru", "geoip:private",
                  "domain:gosuslugi.ru", "domain:vk.com", "domain:yandex.ru",
                  "domain:ozon.ru", "domain:wildberries.ru", "domain:sberbank.ru",
                  "domain:tbank.ru"):
        assert token in script_text, f"встроенный список потерял {token}"


# --- 5. Идемпотентность ---------------------------------------------------


def test_idempotency_by_exit_tag(script_text: str):
    """Повторный запуск ищет outbound по тегу и не плодит второй."""
    assert "count_outbound_tag" in script_text
    assert "не дублирую" in script_text
    assert "обновляю его" in script_text or "обновляю" in script_text
    assert 'DIRECT_RULE_TAG="Kometa-Direct"' in script_text
    assert 'EXIT_RULE_TAG="Kometa-Exit-Rule"' in script_text
    assert "ruleTag" in script_text, "наши правила должны опознаваться при повторном запуске"
    # Прежний outbound с нашим тегом снимается перед добавлением нового.
    assert "map(select((.tag // \"\") != $tag))" in script_text


def test_dry_run_reports_idempotency(curl_shim):
    """Про идемпотентность сказано и в выводе, а не только в коде."""
    shim = curl_shim
    result = run(*EXIT_ARGS, env=shim_env(shim), path_prefix=shim["dir"])
    text = output(result)
    assert result.returncode == 0, text
    assert "не дублирую" in text
    assert "обновляет" in text or "обновляю" in text


# --- 6. Бэкап -------------------------------------------------------------


def test_backup_is_written_before_update(script_text: str):
    """Бэкап — обязателен и делается ДО записи в панель (иначе откатывать нечего)."""
    assert "--backup-file" in script_text
    assert "backup-xray-" in script_text
    assert '> "$BACKUP_FILE"' in script_text
    backup_at = script_text.index('> "$BACKUP_FILE"')
    update_at = script_text.index("api_post /panel/setting/update")
    assert backup_at < update_at, "бэкап должен сниматься до POST /panel/setting/update"
    assert "Бэкап" in script_text and "только с --apply" in script_text


def test_dry_run_says_backup_needs_apply(curl_shim):
    """В dry-run бэкапа нет и это сказано явно (иначе его ищут на диске)."""
    shim = curl_shim
    backup = shim["tmp"] / "backup.json"
    result = run(
        *EXIT_ARGS, "--backup-file", str(backup),
        env=shim_env(shim), path_prefix=shim["dir"],
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "только с --apply" in text
    assert not backup.exists()


# --- 7. API: чтение и запись ---------------------------------------------


def test_uses_settings_endpoints(script_text: str):
    """Чтение — /panel/setting/all, запись — POST /panel/setting/update."""
    assert "api_get /panel/setting/all" in script_text
    assert "api_post /panel/setting/update" in script_text
    assert "obj.xrayTemplateConfig" in script_text
    assert '"Authorization: Bearer ${PANEL_TOKEN}"' in script_text


def test_apply_writes_template_and_keeps_other_settings(curl_shim):
    """--apply: бэкап, POST на /panel/setting/update, проверка после записи."""
    shim = curl_shim
    backup = shim["tmp"] / "backup-xray-test.json"
    result = run(
        *EXIT_ARGS, "--apply", "--backup-file", str(backup),
        env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text

    # 1) бэкап содержит ИСХОДНЫЙ шаблон
    assert backup.exists(), "бэкап обязателен до записи"
    assert json.loads(backup.read_text(encoding="utf-8")) == STOCK_TEMPLATE

    # 2) запись ушла именно на /panel/setting/update и содержит наш outbound
    log = shim["log"].read_text(encoding="utf-8")
    assert "/panel/setting/update" in log
    posted = json.loads(shim["posted"].read_text(encoding="utf-8"))
    tpl = posted["xrayTemplateConfig"]
    assert isinstance(tpl, str), "xrayTemplateConfig в панели — JSON-СТРОКА"
    assert "Kometa-Exit" in tpl and "geosite:category-ru" in tpl
    assert "xtls-rprx-vision" in tpl and "reality" in tpl
    # 3) отправляется ПОЛНЫЙ объект настроек, а не одно поле (иначе панель обнулит прочее)
    assert posted["webPort"] == 2053
    assert posted["webBasePath"] == "/"
    # 4) после записи скрипт перечитывает конфиг и подтверждает тег
    assert "Подтверждено" in text
    assert "Бэкап" in text


def test_apply_without_template_hints_install_node(curl_shim):
    """Пустой xrayTemplateConfig — понятная ошибка с подсказкой про install_node.sh."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS, "--apply", "--backup-file", str(shim["tmp"] / "b.json"),
        env=shim_env(shim, FAKE_MODE="empty"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "xrayTemplateConfig" in text
    assert "install_node.sh" in text, "подсказка должна вести к установке ноды"
    assert not shim["posted"].exists(), "при пустом шаблоне писать в панель нельзя"


def test_read_falls_back_to_post_all(curl_shim):
    """Живой 3x-ui объявляет /panel/setting/all как POST — скрипт не должен падать."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS, "--apply", "--backup-file", str(shim["tmp"] / "b2.json"),
        env=shim_env(shim, FAKE_MODE="get405"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "POST /panel/setting/all" in text, "фолбэк должен быть назван в логе"
    lines = [ln for ln in shim["log"].read_text(encoding="utf-8").splitlines()
             if "/panel/setting/all" in ln]
    assert any("-X POST" not in ln for ln in lines), "первичное чтение — GET"
    assert any("-X POST" in ln for ln in lines), "фолбэк — POST"


def test_apply_keeps_api_rule_first_and_exit_last(curl_shim):
    """Служебное правило панели остаётся ВЫШЕ нашего direct, ловушка — последняя.

    Если поставить direct выше правила «inboundTag: api → api», служебный
    туннель панели (127.0.0.1 → geoip:private) уедет в direct и панель потеряет
    статистику. Штатные правила шаблона (blocked) при этом сохраняются.
    """
    shim = curl_shim
    result = run(
        *EXIT_ARGS, "--apply", "--backup-file", str(shim["tmp"] / "b3.json"),
        env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode == 0, output(result)
    tpl = json.loads(shim["state"].read_text(encoding="utf-8"))
    rules = tpl["routing"]["rules"]

    assert rules[0]["outboundTag"] == "api", "служебное правило панели должно остаться первым"
    assert rules[1]["ruleTag"] == "Kometa-Direct", "direct — сразу после служебных правил"
    assert rules[-1]["ruleTag"] == "Kometa-Exit-Rule", "ловушка на выход — последняя"
    assert [r.get("ruleTag") for r in rules].count("Kometa-Direct") == 1
    assert [r.get("ruleTag") for r in rules].count("Kometa-Exit-Rule") == 1
    # Штатные правила 3x-ui не потерялись.
    assert any(r.get("ruleTag") == "Kometa-Direct" and "geosite:category-ru" in r.get("domain", [])
               for r in rules)
    assert sum(1 for r in rules if r.get("outboundTag") == "blocked") == 2
    assert tpl["outbounds"][0]["tag"] == "direct"
    assert [o["tag"] for o in tpl["outbounds"]].count("Kometa-Exit") == 1


def test_apply_twice_is_idempotent(curl_shim):
    """Повторный --apply обновляет, а не дублирует: один outbound и два правила."""
    shim = curl_shim
    args = (*EXIT_ARGS, "--apply", "--backup-file", str(shim["tmp"] / "b4.json"))
    first = run(*args, env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"])
    assert first.returncode == 0, output(first)

    shim["log"].unlink(missing_ok=True)
    second = run(*args, env=shim_env(shim), path_prefix=shim["dir"], cwd=shim["tmp"])
    text = output(second)
    assert second.returncode == 0, text
    assert "не дублирую" in text
    assert "уже есть в шаблоне" in text

    tpl = json.loads(shim["state"].read_text(encoding="utf-8"))
    tags = [o["tag"] for o in tpl["outbounds"]]
    assert tags.count("Kometa-Exit") == 1, f"дубль outbound: {tags}"
    rule_tags = [r.get("ruleTag") for r in tpl["routing"]["rules"]]
    assert rule_tags.count("Kometa-Direct") == 1, f"дубль правил: {rule_tags}"
    assert rule_tags.count("Kometa-Exit-Rule") == 1, f"дубль правил: {rule_tags}"
    assert len(rule_tags) == 5, f"количество правил выросло: {rule_tags}"


def test_apply_update_failure_is_reported(curl_shim):
    """Панель отклонила шаблон — внятная ошибка и путь к бэкапу, а не «тихий» успех."""
    shim = curl_shim
    backup = shim["tmp"] / "b5.json"
    result = run(
        *EXIT_ARGS, "--apply", "--backup-file", str(backup),
        env=shim_env(shim, FAKE_MODE="updatefail"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "invalid xray config" in text
    assert str(backup) in text, "нужно назвать файл бэкапа для отката"
    assert backup.exists(), "бэкап снимается до записи — даже если запись провалилась"


def test_apply_confirms_tag_after_write(curl_shim):
    """Панель ответила success, но тега в конфиге нет — это ошибка, а не «готово»."""
    shim = curl_shim
    result = run(
        *EXIT_ARGS, "--apply", "--backup-file", str(shim["tmp"] / "b6.json"),
        env=shim_env(shim, FAKE_MODE="dropwrite"), path_prefix=shim["dir"], cwd=shim["tmp"],
    )
    assert result.returncode != 0
    text = output(result)
    assert "не найден в конфиге после записи" in text
    assert "Откат" in text and "journalctl -u x-ui -n 50" in text


# --- 8. Финальный блок «Как проверить» -----------------------------------


def test_has_how_to_check_block(script_text: str):
    """В конце — «Как проверить»: рестарт, логи, вход в ссылке и предупреждения."""
    assert "КАК ПРОВЕРИТЬ" in script_text
    assert "x-ui restart" in script_text
    assert "journalctl -u x-ui -n 50" in script_text
    assert "ВХОДНОЙ адрес" in script_text, "ссылка клиента должна указывать на вход"
    assert "НЕ гонять через вход разрешённые РФ-сервисы" in script_text
    assert "Разделять входной и выходной IP" in script_text
    assert "serverNames" in script_text and "Ротация" in script_text


def test_warns_about_private_rule_shadowing(script_text: str):
    """geoip:private в списке перекрывает штатное «private → blocked» — предупреждаем."""
    assert "geoip:private → blocked" in script_text
    assert "убери строку geoip:private" in script_text
