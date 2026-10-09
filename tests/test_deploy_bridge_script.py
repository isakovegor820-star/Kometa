"""Тесты скрипта моста «RU-адрес → зарубежный выход» (День 0, шаг 6).

Зачем прогон, а не «посмотрели глазами». ``ops/30-deploy-bridge.sh`` меняет
боевой узел: пишет ``/usr/local/etc/xray/config.json``, выключает IPv6,
перезапускает службу и раздаёт ссылки живым людям. Цена ошибки здесь такая:

* **Ключи Reality на повторном запуске.** Скрипт обязан переиспользовать
  ключи и shortId из существующего конфига. Если бы он выпускал новые, каждый
  повторный запуск (например, чтобы дописать данные в реестр) рвал бы ссылки
  у всех трёх клиентов сразу;
* **«Одна ссылка на всех».** Одинаковый UUID дважды — это один клиент, а не
  три: отозвать одного человека, не тронув остальных, становится невозможно.
  Скрипт обязан сказать об этом прямо;
* **Отзыв.** Убрали UUID из ``--client-uuid`` — он должен исчезнуть из
  инбаунда и получить в реестре статус «отозван», а остальные — не шелохнуться;
* **Реестр users.csv** (имя, UUID, дата выдачи, платформа, оператор) —
  единственный след того, кто что получил. Ошибка в нём не восстанавливается,
  а имя вида ``=1+1`` при открытии в Excel выполняется как формула;
* **Изоляция проверки.** ``--dry-run`` обязан ничего не писать и не ходить в
  сеть: иначе «предпросмотр» на боевом узле меняет конфиг.

Прогоны идут на машине разработчика (macOS, bash 3.2, установленного ядра
нет): ядро Xray, ``systemctl``, ``curl`` и ``ufw`` подменены шимами, все пути
(конфиг, реестр) ведут во временный каталог. Сети тесты не касаются вообще —
шим ``curl`` только пишет в лог и падает.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "30-deploy-bridge.sh"
BASH = shutil.which("bash") or "/bin/bash"

#: Значения из плана Дня 0: выход за рубежом, мост — «свой сервер» в РФ.
EXIT_ADDRESS = "203.0.113.9"
EXIT_PORT = "443"
EXIT_UUID = "11111111-2222-3333-4444-555555555555"
EXIT_PUBKEY = "A" * 43
EXIT_SNI = "www.chip.de"
EXIT_SID = "0123456789abcdef"
BRIDGE_ADDRESS = "198.51.100.7"
BRIDGE_PUBKEY = "PUBLICKEYTEST000000000000000000000000000"
BRIDGE_PRIVKEY = "PRIVKEYTEST0000000000000000000000000000"

UUID_1 = "00000000-0000-0000-0000-000000000001"
UUID_2 = "00000000-0000-0000-0000-000000000002"
UUID_3 = "00000000-0000-0000-0000-000000000003"

CLIENTS = [
    f"{UUID_1},Иван,Android/Happ,МТС",
    f"{UUID_2},Пётр,iOS/Streisand,Билайн",
    f"{UUID_3},Аня,macOS/v2rayN,МегаФон",
]

REGISTRY_HEADER = "имя,uuid,дата_выдачи,платформа,оператор,статус,дата_отзыва"


# --------------------------------------------------------------------- шимы ---
# Шимы — обычные bash-скрипты: их видит и наш скрипт (через PATH), и тест
# (через лог-файлы). Всё, что они делают, — отвечают как настоящее ядро/служба.

XRAY_STUB = r"""#!/usr/bin/env bash
# Шим ядра Xray: version / x25519 [-i priv] / run -test -config FILE.
[[ -n "${XRAY_LOG:-}" ]] && echo "$*" >> "$XRAY_LOG"
case "${1:-}" in
    version)
        printf 'Xray %s (Xray, Penetrates Everything.) Custom (go1.25.0 linux/amd64)\n' "${XRAY_STUB_VERSION:-26.9.30}"
        exit 0 ;;
    x25519)
        if [[ "${2:-}" == "-i" ]]; then
            printf 'PrivateKey: %s\nPassword (PublicKey): PUB_OF_%s\n' "$3" "${3:0:8}"
        else
            printf 'PrivateKey: %s\nPassword (PublicKey): %s\n' \
                "${XRAY_STUB_PRIV:-PRIVKEYTEST0000000000000000000000000000}" \
                "${XRAY_STUB_PUB:-PUBLICKEYTEST000000000000000000000000000}"
        fi
        exit 0 ;;
    run|-test)
        if [[ "${XRAY_STUB_TEST_FAIL:-0}" == "1" ]]; then
            echo "failed to build config: unknown field" >&2
            exit 23
        fi
        echo "Configuration OK."
        exit 0 ;;
