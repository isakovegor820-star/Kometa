"""Тесты скрипта серверной части DNS-туннеля dnstt (без сети и без сервера).

Зачем тест, а не памятка. ``scripts/add_dns_tunnel.sh`` разворачивает на VPS
сервер dnstt: ставит бинарник, создаёт ключи, кладёт systemd-юнит и заводит
трафик с UDP/53 на внутренний порт. Ошибка здесь стоит дороже всего в проекте:

  * ``--apply`` без ключей и без бэкапа = повторная генерация ключей, после
    которой уже розданный клиентам ``server.pub`` перестаёт подходить и все
    клиенты отваливаются. Поэтому идемпотентность проверяется и по тексту, и
    живьём: «ключи и юнит на месте» → выход 0 без перегенерации;
  * «DRY-RUN», который на самом деле ходит в сеть (curl за бинарником) или
    пишет в систему (ключи, юнит, iptables), — прямой путь испортить боевой
    сервер. Отсюда проверка с подменёнными ``curl`` и ``dig``: любой сетевой
    вызов оставил бы файл-маркер;
  * порт 53 занят systemd-resolved, поэтому трафик заводится правилом
    iptables NAT PREROUTING REDIRECT, а сервис слушает непривилегированный
    порт. Юнит без capabilities (``CapabilityBoundingSet=``) привилегированный
    порт занять не сможет — это тоже проверяется статически;
  * WhiteDNS (движки StormDNS/CottenDns) с dnstt несовместим: клиент обязан
    узнать это из вывода скрипта, а не из тикета «не подключается».

Проверяем статически и прогоном (машина разработчика — macOS, сервера и
systemd здесь нет): ``bash -n``, ``--help``, dry-run с шимами curl/dig и
grep по тексту скрипта. Ни один тест не поднимает сервер и не ходит в сеть.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "add_dns_tunnel.sh"

BASH = shutil.which("bash") or "/bin/bash"

DOMAIN = "t.example.com"
NS_HOST = "tns.example.com"
SERVER_IP = "203.0.113.7"

# Аргументы, без которых скрипт не строит план.
BASE_ARGS = ["--domain", DOMAIN, "--ns-host", NS_HOST, "--server-ip", SERVER_IP]

# Переменные, которые могут прилететь из окружения разработчика и подменить
# параметры прогона: их всегда вычищаем перед запуском.
SCRIPT_ENV_VARS = (
    "DOMAIN", "NS_HOST", "SERVER_IP", "MODE", "MTU", "PORT", "SSH_PORT",
    "BINARY", "SHA256", "KEY_DIR", "SERVICE_USER", "SERVICE_NAME", "UNIT_DIR",
    "BIN_DIR", "IFACE", "DNSTT_BASE_URL",
    "DIG_LOG", "FAKE_NS", "CURL_LOG",
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


@pytest.fixture
def paths(tmp_path: Path) -> dict:
    """Каталоги-песочницы: ни ключей, ни юнита, ни бинарника там нет.

    Так проверяется, что dry-run не создаёт файлов: пути передаются скрипту
    явно (``--key-dir``, ``UNIT_DIR``, ``BIN_DIR``), и после прогона их быть
    не должно.
    """
    return {
        "key_dir": tmp_path / "etc" / "dnstt",
        "unit_dir": tmp_path / "units",
        "bin_dir": tmp_path / "usr-local-bin",
    }


def plan_args(paths: dict, *extra: str) -> list[str]:
    return [*BASE_ARGS, "--key-dir", str(paths["key_dir"]), *extra]


def plan_env(paths: dict, **overrides: str) -> dict:
    return clean_env(
        UNIT_DIR=str(paths["unit_dir"]),
        BIN_DIR=str(paths["bin_dir"]),
        **overrides,
    )


@pytest.fixture
def net_shims(tmp_path: Path) -> dict:
    """Подменённые curl и dig: только пишут маркер в файл и падают с кодом 7.

    Настоящий curl к несуществующему хосту тоже упал бы, поэтому «маркера нет»
    само по себе ничего не доказывает — отсюда отдельный контрольный тест,
    который дёргает шим напрямую и убеждается, что маркер появляется.
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    marker = tmp_path / "network-called"
    for name in ("curl", "dig"):
        shim = shim_dir / name
        shim.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "{name} $*" >> "{marker}"\n'
            "exit 7\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return {"dir": shim_dir, "marker": marker, "tmp": tmp_path}


