"""Тесты ops-скриптов: сверка ID инбаундов и проверка нод мультигео.

Почему это тестируется, а не проверяется глазами. 07-08.10.2026 нода
«Нидерланды» сутки отдавала клиентам ошибку: в боте был записан ID 3, которого
в панели нет, а третий инбаунд имел ID 4. ``preflight.sh`` и ``healthcheck.sh``
при этом были зелёными, потому что:

* сверяли только ``PANEL_INBOUND_IDS`` из ``.env`` и ничего не знали про
  таблицу ``nodes`` (у каждой ноды своя панель и своя нумерация);
* искали ID грепом по сырому JSON — ``grep -q '"id":3[,}]'``. В ответе панели
  есть вложенный массив ``clientStats``, и у каждого его элемента **своё** поле
  ``id``: проверка находила «инбаунд 3» в статистике клиента.

Здесь проверяем разбор ответа панели (``scripts/panel_inbound_ids.py``) на
настоящей ловушке с ``clientStats`` и то, что оба скрипта зовут этот разбор и
инструмент нод, а не грепают JSON.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "panel_inbound_ids.py"
HEALTHCHECK = ROOT / "scripts" / "healthcheck.sh"
PREFLIGHT = ROOT / "scripts" / "preflight.sh"

BASH = shutil.which("bash") or "/bin/bash"
PYTHON = sys.executable

#: Ответ панели с ловушкой: инбаунда 3 нет, но id=3 есть у статистики клиента.
#: Именно на нём старый греп рапортовал «все ID на месте».
PANEL_RESPONSE = {
    "success": True,
    "obj": [
        {
            "id": 1,
            "remark": "Kometa-Reality-443",
            "protocol": "vless",
            "clientStats": [{"id": 3, "email": "u1"}, {"id": 9, "email": "u2"}],
        },
        {"id": 2, "remark": "Kometa-AWG-51820", "protocol": "amneziawg", "clientStats": [{"id": 77}]},
        {"id": 4, "remark": "Kometa-Reality-8443", "protocol": "vless", "clientStats": []},
    ],
}


def run_helper(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(HELPER)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


# ------------------------------------------------------- разбор ответа панели
def test_helper_ignores_client_stats_ids():
    """Главная регрессия: id статистики клиента — не инбаунд."""
    result = run_helper(json.dumps(PANEL_RESPONSE))

    assert result.returncode == 0
    assert result.stdout.split() == ["1", "2", "4"]
    assert "3" not in result.stdout.split(), "id=3 есть только у clientStats"
    assert "77" not in result.stdout.split()


def test_helper_accepts_bare_list_and_string_ids():
    """Старые панели отвечают и голым списком, и id строкой."""
    result = run_helper(json.dumps([{"id": "5"}, {"id": 6}]))

    assert result.returncode == 0
    assert result.stdout.split() == ["5", "6"]


def test_helper_rejects_non_json():
    """HTML ошибки вместо JSON — это не «инбаундов нет», а понятная ошибка."""
    result = run_helper("<html>502 Bad Gateway</html>")

    assert result.returncode == 1
    assert "не JSON" in result.stderr


def test_helper_skips_items_without_id():
    """Мусор в списке не должен превращаться в выдуманный инбаунд."""
    result = run_helper(json.dumps({"obj": [{"remark": "нет id"}, {"id": None}, "строка", {"id": 8}]}))

    assert result.returncode == 0
    assert result.stdout.split() == ["8"]


# -------------------------------------------------------------- сами скрипты
@pytest.mark.parametrize("script", [HEALTHCHECK, PREFLIGHT])
def test_scripts_are_valid_bash(script: Path):
    result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [HEALTHCHECK, PREFLIGHT])
def test_scripts_do_not_grep_raw_panel_json(script: Path):
    """Сверка ID обязана идти через разбор JSON, а не грепом по сырому ответу."""
    text = script.read_text()

    assert "panel_inbound_ids" in text, "скрипт должен звать общий разбор ID"
    assert "grep -o '\"id\":[0-9]*'" not in text, "старый греп считал и clientStats"
    assert "grep -q \"\\\"id\\\":${id}[,}]\"" not in text, "старый греп находил id статистики"


@pytest.mark.parametrize("script", [HEALTHCHECK, PREFLIGHT])
def test_scripts_check_multi_geo_nodes(script: Path):
    """Основная панель не знает про ноды: скрипт обязан проверить и их."""
    text = script.read_text()

    assert "app.tools.check_nodes" in text
    assert "нод" in text.lower()


def test_healthcheck_uses_https_when_certificate_is_configured():
    """С сертификатом веб-слой слушает https: проверка по http ругалась на живом сервисе."""
    text = HEALTHCHECK.read_text()

    assert "WEB_SSL_CERT" in text and "WEB_SSL_KEY" in text
    assert 'scheme="https"' in text
