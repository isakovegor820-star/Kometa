"""Тесты аварийных скриптов: XHTTP- и MTProto-инбаунды (без панели и без сети).

Зачем тест, а не памятка. Оба скрипта задуманы как «аварийный уровень»: их
запускают в день ограничений, когда панель под рукой, а времени на разбор нет.
Дороже всего в такой день ошибиться в трёх местах:

  * XHTTP в режиме ``packet-up`` — по замеру августа 2026 это 605 запросов/мин
    и ~870 тыс./сутки на четырёх клиентов; один CDN забанил ресурс навсегда.
    Поэтому режим в скрипте — константа ``stream-up``, а не флаг;
  * MTProto на панели младше 3.5.0 — протокола там просто нет, панель ответит
    отказом уже после того, как оператор решит, что «всё сделано»;
  * «DRY-RUN», который на самом деле ходит в панель, — прямой путь создать
    дубль инбаунда на боевой ноде, поэтому здесь есть проверка с подменённым
    ``curl``: любой сетевой вызов оставил бы файл-маркер.

Проверяем статически и прогоном (машина разработчика — macOS, панели и ноды
здесь нет): ``bash -n``, ``--help``, dry-run с закрытым портом вместо панели и
grep по тексту скриптов. Ни один тест не поднимает панель и не ходит в сеть.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
XHTTP = ROOT / "scripts" / "add_xhttp_inbound.sh"
MTPROTO = ROOT / "scripts" / "add_mtproto_inbound.sh"

BASH = shutil.which("bash") or "/bin/bash"

# Панель-заглушка: порт 9 (discard) закрыт, поэтому ЛЮБОЙ настоящий запрос
# провалился бы — и тест «dry-run не ходит в сеть» был бы красным.
FAKE_PANEL = "http://127.0.0.1:9"
FAKE_TOKEN = "test-token"

# Переменные, которые могут прилететь из окружения разработчика и подменить
# параметры прогона: их всегда вычищаем перед запуском.
SCRIPT_ENV_VARS = (
    "PANEL_URL", "PANEL_TOKEN", "DOMAIN", "SNI", "CDN_HOST", "PORT",
    "LISTEN_ADDR", "XHTTP_PATH", "REMARK", "CERT_FILE", "KEY_FILE",
    "CLIENT_EMAIL", "CLIENT_UUID", "BEHIND_CDN", "ORIGIN_SELFSIGNED",
    "FAKE_TLS_DOMAIN", "SECRET", "PUBLIC_HOST", "CREATE_CLIENT",
)

# Аргументы, без которых скрипты не строят payload (XHTTP требует домен).
DRY_RUN_ARGS = {
    XHTTP: ["--domain", "vpn.example.com"],
    MTPROTO: ["--port", "8443"],
}


@pytest.fixture(scope="module")
def xhttp_text() -> str:
    return XHTTP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mtproto_text() -> str:
    return MTPROTO.read_text(encoding="utf-8")


def clean_env(**overrides: str) -> dict:
    """Окружение без наших переменных + то, что нужно конкретному тесту."""
    env = dict(os.environ)
    for name in SCRIPT_ENV_VARS:
        env.pop(name, None)
    env.update(overrides)
    return env


def run(script: Path, *args: str, env: dict | None = None,
        path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    """Запустить скрипт через bash (git-бит не нужен) и собрать вывод целиком."""
    env = dict(env if env is not None else clean_env())
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [BASH, str(script), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def output(result: subprocess.CompletedProcess) -> str:
    """stdout + stderr: предупреждения скрипты пишут в stderr, и это нормально."""
    return f"{result.stdout}\n{result.stderr}"


@pytest.fixture
def curl_shim(tmp_path: Path):
    """Подменённый curl: любой вызов оставляет маркер и падает с кодом 7.

    Так проверяется именно отсутствие сетевого вызова, а не отсутствие ошибки:
    настоящий curl к порту 9 тоже упал бы, и тест был бы слепым.
    """
    marker = tmp_path / "curl-called"
    shim = tmp_path / "curl"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{marker}"\n'
        "exit 7\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return tmp_path, marker


# --- 1. Синтаксис ---------------------------------------------------------


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_bash_syntax_ok(script: Path):
    """`bash -n` — скрипты запускаются на ноде, где нет ни линтера, ни pytest."""
    result = subprocess.run(
        [BASH, "-n", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_set_euo_pipefail(script: Path):
    """`set -euo pipefail` обязателен: без него ошибка curl/jq уходит в тишину."""
    text = script.read_text(encoding="utf-8")
    assert re.search(r"^set -euo pipefail$", text, re.M)


# --- 2. --help ------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "flags"),
    [
        (
            XHTTP,
            ["--port", "--domain", "--path", "--remark", "--sni",
             "--behind-cdn", "--origin-selfsigned", "--apply", "--dry-run",
             "PANEL_URL", "PANEL_TOKEN"],
        ),
        (
            MTPROTO,
            ["--port", "--domain", "--sni", "--remark", "--secret",
             "--apply", "--dry-run", "PANEL_URL", "PANEL_TOKEN"],
        ),
    ],
    ids=["xhttp", "mtproto"],
)
def test_help_exits_zero_and_documents_flags(script: Path, flags: list[str]):
    """--help должен работать без PANEL_URL и без панели: это первое, что читают."""
    result = run(script, "--help")
    assert result.returncode == 0, output(result)
    text = output(result)
    missing = [flag for flag in flags if flag not in text]
    assert not missing, f"в --help нет флагов: {missing}"


# --- 3. Обязательные параметры -------------------------------------------


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_dry_run_without_panel_url_fails_clearly(script: Path):
    """Без PANEL_URL — понятная ошибка, а не пустой payload и не «тихий» успех."""
    result = run(script, *DRY_RUN_ARGS[script], env=clean_env(PANEL_TOKEN=FAKE_TOKEN))
    assert result.returncode != 0
    text = output(result)
    assert "PANEL_URL" in text, text
    assert "--panel-url" in text, "подсказка должна называть и флаг, и переменную"


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_dry_run_without_panel_token_fails_clearly(script: Path):
    """Токен тоже обязателен: без него запрос к API бессмыслен."""
    result = run(script, *DRY_RUN_ARGS[script], env=clean_env(PANEL_URL=FAKE_PANEL))
    assert result.returncode != 0
    assert "PANEL_TOKEN" in output(result)


# --- 4. Dry-run: payload есть, сети нет ----------------------------------


def test_curl_shim_control(curl_shim):
    """Контроль: подменённый curl действительно вызывается и пишет маркер.

    Без этой проверки «маркера нет» означало бы «шим не работает», и тест
    про отсутствие сети был бы зелёным всегда.
    """
    shim_dir, marker = curl_shim
    probe = subprocess.run(
        ["curl", "--version"],
        env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 7
    assert marker.exists(), "шим не сработал — тесты на сеть были бы слепыми"
    marker.unlink()


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_dry_run_prints_payload_and_never_touches_network(script: Path, curl_shim):
    """Главная гарантия: по умолчанию скрипт печатает payload, но не ходит в панель."""
    shim_dir, marker = curl_shim
    result = run(
        script,
        *DRY_RUN_ARGS[script],
        env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN),
        path_prefix=shim_dir,
    )
    text = output(result)
    assert result.returncode == 0, text
    assert "DRY-RUN" in text
    assert "Сетевых запросов нет" in text, "нужен явный маркер отсутствия сети"
    assert '"remark"' in text and '"port"' in text, "payload должен печататься целиком"
    assert not marker.exists(), f"скрипт всё-таки вызвал curl: {marker.read_text()}"


def test_xhttp_dry_run_payload_is_vless_stream_up(curl_shim):
    """В payload — ровно vless/xhttp/stream-up и loopback-адрес прослушивания."""
    shim_dir, _marker = curl_shim
    result = run(
        XHTTP,
        *DRY_RUN_ARGS[XHTTP],
        env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN),
        path_prefix=shim_dir,
    )
    text = output(result)
    # streamSettings печатается JSON-строкой внутри JSON (так устроено API 3x-ui),
    # поэтому кавычки внутри неё экранированы — сравниваем по «расплющенному» виду.
    flat = text.replace("\\", "")
    assert '"protocol": "vless"' in text
    assert '"network":"xhttp"' in flat
    assert '"mode":"stream-up"' in flat
    assert '"listen": "127.0.0.1"' in text, "инбаунд не должен торчать наружу мимо nginx"


def test_mtproto_dry_run_payload_has_fake_tls_domain(curl_shim):
    """MTProto: протокол mtproto, settings без streamSettings, fakeTlsDomain на месте."""
    shim_dir, _marker = curl_shim
    result = run(
        MTPROTO,
        "--port", "8443", "--domain", "www.cloudflare.com",
        env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN),
        path_prefix=shim_dir,
    )
    text = output(result)
    assert '"protocol": "mtproto"' in text
    assert "fakeTlsDomain" in text
    assert '"streamSettings": "{}"' in text, "MTProto обслуживает mtg, а не Xray"


# --- 5. XHTTP: stream-up, nginx, CDN -------------------------------------


def test_xhttp_mode_is_hardcoded_stream_up(xhttp_text: str):
    """Режим — не флаг, а константа: packet-up = гарантированный бан CDN."""
    assert re.search(r'^XHTTP_MODE="stream-up"$', xhttp_text, re.M)
    assert '"mode":"packet-up"' not in xhttp_text
    assert "--mode" not in xhttp_text, "флага выбора режима быть не должно"


def test_xhttp_warns_about_packet_up_cost(xhttp_text: str):
    """Предупреждение должно называть цену packet-up цифрами и источником."""
    assert "packet-up" in xhttp_text
    assert "605 запросов" in xhttp_text
    assert "870" in xhttp_text
    assert "entry-points-ru" in xhttp_text


def test_xhttp_prints_nginx_location(xhttp_text: str):
    """nginx-локация: proxy_pass на порт инбаунда, HTTP/1.1, буферизация off, таймауты."""
    assert "proxy_pass http://${LISTEN_ADDR}:${PORT}" in xhttp_text
    assert "proxy_http_version 1.1" in xhttp_text
    assert "proxy_buffering off" in xhttp_text
    assert "proxy_request_buffering off" in xhttp_text, "stream-up буферизуется и рвётся"
    assert "proxy_read_timeout 1h" in xhttp_text
    assert "proxy_send_timeout 1h" in xhttp_text
    assert "location ${XHTTP_PATH}" in xhttp_text


def test_xhttp_cdn_hints(xhttp_text: str):
    """За CDN: проксирование, WebSockets + HTTP/2, origin только по https."""
    assert "Проксирование (Cloudflare" in xhttp_text
    assert "WebSockets — ON" in xhttp_text
    assert "HTTP/2 — ON" in xhttp_text
    assert "3. ORIGIN только по https" in xhttp_text
    assert "Full (strict)" in xhttp_text, "предупреждение про самоподписанный origin"


# --- 6. MTProto: версия панели и секрет ----------------------------------


def test_mtproto_gates_panel_version(mtproto_text: str):
    """Версия берётся так же, как в install_node.sh, и сравнивается с 3.5.0."""
    assert 'MIN_PANEL_VERSION="3.5.0"' in mtproto_text
    assert "/panel/api/server/status" in mtproto_text
    assert ".obj.panelVersion" in mtproto_text
    assert "version_ge" in mtproto_text, "сравнение версий как в install_node.sh"
    assert "panel_version_api" in mtproto_text and "panel_version_local" in mtproto_text


def test_mtproto_dry_run_mentions_version_requirement(curl_shim):
    """Без --apply версия — предупреждение (сети нет), но требование названо."""
    shim_dir, marker = curl_shim
    result = run(
        MTPROTO,
        "--port", "8443",
        # Прячем локальную панель: на сервере, где 3x-ui установлена, скрипт иначе
        # узнаёт версию локально и до подсказки про --check-version не доходит.
        env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN, XUI_BIN="/nonexistent/x-ui"),
        path_prefix=shim_dir,
    )
    text = output(result)
    assert result.returncode == 0
    assert "3.5.0" in text
    assert "--check-version" in text, "должен быть способ проверить версию явно"
    assert not marker.exists()


def test_mtproto_apply_aborts_on_old_panel(mtproto_text: str):
    """С --apply старая панель — ошибка (выход 1), а не «попробуем и посмотрим»."""
    assert 'if [[ "$version_rc" -eq 1 && "$APPLY" -eq 1 ]]' in mtproto_text
    assert "print_version_hint" in mtproto_text
    assert "install_panel.sh" in mtproto_text, "подсказка должна вести к обновлению"


def test_mtproto_does_not_invent_secret(mtproto_text: str):
    """Секрет сам не генерируем: панель выводит его из fakeTlsDomain."""
    assert "--secret" in mtproto_text
    assert "openssl rand -hex 16" in mtproto_text, "команда для своего секрета"
    assert "сгенерирует секрет сама" in mtproto_text
    assert re.search(r"if \[\[ -n \"\$SECRET\" \]\]; then", mtproto_text), (
        "secret отправляется только если задан явно"
    )


def test_mtproto_dry_run_prints_placeholder_link(curl_shim):
    """Без --secret ссылка печатается с плейсхолдером, а не с выдуманным секретом."""
    shim_dir, _marker = curl_shim
    result = run(
        MTPROTO,
        "--port", "8443",
        env=clean_env(PANEL_URL=FAKE_PANEL, PANEL_TOKEN=FAKE_TOKEN),
        path_prefix=shim_dir,
    )
    text = output(result)
    assert "tg://proxy?server=" in text
    assert "<СЕКРЕТ_ИЗ_ПАНЕЛИ>" in text
    assert "--secret" in text


# --- 7. Идемпотентность ---------------------------------------------------


@pytest.mark.parametrize(
    ("script", "remark", "default_line", "prefix_call"),
    [
        (
            XHTTP,
            "Kometa-XHTTP",
            'REMARK="Kometa-XHTTP"',
            'find_inbound_by_prefix "$existing" "Kometa-XHTTP"',
        ),
        (
            MTPROTO,
            "Kometa-MTProto",
            'REMARK="Kometa-MTProto"',
            'find_inbound_by_prefix "$existing" "Kometa-MTProto"',
        ),
    ],
    ids=["xhttp", "mtproto"],
)
def test_idempotency_by_remark(script: Path, remark: str, default_line: str,
                               prefix_call: str):
    """Повторный запуск ищет инбаунд по remark (и по семейству Kometa-*) и не дублирует."""
    text = script.read_text(encoding="utf-8")
    assert "/panel/api/inbounds/list" in text, "поиск идёт по списку инбаундов"
    assert "find_inbound_by_remark" in text
    assert default_line in text, f"дефолтный remark «{remark}» должен быть в скрипте"
    assert prefix_call in text, "старые имена того же семейства тоже надо находить"
    assert "не дублирую" in text
    assert "идемпотентность" in text


def test_remarks_do_not_embed_port():
    """Порт в remark создал бы второй инбаунд при смене --port."""
    for script in (XHTTP, MTPROTO):
        text = script.read_text(encoding="utf-8")
        defaults = [
            value for value in re.findall(r'REMARK="([^"]*)"', text)
            if "Kometa" in value
        ]
        assert defaults, f"{script.name}: не нашёл дефолтный remark"
        for value in defaults:
            assert "${PORT}" not in value, f"порт попал в remark: {value}"


# --- 8. Финальный блок «Как проверить» и зависимости ----------------------


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_has_how_to_check_block(script: Path):
    """В конце — блок «Как проверить», как в install_node.sh."""
    text = script.read_text(encoding="utf-8")
    assert "КАК ПРОВЕРИТЬ" in text
    assert "ss -tlnp | grep" in text
    assert "journalctl -u x-ui" in text


@pytest.mark.parametrize("script", [XHTTP, MTPROTO], ids=["xhttp", "mtproto"])
def test_external_dependencies_are_limited(script: Path):
    """Кроме curl/jq/openssl — ничего обязательного (ss/lsof только как проба)."""
    text = script.read_text(encoding="utf-8")
    used = set(re.findall(r"command -v ([A-Za-z0-9_.-]+)", text))
    used |= set(re.findall(r"^\s*have ([A-Za-z0-9_.-]+)", text, re.M))
    assert used <= {"jq", "curl", "ss", "lsof"}, f"лишние зависимости: {used}"
    assert "python" not in text and "pip " not in text