esac
exit 1
"""

SYSTEMCTL_STUB = r"""#!/usr/bin/env bash
[[ -n "${SYSTEMCTL_LOG:-}" ]] && echo "$*" >> "$SYSTEMCTL_LOG"
case "${1:-}" in
    is-active) echo "${SYSTEMCTL_ACTIVE:-active}"; exit 0 ;;
esac
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
# Любой настоящий поход в сеть отметился бы здесь. В тестах файла быть не должно.
echo "$*" >> "${CURL_LOG:-/tmp/kometa-curl-should-not-be-called.log}"
exit 22
"""

UFW_STUB = r"""#!/usr/bin/env bash
[[ -n "${UFW_LOG:-}" ]] && echo "$*" >> "$UFW_LOG"
case "${1:-}" in
    status) echo "Status: ${UFW_STATUS:-inactive}"; exit 0 ;;
esac
exit 0
"""

SS_STUB = r"""#!/usr/bin/env bash
[[ -n "${SS_LOG:-}" ]] && echo "$*" >> "$SS_LOG"
[[ -n "${SS_STUB_OUTPUT:-}" ]] && printf '%s\n' "$SS_STUB_OUTPUT"
exit 0
"""


def write_stub(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """Каталог с шимами ядра, systemctl, curl, ufw и ss."""
    d = tmp_path / "stub-bin"
    d.mkdir()
    write_stub(d / "xray", XRAY_STUB)
    write_stub(d / "systemctl", SYSTEMCTL_STUB)
    write_stub(d / "curl", CURL_STUB)
    write_stub(d / "ufw", UFW_STUB)
    write_stub(d / "ss", SS_STUB)
    return d


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def clean_env() -> dict:
    env = dict(os.environ)
    for name in ("XRAY_STUB_VERSION", "XRAY_STUB_TEST_FAIL", "XRAY_STUB_PRIV",
                 "XRAY_STUB_PUB", "XRAY_LOG", "SYSTEMCTL_LOG", "SYSTEMCTL_ACTIVE",
                 "CURL_LOG", "UFW_LOG", "UFW_STATUS", "SS_LOG", "SS_STUB_OUTPUT"):
        env.pop(name, None)
    return env


def run(*args: str, env_extra: dict | None = None, stub_bin: Path | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = clean_env()
    if stub_bin is not None:
        env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env.update(env_extra or {})
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def out(result: subprocess.CompletedProcess) -> str:
    """stdout + stderr: предупреждения скрипт пишет в stderr, и это нормально."""
    return f"{result.stdout}\n{result.stderr}"


def links(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("vless://")]


def base_args(*extra: str, dry_run: bool = True) -> list[str]:
    args = [
        "--bridge-address", BRIDGE_ADDRESS,
        "--exit-address", EXIT_ADDRESS,
        "--exit-port", EXIT_PORT,
        "--exit-uuid", EXIT_UUID,
        "--exit-pubkey", EXIT_PUBKEY,
        "--exit-sni", EXIT_SNI,
        "--exit-shortid", EXIT_SID,
    ]
    if dry_run:
        args.append("--dry-run")
    return [*args, *extra]


def apply_args(workdir: Path, *extra: str) -> list[str]:
    """Аргументы боевого прогона: пути в tmp, IPv6/MSS/установка не трогаются."""
    return base_args(
        "--allow-non-root",        # на маке root нет; на мосте этот флаг не нужен
        "--keep-ipv6",             # в тестах не пишем /etc/sysctl.d
        "--no-mss",                # iptables на маке нет
        "--no-install",            # ядро — шим из stub-bin
        "--xray-bin", str(workdir.parent / "stub-bin" / "xray"),
        "--config", str(workdir / "config.json"),
        "--registry", str(workdir / "users.csv"),
        *extra,
        dry_run=False,
    )


# ------------------------------------------------------------------ статика ---
def test_script_exists_and_has_bash_syntax():
    assert SCRIPT.is_file(), f"нет файла {SCRIPT}"
    proc = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_help_lists_contract_flags():
    proc = run("--help")
    text = out(proc)
    assert proc.returncode == 0
    for flag in ("--exit-address", "--exit-port", "--exit-uuid", "--exit-pubkey",
                 "--exit-sni", "--exit-shortid", "--client-uuid", "--client",
                 "--dry-run", "--registry", "--links-only"):
        assert flag in text, f"в справке нет {flag}"


def test_plan_command_shape_is_supported(workdir: Path, stub_bin: Path):
    """Команда из плана Дня 0 (только --client-uuid ×3) должна работать как есть."""
    proc = run(
        *base_args("--client-uuid", UUID_1, "--client-uuid", UUID_2, "--client-uuid", UUID_3),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    assert "Пользователей: 3" in text
    assert len(links(text)) == 3
    assert "Версия ядра: v26.9.30" in text


# ------------------------------------------------------------- валидация ------
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ([], "Обязателен --exit-address"),
        (["--exit-address", "203.0.113.9"], "Обязателен --exit-port"),
        (["--exit-address", "203.0.113.9", "--exit-port", "443"], "Обязателен --exit-uuid"),
        (["--exit-address", "203.0.113.9", "--exit-port", "443",
          "--exit-uuid", EXIT_UUID], "Обязателен --exit-pubkey"),
        (["--exit-address", "203.0.113.9", "--exit-port", "443",
          "--exit-uuid", EXIT_UUID, "--exit-pubkey", EXIT_PUBKEY], "Обязателен --exit-sni"),
    ],
)
def test_missing_exit_params_are_named(extra, expected):
    proc = run("--dry-run", *extra)
    assert proc.returncode != 0
    assert expected in out(proc)


def test_client_is_required():
    proc = run(*base_args(), stub_bin=None)
    assert proc.returncode != 0
    assert "Нужен хотя бы один клиент" in out(proc)


def test_broken_uuid_is_rejected():
    proc = run(*base_args("--client-uuid", "not-a-uuid"))
    assert proc.returncode != 0
    assert "не UUID" in out(proc)


def test_exit_hostname_requires_explicit_flag():
    args = ["--dry-run", "--exit-address", "exit.example.com", "--exit-port", "443",
            "--exit-uuid", EXIT_UUID, "--exit-pubkey", EXIT_PUBKEY, "--exit-sni", EXIT_SNI,
            "--exit-shortid", EXIT_SID, "--client-uuid", UUID_1]
    proc = run(*args)
    assert proc.returncode != 0
    assert "--allow-exit-hostname" in out(proc)
    proc2 = run(*args, "--allow-exit-hostname")
    assert proc2.returncode == 0


def test_unknown_flag_and_missing_value():
    proc = run(*base_args("--client-uuid", UUID_1, "--turbo"))
    assert proc.returncode != 0 and "Неизвестный аргумент" in out(proc)
    proc = run(*base_args("--client-uuid", UUID_1, "--exit-fp"))
    assert proc.returncode != 0 and "не хватает значения" in out(proc)


def test_apply_without_root_is_refused(stub_bin: Path):
    if os.geteuid() == 0:  # pragma: no cover - на мосте root, проверять нечего
        pytest.skip("под root проверка root не срабатывает")
    proc = run(*base_args("--client-uuid", UUID_1, dry_run=False), stub_bin=stub_bin)
    assert proc.returncode != 0
    assert "требует root" in out(proc)


def test_duplicate_uuid_warns_that_one_link_for_all_is_forbidden():
    proc = run(*base_args("--client-uuid", UUID_1, "--client-uuid", UUID_1))
    text = out(proc)
    assert proc.returncode == 0
    assert "указан повторно" in text and "одна ссылка на всех запрещена" in text
    assert "Пользователей: 1" in text
    assert len(links(text)) == 1


def test_chrome_fingerprint_is_flagged():
    proc = run(*base_args("--client-uuid", UUID_1, "--exit-fp", "chrome"))
    assert "чёрном списке эвристики июня 2026" in out(proc)
    assert "fp=firefox" not in out(proc)


# ------------------------------------------------------ dry-run: изоляция ----
def test_dry_run_writes_nothing_and_touches_no_network(workdir: Path, stub_bin: Path, tmp_path: Path):
    config = workdir / "config.json"
    registry = workdir / "users.csv"
    systemctl_log = tmp_path / "systemctl.log"
    curl_log = tmp_path / "curl.log"
    ufw_log = tmp_path / "ufw.log"
    xray_log = tmp_path / "xray.log"

    proc = run(
        *base_args(
            "--config", str(config), "--registry", str(registry),
            *(f"--client={c}" for c in CLIENTS),
        ),
        stub_bin=stub_bin,
        env_extra={
            "SYSTEMCTL_LOG": str(systemctl_log), "CURL_LOG": str(curl_log),
            "UFW_LOG": str(ufw_log), "XRAY_LOG": str(xray_log),
        },
    )
    text = out(proc)
    assert proc.returncode == 0, text
    assert not config.exists(), "dry-run не должен писать конфиг"
    assert not registry.exists(), "dry-run не должен писать реестр"
    assert not systemctl_log.exists(), "dry-run не должен звать systemctl"
    assert not curl_log.exists(), "dry-run не должен ходить в сеть (curl)"
    assert not ufw_log.exists(), "dry-run не должен трогать фаервол"


def test_dry_run_three_links_one_per_client_and_firefox(workdir: Path, stub_bin: Path):
    proc = run(
        *base_args(
            "--config", str(workdir / "config.json"), "--registry", str(workdir / "users.csv"),
            *(f"--client={c}" for c in CLIENTS),
        ),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    assert "Пользователей: 3" in text
    found = links(text)
    assert len(found) == 3
    assert len(set(found)) == 3, "ссылки обязаны отличаться: у каждого свой UUID"
    for uuid in (UUID_1, UUID_2, UUID_3):
        assert any(f"vless://{uuid}@" in link for link in found)
    for link in found:
        assert "security=reality" in link
        assert "fp=firefox" in link
        assert f"sni={EXIT_SNI}" in link
        assert "flow=xtls-rprx-vision" in link
        assert f"@{BRIDGE_ADDRESS}:443" in link
        assert "fp=chrome" not in link and "fp=safari" not in link and "fp=ios" not in link


def test_dry_run_config_matches_plan(workdir: Path, stub_bin: Path):
    proc = run(
        *base_args(
            "--config", str(workdir / "config.json"), "--registry", str(workdir / "users.csv"),
            *(f"--client={c}" for c in CLIENTS),
        ),
        stub_bin=stub_bin,
    )
    assert proc.returncode == 0, out(proc)
    # Конфиг печатается в stdout одной строкой JSON — забираем его из вывода.
    raw = re.search(r'^\{"log".*\}$', proc.stdout, re.MULTILINE)
    assert raw, "в dry-run не напечатан конфиг"
    cfg = json.loads(raw.group(0))

    inbound = cfg["inbounds"][0]
    assert inbound["tag"] == "bridge-in" and inbound["port"] == 443
    assert inbound["protocol"] == "vless"
    assert [c["id"] for c in inbound["settings"]["clients"]] == [UUID_1, UUID_2, UUID_3]
    assert inbound["sniffing"]["routeOnly"] is True, "домен нужен только для маршрутизации, не для подмены"
    reality = inbound["streamSettings"]["realitySettings"]
    assert reality["serverNames"] == [EXIT_SNI]
    assert reality["target"].endswith(":443")
    assert cfg["log"]["access"] == "none", "логи доступа выключены (обещание «не храним историю»)"

    exit_ob = [o for o in cfg["outbounds"] if o["tag"] == "exit"][0]
    vnext = exit_ob["settings"]["vnext"][0]
    assert vnext["address"] == EXIT_ADDRESS and vnext["port"] == int(EXIT_PORT)
    assert vnext["users"][0]["id"] == EXIT_UUID
    assert vnext["users"][0]["flow"] == "xtls-rprx-vision"
    ereality = exit_ob["streamSettings"]["realitySettings"]
    assert ereality["publicKey"] == EXIT_PUBKEY
    assert ereality["serverName"] == EXIT_SNI and ereality["shortId"] == EXIT_SID
    assert ereality["fingerprint"] == "firefox"

    tags = [r["ruleTag"] for r in cfg["routing"]["rules"]]
    assert tags == ["Kometa-Block-Private", "Kometa-Exit"], "по умолчанию РФ-прямых правил нет"
    assert cfg["routing"]["rules"][-1]["outboundTag"] == "exit"
    assert cfg["routing"]["domainStrategy"] == "AsIs", "мост сам ничего не резолвит — порт 53 молчит"


def test_direct_ru_adds_direct_rule_first(workdir: Path, stub_bin: Path):
    proc = run(
        *base_args("--direct-ru", "--client-uuid", UUID_1,
                   "--config", str(workdir / "config.json")),
        stub_bin=stub_bin,
    )
    raw = re.search(r'^\{"log".*\}$', proc.stdout, re.MULTILINE)
    assert raw
    cfg = json.loads(raw.group(0))
    rules = cfg["routing"]["rules"]
    assert [r["ruleTag"] for r in rules] == ["Kometa-Block-Private", "Kometa-Direct", "Kometa-Exit"]
    direct = rules[1]
    assert "geosite:category-ru" in direct["domain"] and "geoip:ru" in direct["ip"]


def test_bridge_sni_extra_lands_in_server_names(workdir: Path, stub_bin: Path):
    proc = run(
        *base_args("--bridge-sni", EXIT_SNI, "--bridge-sni-extra", "www.samsung.com,x5.ru",
                   "--client-uuid", UUID_1, "--config", str(workdir / "config.json")),
        stub_bin=stub_bin,
    )
    raw = re.search(r'^\{"log".*\}$', proc.stdout, re.MULTILINE)
    assert raw
    cfg = json.loads(raw.group(0))
    assert cfg["inbounds"][0]["streamSettings"]["realitySettings"]["serverNames"] == [
        EXIT_SNI, "www.samsung.com", "x5.ru",
    ]


# ----------------------------------------------------------- реестр users ----
def test_registry_preview_has_required_columns(workdir: Path, stub_bin: Path):
    proc = run(
        *base_args("--registry", str(workdir / "users.csv"),
                   *(f"--client={c}" for c in CLIENTS)),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    assert REGISTRY_HEADER in text
    for uuid in (UUID_1, UUID_2, UUID_3):
        assert re.search(rf"^{uuid},|,{uuid},", text, re.MULTILINE)
    today = dt.date.today().isoformat()
    assert f"{UUID_1},{today},Android/Happ,МТС,активен" in text
    assert not (workdir / "users.csv").exists()


def test_registry_existing_row_keeps_issue_date_and_gets_revoked(workdir: Path, stub_bin: Path):
    registry = workdir / "users.csv"
    registry.write_text(
        f"{REGISTRY_HEADER}\n"
        f"Иван,{UUID_1},2026-10-01,Android/Happ,МТС,активен,\n"
        f"Пётр,{UUID_2},2026-10-01,iOS/Streisand,Билайн,активен,\n"
        f"Аня,{UUID_3},2026-10-01,macOS/v2rayN,МегаФон,активен,\n",
        encoding="utf-8",
    )
    before = registry.read_text(encoding="utf-8")
    proc = run(
        *base_args("--registry", str(registry),
                   "--client-uuid", UUID_1, "--client-uuid", UUID_3),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    today = dt.date.today().isoformat()
    assert f"Пётр,{UUID_2},2026-10-01,iOS/Streisand,Билайн,отозван,{today}" in text
    assert f"{UUID_1},2026-10-01" in text, "дата выдачи не переписывается"
    assert "Пользователей: 2" in text and len(links(text)) == 2
    assert "Отозваны в этом запуске" in text and UUID_2 in text
    assert registry.read_text(encoding="utf-8") == before, "dry-run не трогает файл реестра"


def test_registry_neutralises_excel_formula(workdir: Path, stub_bin: Path):
    proc = run(
        *base_args("--registry", str(workdir / "users.csv"),
                   f"--client={UUID_1},=1+1,Android/Happ,МТС"),
        stub_bin=stub_bin,
    )
    assert "'=1+1," in out(proc), "имя-формула обязано быть обезврежено"


def test_client_with_too_many_fields_is_rejected(workdir: Path, stub_bin: Path):
    """Запятая внутри поля сдвинула бы всю строку реестра — это ошибка, не догадка."""
    proc = run(
        *base_args("--registry", str(workdir / "users.csv"),
                   f"--client={UUID_1},Иван,Пётр,Android/Happ,МТС"),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode != 0
    assert "полей больше четырёх" in text and ";" in text


def test_formula_name_from_old_registry_is_neutralised(workdir: Path, stub_bin: Path):
    """Реестр мог быть правлен руками: имя-формула обязано быть обезврежено."""
    registry = workdir / "users.csv"
    registry.write_text(
        f"{REGISTRY_HEADER}\n=1+1,{UUID_1},2026-10-01,Android/Happ,МТС,активен,\n",
        encoding="utf-8",
    )
    proc = run(
        *base_args("--registry", str(registry), "--client-uuid", UUID_1),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    assert "'=1+1," in text, "формула из старого реестра обезвреживается при перезаписи"


# ------------------------------------------------------------- применение ----
def apply(workdir: Path, stub_bin: Path, *extra_clients: str, env_extra: dict | None = None):
    return run(
        *apply_args(workdir, *(f"--client={c}" for c in extra_clients)),
        stub_bin=stub_bin,
        env_extra=env_extra,
    )


def test_apply_writes_config_meta_registry_and_reports_active(workdir: Path, stub_bin: Path, tmp_path: Path):
    systemctl_log = tmp_path / "systemctl.log"
    proc = apply(workdir, stub_bin, *CLIENTS,
                 env_extra={"SYSTEMCTL_LOG": str(systemctl_log)})
    text = out(proc)
    assert proc.returncode == 0, text
    assert "Служба xray: active" in text
    assert "Пользователей: 3" in text and len(links(text)) == 3

    cfg = json.loads((workdir / "config.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in cfg["inbounds"][0]["settings"]["clients"]] == [UUID_1, UUID_2, UUID_3]
    assert cfg["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] == BRIDGE_PRIVKEY
    assert cfg["outbounds"][0]["settings"]["vnext"][0]["address"] == EXIT_ADDRESS

    meta = json.loads((workdir / "config.bridge.json").read_text(encoding="utf-8"))
    assert meta["publicKey"] == BRIDGE_PUBKEY and meta["shortId"]
    assert meta["address"] == BRIDGE_ADDRESS

    rows = (workdir / "users.csv").read_text(encoding="utf-8").strip().splitlines()
    assert rows[0] == REGISTRY_HEADER and len(rows) == 4
    assert all(row.endswith("активен,") for row in rows[1:])

    calls = systemctl_log.read_text(encoding="utf-8")
    assert "restart xray" in calls and "is-active xray" in calls
    mode = (workdir / "config.json").stat().st_mode & 0o777
    assert mode == 0o600, f"конфиг с приватным ключом обязан быть 0600, а не {oct(mode)}"


def test_second_apply_keeps_keys_and_does_not_restart(workdir: Path, stub_bin: Path, tmp_path: Path):
    systemctl_log = tmp_path / "systemctl.log"
    env = {"SYSTEMCTL_LOG": str(systemctl_log)}
    first = apply(workdir, stub_bin, *CLIENTS, env_extra=env)
    assert first.returncode == 0, out(first)
    cfg_before = (workdir / "config.json").read_text(encoding="utf-8")
    first_links = links(out(first))
    systemctl_log.write_text("", encoding="utf-8")

    # Повторный запуск без данных реестра: UUID те же, ключи обязаны сохраниться.
    second = run(
        *apply_args(workdir, "--client-uuid", UUID_1, "--client-uuid", UUID_2,
                    "--client-uuid", UUID_3),
        stub_bin=stub_bin, env_extra=env,
    )
    text = out(second)
    assert second.returncode == 0, text
    assert "Конфиг не изменился — перезапуск не нужен" in text
    assert (workdir / "config.json").read_text(encoding="utf-8") == cfg_before
    assert links(text) == first_links, "ссылки на повторном запуске обязаны совпадать"
    assert "restart xray" not in systemctl_log.read_text(encoding="utf-8")
    # Имена подтянулись из реестра: ссылка подписана человеком, а не «client-1».
    assert "Kometa-%D0%98%D0%B2%D0%B0%D0%BD" in text


def test_apply_revocation_removes_only_that_uuid(workdir: Path, stub_bin: Path, tmp_path: Path):
    env = {"SYSTEMCTL_LOG": str(tmp_path / "systemctl.log")}
    assert apply(workdir, stub_bin, *CLIENTS, env_extra=env).returncode == 0
    proc = apply(workdir, stub_bin, CLIENTS[0], CLIENTS[2], env_extra=env)
    text = out(proc)
    assert proc.returncode == 0, text

    cfg = json.loads((workdir / "config.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in cfg["inbounds"][0]["settings"]["clients"]] == [UUID_1, UUID_3]
    assert "Пользователей: 2" in text and len(links(text)) == 2
    assert UUID_2 not in " ".join(links(text)), "отозванный UUID не должен попадать в ссылки"

    rows = (workdir / "users.csv").read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    revoked = [r for r in rows.splitlines() if f",{UUID_2}," in r]
    assert len(revoked) == 1 and revoked[0].endswith(f"отозван,{today}")
    assert any(f",{UUID_1}," in r and r.endswith("активен,") for r in rows.splitlines())
    assert any(f",{UUID_3}," in r and r.endswith("активен,") for r in rows.splitlines())


def test_bad_config_is_not_applied_and_old_config_survives(workdir: Path, stub_bin: Path, tmp_path: Path):
    env = {"SYSTEMCTL_LOG": str(tmp_path / "systemctl.log")}
    assert apply(workdir, stub_bin, *CLIENTS, env_extra=env).returncode == 0
    good = (workdir / "config.json").read_text(encoding="utf-8")

    proc = run(
        *apply_args(workdir, *(f"--client={c}" for c in CLIENTS[:2])),
        stub_bin=stub_bin,
        env_extra={**env, "XRAY_STUB_TEST_FAIL": "1"},
    )
    text = out(proc)
    assert proc.returncode != 0
    assert "не проходит проверку ядром" in text
    assert (workdir / "config.json").read_text(encoding="utf-8") == good, \
        "битый конфиг не должен попасть на мост"


def test_other_core_version_is_refused_until_allowed(workdir: Path, stub_bin: Path, tmp_path: Path):
    env = {"SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"), "XRAY_STUB_VERSION": "26.9.29"}
    proc = apply(workdir, stub_bin, *CLIENTS, env_extra=env)
    text = out(proc)
    assert proc.returncode != 0 and "нужна v26.9.30" in text
    proc2 = run(*apply_args(workdir, "--allow-other-version", *(f"--client={c}" for c in CLIENTS)),
                stub_bin=stub_bin, env_extra=env)
    assert proc2.returncode == 0, out(proc2)


def test_no_install_and_no_core_is_a_clear_error(workdir: Path, tmp_path: Path):
    """Без ядра и без --xray-zip скрипт говорит, что делать, а не падает молча."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    write_stub(empty / "systemctl", SYSTEMCTL_STUB)
    args = [
        "--allow-non-root", "--keep-ipv6", "--no-mss", "--no-install",
        "--config", str(workdir / "config.json"), "--registry", str(workdir / "users.csv"),
        "--bridge-address", BRIDGE_ADDRESS, "--exit-address", EXIT_ADDRESS,
        "--exit-port", EXIT_PORT, "--exit-uuid", EXIT_UUID, "--exit-pubkey", EXIT_PUBKEY,
        "--exit-sni", EXIT_SNI, "--exit-shortid", EXIT_SID, "--client-uuid", UUID_1,
    ]
    proc = run(*args, stub_bin=empty)
    assert proc.returncode != 0
    assert "не найдено" in out(proc) or "Не найдено" in out(proc)


