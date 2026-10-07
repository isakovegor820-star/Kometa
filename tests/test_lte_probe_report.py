"""Отчёт замеров LTE: --report копит строки CSV, чтобы сводку не собирать руками.

Замеров много (оператор × город × адрес), и глазами их не свести. Тесты
проверяют, что строки появляются там, где нужно, вердикт соответствует
картине ответов, а без --report скрипт по-прежнему ничего не пишет.

Сети нет: curl подменён шимом, который отдаёт заданные rc.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "lte_probe.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return PROBE.read_text(encoding="utf-8")


def make_shim(tmp_path: Path, *, rst: tuple[str, ...] = (), ok: tuple[str, ...] = ()) -> Path:
    """curl-шим: контроль отвечает, заданные хосты дают rc=7 или rc=0, прочие rc=28."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "curl"
    body = ["#!/bin/sh", 'case "$*" in', "  *ifconfig.co*) exit 0 ;;", "esac"]
    body += [f'case "$*" in *"{host}"*) exit 7 ;; esac' for host in rst]
    body += [f'case "$*" in *"{host}"*) exit 0 ;; esac' for host in ok]
    body.append("exit 28")
    shim.write_text("\n".join(body) + "\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir


def run_probe(tmp_path: Path, shim_dir: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    environ = dict(os.environ)
    environ["PATH"] = f"{shim_dir}:{environ['PATH']}"
    if env:
        environ.update(env)
    return subprocess.run(
        ["bash", str(PROBE), *args, "--timeout", "1"],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )


def read_report(tmp_path: Path) -> list[str]:
    report = tmp_path / "probes.csv"
    return report.read_text(encoding="utf-8").strip().splitlines()


# ---------------------------------------------------------------- база
def test_syntax_ok():
    assert subprocess.run(["bash", "-n", str(PROBE)], capture_output=True).returncode == 0


def test_help_mentions_report(script: str):
    assert "--report" in script
    assert "--label" in script


def test_report_header_is_csv_with_expected_columns(script: str):
    assert "время,метка,оператор,регион,режим,цель,группа,вердикт,детали" in script


def test_without_report_nothing_is_written(tmp_path):
    shim = make_shim(tmp_path, rst=("10.1.1.1",))
    result = run_probe(tmp_path, shim, "--host", "10.1.1.1", "--ports", "443")

    assert result.returncode == 0
    assert not list(tmp_path.glob("*.csv")), "без --report файлов быть не должно"


# ---------------------------------------------------------------- режимы
def test_full_run_writes_row_with_verdict(tmp_path):
    shim = make_shim(tmp_path, rst=("10.2.2.2",))
    result = run_probe(
        tmp_path,
        shim,
        "--host", "10.2.2.2",
        "--ports", "443",
        "--report", "probes.csv",
        "--label", "МТС Новосибирск",
        env={"LTE_OPERATOR": "MTS", "LTE_REGION": "NSK"},
    )

    assert result.returncode == 0
    lines = read_report(tmp_path)
    assert lines[0].startswith("время,метка,оператор,регион,режим")
    assert len(lines) == 2

    row = lines[1].split(",")
    assert row[1] == "МТС Новосибирск"
    assert row[2] == "MTS"
    assert row[3] == "NSK"
    assert row[4] == "full"
    assert row[5] == "10.2.2.2"
    assert row[7] in {"ПРОХОДИТ", "ЧАСТИЧНО", "НЕ ПРОХОДИТ"}
    assert "fail=" in row[8]


def test_deep_run_reports_block_on_rst(tmp_path):
    shim = make_shim(tmp_path, rst=("10.3.3.3",))
    result = run_probe(
        tmp_path,
        shim,
        "--deep", "--host", "10.3.3.3", "--deep-ports", "443", "--repeat", "2",
        "--report", "probes.csv",
    )

    assert result.returncode == 0
    row = read_report(tmp_path)[1].split(",")
    assert row[4] == "deep"
    assert row[5] == "10.3.3.3:443"
    assert row[7] == "БЛОК"
    assert "rst=2" in row[8]


def test_deep_run_reports_pass_on_tls(tmp_path):
    shim = make_shim(tmp_path, ok=("10.4.4.4",))
    run_probe(
        tmp_path,
        shim,
        "--deep", "--host", "10.4.4.4", "--deep-ports", "443", "--repeat", "2",
        "--report", "probes.csv",
    )

    row = read_report(tmp_path)[1].split(",")
    assert row[7] == "ПРОХОДИТ"
    assert "tls=2" in row[8]


def test_range_reports_row_per_address_with_group(tmp_path):
    shim = make_shim(tmp_path, rst=("10.5.5.",))
    run_probe(
        tmp_path,
        shim,
        "--check-range", "10.5.5.0/24", "--repeat", "1",
        "--report", "probes.csv",
    )

    lines = read_report(tmp_path)
    # Заголовок + три пустых адреса подсети (.1, .77, .254).
    assert len(lines) == 4
    rows = [line.split(",") for line in lines[1:]]
    assert {row[5] for row in rows} == {"10.5.5.1", "10.5.5.77", "10.5.5.254"}
    assert all(row[6] == "10.5.5.0/24" for row in rows)
    assert all(row[7] == "БЛОК" for row in rows)


def test_report_appends_instead_of_overwriting(tmp_path):
    shim = make_shim(tmp_path, rst=("10.6.6.6",))
    for _ in range(2):
        run_probe(
            tmp_path,
            shim,
            "--deep", "--host", "10.6.6.6", "--deep-ports", "443", "--repeat", "1",
            "--report", "probes.csv",
        )

    lines = read_report(tmp_path)
    assert len(lines) == 3, "второй замер дописывается, заголовок не дублируется"


def test_commas_in_label_do_not_break_csv(tmp_path):
    shim = make_shim(tmp_path, rst=("10.7.7.7",))
    run_probe(
        tmp_path,
        shim,
        "--deep", "--host", "10.7.7.7", "--deep-ports", "443", "--repeat", "1",
        "--report", "probes.csv", "--label", "МТС, центр",
    )

    row = read_report(tmp_path)[1].split(",")
    assert row[1] == "МТС; центр", "запятые внутри поля заменяются, чтобы CSV не разъезжался"
    assert len(row) == 9