@pytest.fixture
def dig_shim(tmp_path: Path) -> dict:
    """Подменённый dig, отвечающий как настроенное делегирование.

    FAKE_NS=good — NS зоны указывает на наш сервер имён (проверка проходит);
    FAKE_NS=bad  — NS указывает на чужое имя (проверка обязана упасть).
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    log = tmp_path / "dig.log"
    shim = shim_dir / "dig"
    shim.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DIG_LOG"\n'
        'case "$*" in\n'
        '  *"NS t.example.com"*)\n'
        '      if [ "${FAKE_NS:-good}" = "good" ]; then printf "tns.example.com.\\n";'
        ' else printf "other.example.net.\\n"; fi ;;\n'
        '  *"A tns.example.com"*) printf "203.0.113.7\\n" ;;\n'
        '  *"@203.0.113.7"*) printf "tns.example.com.\\n" ;;\n'
        'esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return {"dir": shim_dir, "log": log, "tmp": tmp_path}


@pytest.fixture(scope="module")
def path_without_dig(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """PATH, в котором есть всё, кроме dig: эмуляция «dig не установлен».

    Симлинкуем содержимое всех каталогов PATH (tr, sed, sort и прочее скрипту
    нужны), а dig пропускаем — иначе подсказку про bind9-dnsutils не проверить.
    """
    target = tmp_path_factory.mktemp("nodig")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isdir(entry):
            continue
        for name in os.listdir(entry):
            if name == "dig":
                continue
            src = Path(entry) / name
            dst = target / name
            if dst.is_symlink() or dst.exists():
                continue
            if not src.is_file() or not os.access(src, os.X_OK):
                continue
            try:
                dst.symlink_to(src)
            except OSError:
                continue
    return target


def dig_env(dig_shim: dict, paths: dict, **overrides: str) -> dict:
    env = plan_env(paths, DIG_LOG=str(dig_shim["log"]), **overrides)
    env["PATH"] = f"{dig_shim['dir']}{os.pathsep}{env.get('PATH', '')}"
    return env


# --- 1. Синтаксис и базовые требования ------------------------------------


def test_bash_syntax_ok():
    """`bash -n` — скрипт запускают на VPS, где нет ни линтера, ни pytest."""
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_set_euo_pipefail(script_text: str):
    """`set -euo pipefail` обязателен: иначе ошибка curl/systemctl уходит в тишину."""
    assert re.search(r"^set -euo pipefail$", script_text, re.M)


def test_dependencies_are_limited_to_the_documented_set(script_text: str):
    """Зависимости: sha256sum, systemctl, iptables, curl (с --apply), dig, getent.

    ip6tables идёт из того же пакета iptables и проверяется отдельно (может
    отсутствовать). python/pip не вызываются вообще: скрипт должен работать на
    голом Ubuntu/Debian.
    """
    used = set(re.findall(r"(?:^|[\s;|(])have ([A-Za-z0-9_.-]+)", script_text, re.M))
    allowed = {"sha256sum", "systemctl", "iptables", "ip6tables", "curl", "dig", "getent"}
    assert used <= allowed, f"лишние зависимости: {sorted(used - allowed)}"
    assert {"sha256sum", "systemctl", "iptables", "getent", "curl", "dig"} <= used

    # python/pip не вызываются (упоминание «без python/pip» в шапке — не вызов).
    assert not re.search(r"^\s*(python3?|pip3?)\b", script_text, re.M)
    assert not re.search(r"(pip3?|python3?)\s+install", script_text)

    # curl нужен только на ветке --apply без --binary.
    assert re.search(r'have curl \|\| die "Нужен curl для --apply', script_text)
    assert script_text.count("require_curl") == 2, "одно определение и один вызов"
    # Скачивание — одно место (download_binary), и в плане та же команда лишь печатается.
    assert script_text.count("download_binary") == 2, "одно определение и один вызов"
    assert script_text.count("curl -fsSL") == 2, "один реальный вызов и одна печать в плане"


# --- 2. --help ------------------------------------------------------------


def test_help_exits_zero_without_required_args():
    """--help работает без обязательных аргументов: это первое, что читают."""
    result = run("--help", env=clean_env())
    assert result.returncode == 0, output(result)
    text = output(result)
    flags = [
        "--domain", "--ns-host", "--server-ip", "--mode", "--mtu", "--port",
        "--ssh-port", "--binary", "--sha256", "--key-dir", "--user",
        "--service-name", "--check-dns", "--apply", "--dry-run", "--force",
        "-h, --help",
    ]
    missing = [flag for flag in flags if flag not in text]
    assert not missing, f"в --help нет флагов: {missing}"
    assert "DRY-RUN" in text and "--apply" in text
    assert "bind9-dnsutils" in text, "в справке должна быть подсказка про пакет с dig"
    assert "WhiteDNS" in text and "StormDNS" in text, "несовместимость видна ещё из справки"
    assert "dnstt-server" in text and "dnstt-client" in text
    assert "sha256" in text.lower()


# --- 3. Обязательные параметры и валидация --------------------------------


def test_missing_domain_fails_clearly():
    """Без --domain нечего делегировать: ошибка называет флаг и объясняет зачем."""
    result = run("--ns-host", NS_HOST, "--server-ip", SERVER_IP, env=clean_env())
    assert result.returncode != 0
    text = output(result)
    assert "--domain" in text and "Обязателен" in text, text


def test_missing_ns_host_fails_clearly():
    result = run("--domain", DOMAIN, "--server-ip", SERVER_IP, env=clean_env())
    assert result.returncode != 0
    text = output(result)
    assert "--ns-host" in text and "Обязателен" in text, text


def test_missing_server_ip_fails_clearly():
    """--server-ip нужен для проверки делегирования (A-запись сервера имён)."""
    result = run("--domain", DOMAIN, "--ns-host", NS_HOST, env=clean_env())
    assert result.returncode != 0
    text = output(result)
    assert "--server-ip" in text and "Обязателен" in text, text


def test_invalid_mode_is_rejected():
    """Режим — это либо форвард в sshd, либо Dante SOCKS5; третьего нет."""
    result = run(
        *BASE_ARGS, "--mode", "quic",
        env=clean_env(UNIT_DIR="/nonexistent-kometa-units"),
    )
    assert result.returncode != 0
    text = output(result)
    assert "--mode" in text and "quic" in text, text


@pytest.mark.parametrize("mtu", ["100", "511", "1401", "9999", "abc", ""])
def test_mtu_outside_range_is_rejected(mtu: str):
    """512–1400 — диапазон из первоисточника; вне него скрипт обязан отказать."""
    result = run(
        *BASE_ARGS, "--mtu", mtu,
        env=clean_env(UNIT_DIR="/nonexistent-kometa-units"),
    )
    assert result.returncode != 0
    text = output(result)
    assert "--mtu" in text, text


def test_ns_host_inside_tunnel_zone_is_rejected():
    """Вся зона туннеля отдана под нагрузку — сервер имён не может быть внутри."""
    result = run(
        "--domain", DOMAIN, "--ns-host", f"ns.{DOMAIN}", "--server-ip", SERVER_IP,
        env=clean_env(UNIT_DIR="/nonexistent-kometa-units"),
    )
    assert result.returncode != 0
    text = output(result)
    assert "--ns-host" in text and "--domain" in text, text
    assert f"ns.{DOMAIN}" in text


def test_sha256_is_validated_and_shown():
    """Сумма — 64 hex; мусор отвергается, корректная попадает в план."""
    bad = run(
        *BASE_ARGS, "--sha256", "zzz",
        env=clean_env(UNIT_DIR="/nonexistent-kometa-units"),
    )
    assert bad.returncode != 0
    assert "--sha256" in output(bad)

    digest = "a" * 64
    good = run(
        *BASE_ARGS, "--sha256", digest,
        env=clean_env(UNIT_DIR="/nonexistent-kometa-units",
                      KEY_DIR="/nonexistent-kometa-keys"),
    )
    text = output(good)
    assert good.returncode == 0, text
    assert digest in text
    assert "сумма не проверена" not in text, "сумма задана — предупреждения быть не должно"


# --- 4. Dry-run: план есть, сети и записей нет ----------------------------


def test_network_shims_are_real(net_shims: dict, paths: dict):
    """Контроль: подменённые curl и dig действительно вызываются и пишут маркер.

    Без этой проверки «маркера нет» означало бы «шимы не работают», и тест про
    отсутствие сети был бы зелёным всегда.
    """
    env = plan_env(paths)
    env["PATH"] = f"{net_shims['dir']}{os.pathsep}{env.get('PATH', '')}"
    for name in ("curl", "dig"):
        probe = subprocess.run(
            [name, "-sS", "https://example.invalid/"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert probe.returncode == 7, f"шим {name} не сработал"
    assert net_shims["marker"].exists(), "шимы не сработали — тесты на сеть были бы слепыми"
    written = net_shims["marker"].read_text(encoding="utf-8")
    assert "curl " in written and "dig " in written


def test_dry_run_prints_plan_and_never_touches_network_or_disk(paths: dict, net_shims: dict):
    """Главная гарантия: по умолчанию — только план, без сети и без записи."""
    result = run(*plan_args(paths), env=plan_env(paths), path_prefix=net_shims["dir"])
    text = output(result)
    assert result.returncode == 0, text
    assert "DRY-RUN" in text
    assert "Сетевых запросов нет" in text, "нужен явный маркер отсутствия сети"
    assert not net_shims["marker"].exists(), (
        f"скрипт всё-таки позвал сеть: {net_shims['marker'].read_text(encoding='utf-8')}"
    )
    assert not paths["key_dir"].exists(), "в dry-run ключи не создаются"
    assert not paths["unit_dir"].exists(), "в dry-run юнит не кладётся"
    assert not paths["bin_dir"].exists(), "в dry-run бинарник не ставится"


def test_explicit_dry_run_behaves_like_default(paths: dict, net_shims: dict):
    """Явный --dry-run — то же поведение, что и по умолчанию (никаких сюрпризов)."""
    result = run(*plan_args(paths, "--dry-run"), env=plan_env(paths),
                 path_prefix=net_shims["dir"])
    text = output(result)
    assert result.returncode == 0, text
    assert "DRY-RUN" in text
    assert not net_shims["marker"].exists()
    assert not paths["key_dir"].exists() and not paths["unit_dir"].exists()


def test_dry_run_prints_server_deployment_plan(paths: dict):
    """В плане видно всё, что скрипт сделает с --apply: бинарник, ключи, юнит, NAT."""
    result = run(*plan_args(paths), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text

    # 1. серверный бинарник и план скачивания;
    assert "dnstt-server" in text
    assert "dnstt.network/dnstt-server-linux" in text
    assert "SHA256SUMS" in text
    assert "сумма не проверена" in text, "без --sha256 обязано быть предупреждение"
    # 2. генерация ключей и путь публичного ключа;
    assert "-gen-key" in text
    assert "-privkey-file" in text and "-pubkey-file" in text
    assert "server.pub" in text and "server.key" in text
    assert str(paths["key_dir"]) in text
    # 3. systemd-юнит целиком, с ужесточением и рестартом;
    assert "юнит" in text.lower()
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert "ExecStart=" in text and "WantedBy=multi-user.target" in text
    assert "Restart=always" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=true" in text
    assert "User=dnstt" in text, "сервис работает отдельным пользователем"
    assert re.search(r"ExecStart=.*-udp :5300", text), "слушаем внутренний порт, а не 53"
    # 4. заведение трафика с 53 правилом NAT;
    assert "PREROUTING" in text
    assert "--dport 53" in text
    assert "--to-ports 5300" in text
    assert "REDIRECT" in text
    assert "iptables" in text


def test_dry_run_prints_client_command_and_mobile_apps(paths: dict):
    """Клиентская часть: команда dnstt-client, мобильные приложения и MTU 512."""
    result = run(*plan_args(paths), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert "dnstt-client" in text
    assert f"-udp {SERVER_IP}:53" in text
    assert DOMAIN in text
    assert "HTTP Injector" in text
    assert "HTTP Custom" in text or "DarkTunnel" in text
    assert "--mtu 512" in text, "снижение MTU для мобильных сетей — обязательная подсказка"
    assert "requester payload size" in text, "ошибка MTU названа так, как её видит оператор"


def test_socks_mode_targets_dante_on_1080(paths: dict):
    """--mode socks — форвард в Dante SOCKS5 на 127.0.0.1:1080, а не в sshd."""
    result = run(*plan_args(paths, "--mode", "socks"), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert re.search(r"ExecStart=.*127\.0\.0\.1:1080", text), "форвард в Dante"
    assert "Dante" in text and "SOCKS5" in text
    assert "dante-server" in text, "честно: Dante скрипт не ставит, только требует"

    ssh_run = run(*plan_args(paths), env=plan_env(paths))
    assert re.search(r"ExecStart=.*127\.0\.0\.1:22", output(ssh_run)), "по умолчанию sshd"


def test_whitedns_incompatibility_is_printed_last(paths: dict):
    """WhiteDNS/StormDNS/CottenDns несовместимы — об этом в самом конце вывода."""
    result = run(*plan_args(paths), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert "WhiteDNS" in text and "StormDNS" in text and "CottenDns" in text
    assert "НЕ СОВМЕСТИМЫ" in text or "не совместим" in text.lower()
    assert "HTTP Injector" in text, "альтернатива названа: SSH-приложения"
    assert text.index("КАК ПРОВЕРИТЬ") < text.index("WhiteDNS"), (
        "предупреждение о несовместимости должно идти последним блоком"
    )


def test_how_to_check_block_is_actionable(paths: dict):
    """«КАК ПРОВЕРИТЬ» — с конкретными командами, а не «проверьте работу»."""
    result = run(*plan_args(paths), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert "КАК ПРОВЕРИТЬ" in text
    assert "systemctl status dnstt-server" in text
    assert "journalctl -u dnstt-server" in text
    assert "ss -ulnp" in text and "grep 5300" in text
    assert f"dig @{SERVER_IP}" in text
    assert f"dig +short NS {DOMAIN}" in text
    assert "MTU 512" in text or "--mtu 512" in text, "проверка с телефона — через MTU"


def test_binary_flag_skips_download(paths: dict, tmp_path: Path):
    """--binary: готовый бинарник — скачивание не планируется вообще."""
    ready = tmp_path / "dnstt-server"
    ready.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    result = run(*plan_args(paths, "--binary", str(ready)), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert str(ready) in text
    assert "Скачивание не нужно" in text
    assert "dnstt-server-linux-amd64" not in text and "dnstt-server-linux-arm64" not in text

    missing = tmp_path / "nope-dnstt-server"
    warn_run = run(*plan_args(paths, "--binary", str(missing)), env=plan_env(paths))
    warn_text = output(warn_run)
    assert warn_run.returncode == 0, warn_text
    assert str(missing) in warn_text
    assert "не найден" in warn_text or "файла пока нет" in warn_text


# --- 5. Идемпотентность ---------------------------------------------------


def test_idempotency_is_built_into_the_text(script_text: str):
    """В скрипте есть проверка ключей и юнита и явная формулировка «не перегенерирую»."""
    assert "не перегенерирую" in script_text
    assert "уже настроено" in script_text.lower()
    assert "is_configured" in script_text
    assert "keys_present" in script_text and "unit_present" in script_text
    assert "server.key" in script_text and "server.pub" in script_text
    assert "--force" in script_text, "перегенерация — только явным --force"
    assert "backup-" in script_text, "старые ключи перед перегенерацией — в бэкап"


def test_idempotency_stops_second_run(paths: dict):
    """Ключи и юнит уже есть → выход 0 с «не перегенерирую», файлы не тронуты."""
    paths["key_dir"].mkdir(parents=True)
    (paths["key_dir"] / "server.key").write_text("PRIVATE-KEY", encoding="utf-8")
    (paths["key_dir"] / "server.pub").write_text("PUBLIC-KEY", encoding="utf-8")
    paths["unit_dir"].mkdir(parents=True)
    (paths["unit_dir"] / "dnstt-server.service").write_text("[Unit]\n", encoding="utf-8")

    result = run(*plan_args(paths), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert "не перегенерирую" in text
    assert "УЖЕ НАСТРОЕНО" in text
    assert "КАК ПРОВЕРИТЬ" in text, "проверки печатаются и в этом режиме"
    assert "WhiteDNS" in text

    assert (paths["key_dir"] / "server.key").read_text(encoding="utf-8") == "PRIVATE-KEY"
    assert (paths["key_dir"] / "server.pub").read_text(encoding="utf-8") == "PUBLIC-KEY"


def test_partial_state_still_prints_plan(paths: dict):
    """Ключи есть, юнита нет — это не «настроено»: план печатается."""
    paths["key_dir"].mkdir(parents=True)
    (paths["key_dir"] / "server.key").write_text("PRIVATE-KEY", encoding="utf-8")
    (paths["key_dir"] / "server.pub").write_text("PUBLIC-KEY", encoding="utf-8")

    result = run(*plan_args(paths), env=plan_env(paths))
    text = output(result)
    assert result.returncode == 0, text
    assert "DRY-RUN" in text, "частичное состояние не должно глушить план"
    assert "УЖЕ НАСТРОЕНО" not in text


# --- 6. Проверка делегирования --------------------------------------------


def test_check_dns_without_dig_prints_hint(paths: dict, path_without_dig: Path):
    """Нет dig — внятная подсказка про bind9-dnsutils, а не трейсбек и не тишина."""
    env = clean_env(
        UNIT_DIR=str(paths["unit_dir"]),
        BIN_DIR=str(paths["bin_dir"]),
        PATH=str(path_without_dig),
    )
    result = run(*plan_args(paths, "--check-dns"), env=env)
    text = output(result)
    assert result.returncode != 0, "без dig проверка не выполнена — это не успех"
    assert "dig не найден" in text
    assert "bind9-dnsutils" in text, "подсказка должна называть пакет"
    assert "apt install" in text
    assert "Traceback" not in text, "это bash-скрипт: трейсбеков быть не может"
    assert not paths["key_dir"].exists() and not paths["unit_dir"].exists()


def test_check_dns_reports_success_with_shim(dig_shim: dict, paths: dict):
    """--check-dns: NS и A сходятся — проверка проходит и выходит 0."""
    result = run(*plan_args(paths, "--check-dns"), env=dig_env(dig_shim, paths))
    text = output(result)
    assert result.returncode == 0, text
    assert f"dig +short NS {DOMAIN}" in text or "Запрос 1/3" in text
    assert "Делегирование выглядит настроенным" in text
    calls = dig_shim["log"].read_text(encoding="utf-8")
    assert f"+short NS {DOMAIN}" in calls
    assert f"+short A {NS_HOST}" in calls
    assert not paths["key_dir"].exists(), "--check-dns ничего не создаёт"


def test_check_dns_detects_wrong_ns(dig_shim: dict, paths: dict):
    """--check-dns: NS указывает на чужое имя — выход не 0 и внятное объяснение."""
    result = run(
        *plan_args(paths, "--check-dns"),
        env=dig_env(dig_shim, paths, FAKE_NS="bad"),
    )
    text = output(result)
    assert result.returncode != 0, text
    assert "other.example.net" in text
    assert "Делегирование проверить не удалось" in text
    assert "до 24 часов" in text or "распростран" in text


# --- 7. --apply на машине без root и без systemd --------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="тест про отсутствие root")
def test_apply_without_root_stops_before_writing(paths: dict):
    """--apply без root обязан остановиться до любых записей (тут и проверяем)."""
    result = run(*plan_args(paths, "--apply"), env=plan_env(paths))
    text = output(result)
    assert result.returncode != 0
    assert "root" in text, text
    assert not paths["key_dir"].exists(), "ничего не создано"
    assert not paths["unit_dir"].exists(), "юнит не положен"
    assert not paths["bin_dir"].exists(), "бинарник не скопирован"