def test_firewall_inactive_is_not_treated_as_active(workdir: Path, stub_bin: Path, tmp_path: Path):
    """«Status: inactive» содержит «active» — на этой подстроке легко ошибиться."""
    ufw_log = tmp_path / "ufw.log"
    proc = apply(workdir, stub_bin, *CLIENTS,
                 env_extra={"SYSTEMCTL_LOG": str(tmp_path / "s.log"),
                            "UFW_LOG": str(ufw_log), "UFW_STATUS": "inactive"})
    assert proc.returncode == 0, out(proc)
    log = ufw_log.read_text(encoding="utf-8")
    assert "status" in log
    assert "allow 443/tcp" not in log, "на выключенном ufw правила не добавляются"


def test_firewall_active_opens_bridge_port(workdir: Path, stub_bin: Path, tmp_path: Path):
    ufw_log = tmp_path / "ufw.log"
    proc = apply(workdir, stub_bin, *CLIENTS,
                 env_extra={"SYSTEMCTL_LOG": str(tmp_path / "s.log"),
                            "UFW_LOG": str(ufw_log), "UFW_STATUS": "active"})
    assert proc.returncode == 0, out(proc)
    assert "allow 443/tcp" in ufw_log.read_text(encoding="utf-8")


# ------------------------------------------------------------- links-only -----
def test_links_only_reprints_from_config_with_meta_address(workdir: Path, stub_bin: Path, tmp_path: Path):
    env = {"SYSTEMCTL_LOG": str(tmp_path / "s.log")}
    assert apply(workdir, stub_bin, *CLIENTS, env_extra=env).returncode == 0
    proc = run("--links-only", "--config", str(workdir / "config.json"),
               "--registry", str(workdir / "users.csv"))
    text = out(proc)
    assert proc.returncode == 0, text
    found = links(text)
    assert len(found) == 3
    assert all(f"@{BRIDGE_ADDRESS}:443" in link for link in found), \
        "адрес берётся из файла-спутника, а не из сети текущей машины"
    assert "Иван" in text


