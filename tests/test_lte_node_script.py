"""Предохранитель: настройки ноды не расходятся с исследованием по LTE.

Зачем тест, а не памятка. Аудит корпуса исследований (`.research/lte-audit.md`,
раздел 5) нашёл три места, где скрипты делали ровно то, что собственные
документы проекта называют ошибкой:

  * `install_node.sh` ставил `fingerprint: chrome` — а chrome попал под
    эвристику ТСПУ «IP → фингерпринт → >3 параллельных TLS» (июнь 2026);
  * AmneziaWG по умолчанию слушал 51820/udp — а при детекте WG-рукопожатия
    оператор блокирует ВСЕ UDP-порты выше 1000 (NTC 22319, 18.02.2026);
  * MSS-clamp был «обязателен» по документам и не реализован нигде — это
    прямая причина «сайт открывается, SSH работает, а страницы висят» на LTE.

Проверяем статически: на машине разработчика (macOS) нет ни iptables, ни
панели, поэтому тест сверяет текст скрипта — значения, порты и форму правил.
Сам факт применения правил проверяется на ноде: `iptables -t mangle -S FORWARD`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE_SCRIPT = ROOT / "scripts" / "install_node.sh"
SUBSCRIPTION = ROOT / "app" / "web" / "subscription_format.py"


@pytest.fixture(scope="module")
def node_script() -> str:
    return NODE_SCRIPT.read_text(encoding="utf-8")


# --- 1. Фингерпринт Reality ----------------------------------------------


def test_default_fingerprint_is_not_chrome(node_script: str):
    """Дефолт — firefox: chrome/safari/ios палятся эвристикой июня 2026."""
    match = re.search(r'^REALITY_FP="\$\{REALITY_FP:-(\w+)\}"', node_script, re.M)
    assert match, "REALITY_FP должен задаваться с дефолтом"
    assert match.group(1) == "firefox"


def test_inbound_uses_fingerprint_variable(node_script: str):
    """В payload инбаунда уходит переменная, а не жёсткий chrome."""
    assert 'fingerprint:$fp' in node_script
    assert 'fingerprint:"chrome"' not in node_script


def test_chrome_fingerprint_warns(node_script: str):
    """Явный выбор chrome не запрещён, но предупреждает."""
    assert "чёрный список эвристики июня 2026" in node_script


def test_summary_prints_actual_fingerprint(node_script: str):
    """Итоговая справка печатает фактический fp, а не зашитый chrome.

    Регрессия: скрипт ставил firefox, а оператору в отчёте показывал chrome —
    диагностика уводила в сторону при разборе «почему не подключается».
    """
    assert "fingerprint: ${REALITY_FP}, spiderX: /" in node_script
    assert "fingerprint: chrome" not in node_script


# --- 2. Порт AmneziaWG ---------------------------------------------------


def test_default_awg_port_is_below_1000(node_script: str):
    """Порт по умолчанию — ниже 1000, вне зоны блокировки UDP >1000."""
    match = re.search(r'^AWG_PORT="\$\{AWG_PORT:-(\d+)\}"', node_script, re.M)
    assert match, "AWG_PORT должен задаваться с дефолтом"
    assert int(match.group(1)) < 1000, "порты >1000 блокируются по триггеру WG-рукопожатия"


def test_awg_remark_does_not_embed_port(node_script: str):
    """Имя инбаунда не зависит от порта, иначе смена порта создаёт дубль."""
    match = re.search(r'^AWG_REMARK="\$\{AWG_REMARK:-([^}]+)\}"', node_script, re.M)
    assert match
    assert "${AWG_PORT}" not in match.group(1)


def test_legacy_awg_remark_still_found(node_script: str):
    """Нода, настроенная до смены дефолта, не получает второй инбаунд."""
    assert 'AWG_REMARK_LEGACY="Kometa-AWG-51820"' in node_script
    assert 'find_inbound_by_remark "$existing" "$AWG_REMARK_LEGACY"' in node_script


# --- 3. MSS-clamp под LTE ------------------------------------------------


def test_mss_clamp_defaults_match_lte_mtu(node_script: str):
    """MTU 1280 даёт MSS 1240 для IPv4 и 1220 для IPv6."""
    assert 'LTE_MTU="${LTE_MTU:-1280}"' in node_script
    assert 'LTE_MSS_V4="${LTE_MSS_V4:-1240}"' in node_script
    assert 'LTE_MSS_V6="${LTE_MSS_V6:-1220}"' in node_script


def test_mss_rule_shape(node_script: str):
    """Правило подрезает только транзитный SYN в mangle/FORWARD."""
    rule = re.search(r"mss_rule\(\)\s*\{.*?\n\}", node_script, re.S)
    assert rule, "нужна функция mss_rule"
    body = rule.group(0)
    assert "-t mangle" in body
    assert "-C FORWARD" in body
    assert "--tcp-flags SYN,RST SYN" in body
    assert "--set-mss %s" in body


def test_mss_clamp_is_idempotent(node_script: str):
    """Повторный запуск не копит дубли: старые правила снимаются."""
    clamp = re.search(r"apply_mss_clamp\(\)\s*\{.*?\n\}", node_script, re.S)
    assert clamp, "нужна функция apply_mss_clamp"
    body = clamp.group(0)
    assert "-D >/dev/null" in body, "перед вставкой надо снять прежние правила"
    assert "-I >/dev/null" in body


def test_mss_clamp_runs_on_node_setup(node_script: str):
    """Clamp применяется при подготовке ноды и отключается флагом --no-mss."""
    assert "apply_mss_clamp" in node_script.split("main()")[1], "вызов должен быть в main"
    assert "--no-mss" in node_script


def test_panel_and_node_agree_on_awg_port():
    """Фаервол и нода не должны разойтись по порту.

    install_panel.sh открывает UDP-порт в фаерволе, install_node.sh создаёт на
    нём инбаунд. Если дефолты разные, трафик молча не доходит: порт открыт, но
    слушает его пустота.
    """
    panel = (ROOT / "scripts" / "install_panel.sh").read_text(encoding="utf-8")
    node = (ROOT / "scripts" / "install_node.sh").read_text(encoding="utf-8")

    def default_port(source: str) -> int:
        match = re.search(r'^AWG_PORT="\$\{AWG_PORT:-(\d+)\}"', source, re.M)
        assert match, "AWG_PORT должен задаваться с дефолтом"
        return int(match.group(1))

    assert default_port(panel) == default_port(node) < 1000


def test_mtu_flag_recomputes_mss(node_script: str):
    """--lte-mtu пересчитывает MSS из MTU, а не оставляет старые числа."""
    assert "LTE_MSS_V4=$((LTE_MTU - 40))" in node_script
    assert "LTE_MSS_V6=$((LTE_MTU - 60))" in node_script


# --- 4. Обфускация AmneziaWG не уходит в sing-box ------------------------


def test_singbox_builder_does_not_emit_amneziawg_fields():
    """sing-box не знает jc/jmin/s1-h4/i1 — неизвестное поле ломает конфиг.

    Проверено по документации sing-box: в структуре wireguard-endpoint
    (v1.13.0, v1.14.2) и устаревшего outbound (v1.12.0) этих полей нет.
    """
    source = SUBSCRIPTION.read_text(encoding="utf-8")
    builder = source.split("def _singbox_wireguard_outbound")[1].split("\ndef ")[0]
    for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "i1"):
        assert f'"{key}"' not in builder, f"поле {key} попадёт в sing-box и сломает конфиг"