# ------------------------------------- чужой конфиг и занятый порт (реальные грабли) --
FOREIGN_FIRST_CONFIG = {
    # Официальный установщик Xray кладёт свой демо-конфиг: первый инбаунд —
    # не наш. Если читать «inbounds[0]», ключи и SNI возьмутся не оттуда, и
    # повторный запуск молча выпустит новые ключи, порвав розданные ссылки.
    "log": {"loglevel": "warning"},
    "inbounds": [
        {"tag": "socks-in", "port": 1080, "protocol": "socks",
         "settings": {"auth": "noauth", "udp": True}},
        {"tag": "bridge-in", "listen": "0.0.0.0", "port": 443, "protocol": "vless",
         "settings": {"clients": [{"id": UUID_1, "email": "client-1"}], "decryption": "none"},
         "streamSettings": {"network": "tcp", "security": "reality",
                            "realitySettings": {"show": False, "xver": 0,
                                                "target": f"{EXIT_SNI}:443",
                                                "serverNames": ["keep.example.com"],
                                                "privateKey": "KEEPME_PRIVATE_KEY",
                                                "shortIds": ["keepme12"]}}},
    ],
    "outbounds": [{"tag": "freedom", "protocol": "freedom", "settings": {}}],
    "routing": {"rules": []},
}


def test_keys_are_taken_from_bridge_inbound_not_from_first(workdir: Path, stub_bin: Path, tmp_path: Path):
    config = workdir / "config.json"
    config.write_text(json.dumps(FOREIGN_FIRST_CONFIG), encoding="utf-8")
    (workdir / "config.bridge.json").write_text(
        json.dumps({"publicKey": "OLD_PUB", "address": BRIDGE_ADDRESS}), encoding="utf-8")
    env = {"SYSTEMCTL_LOG": str(tmp_path / "s.log")}

    proc = run(
        *apply_args(workdir, "--client-uuid", UUID_1, "--client-uuid", UUID_2),
        stub_bin=stub_bin, env_extra=env,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    cfg = json.loads(config.read_text(encoding="utf-8"))
    ours = [i for i in cfg["inbounds"] if i.get("tag") == "bridge-in"][0]
    reality = ours["streamSettings"]["realitySettings"]
    assert reality["privateKey"] == "KEEPME_PRIVATE_KEY", "ключ обязан переиспользоваться"
    assert reality["shortIds"] == ["keepme12"]
    assert reality["serverNames"] == ["keep.example.com"]
    assert "Приватный ключ моста взят из существующего конфига" in text


def test_links_only_reads_our_inbound_and_meta(workdir: Path, stub_bin: Path):
    config = workdir / "config.json"
    config.write_text(json.dumps(FOREIGN_FIRST_CONFIG), encoding="utf-8")
    (workdir / "config.bridge.json").write_text(
        json.dumps({"publicKey": "OLD_PUB", "address": BRIDGE_ADDRESS}), encoding="utf-8")
    proc = run("--links-only", "--config", str(config))
    text = out(proc)
    assert proc.returncode == 0, text
    found = links(text)
    assert len(found) == 1, "клиент один — и он из bridge-in, а не из socks-инбаунда"
    assert f"vless://{UUID_1}@" in found[0]
    assert "pbk=OLD_PUB" in found[0]
    assert "sid=keepme12" in found[0]
    assert f"sni=keep.example.com" in found[0]


def test_busy_bridge_port_is_reported_before_restart(workdir: Path, stub_bin: Path, tmp_path: Path):
    """443 занят nginx, xray не запущен: сказать об этом ДО перезапуска, а не после."""
    proc = apply(workdir, stub_bin, *CLIENTS,
                 env_extra={"SYSTEMCTL_LOG": str(tmp_path / "s.log"),
                            "SYSTEMCTL_ACTIVE": "inactive",
                            "SS_STUB_OUTPUT": "LISTEN 0 511 0.0.0.0:443 0.0.0.0:*"})
    text = out(proc)
    assert "уже кем-то занят" in text
    assert "0.0.0.0:443" in text


def test_private_bridge_address_is_flagged(workdir: Path, stub_bin: Path):
    """Ссылка с 192.168.x не подключится извне — об этом надо сказать сразу."""
    proc = run(
        *base_args("--bridge-address", "192.168.1.5", "--client-uuid", UUID_1),
        stub_bin=stub_bin,
    )
    text = out(proc)
    assert proc.returncode == 0, text
    assert "приватный" in text


def test_unreadable_existing_config_warns_about_new_keys(workdir: Path, stub_bin: Path, tmp_path: Path):
    """Битый конфиг = ключи не восстановить; молча выпустить новые нельзя."""
    config = workdir / "config.json"
    config.write_text("{ это не JSON", encoding="utf-8")
    proc = run(
        *apply_args(workdir, "--client-uuid", UUID_1),
        stub_bin=stub_bin,
        env_extra={"SYSTEMCTL_LOG": str(tmp_path / "s.log")},
    )
    text = out(proc)
    assert "не читается как JSON" in text
    assert "НОВЫЕ ключи" in text
