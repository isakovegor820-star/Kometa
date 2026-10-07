"""Тесты безопасной правки .env (scripts/configure_env.py).

Смысл: на сервере `.env` правится один раз, руками и почти всегда с опечаткой.
Инструмент должен либо записать ровно переданное значение, либо отказаться —
поэтому проверяем и замену, и добавление, и отказ, и резервную копию.
"""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("configure_env", ROOT / "scripts" / "configure_env.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


configure_env = _load_module()


# ---------------------------------------------------------------- разбор входа
def test_parse_updates_ok():
    assert configure_env.parse_updates(["A=1", "B=два слова"]) == [("A", "1"), ("B", "два слова")]


def test_parse_updates_trims_spaces():
    assert configure_env.parse_updates(["  KEY = value  "]) == [("KEY", "value")]


def test_parse_updates_keeps_empty_value():
    """Пустое значение — это норма (например PUBLIC_BASE_URL=), а не ошибка."""
    assert configure_env.parse_updates(["PANEL_URL="]) == [("PANEL_URL", "")]


@pytest.mark.parametrize("bad", ["JUSTKEY", "1BAD=x", "BAD-KEY=x", "ключ=x"])
def test_parse_updates_rejects_bad_input(bad):
    with pytest.raises(configure_env.EnvError):
        configure_env.parse_updates([bad])


def test_parse_updates_rejects_multiline_value():
    with pytest.raises(configure_env.EnvError):
        configure_env.parse_updates(["KEY=a\nb"])


# ---------------------------------------------------------------- чтение
def test_get_value_returns_last_occurrence():
    text = "PANEL_URL=http://old\nPANEL_URL=http://new\n"
    assert configure_env.get_value(text, "PANEL_URL") == "http://new"


def test_get_value_missing_returns_none():
    assert configure_env.get_value("A=1\n", "PANEL_URL") is None


# ---------------------------------------------------------------- правка текста
def test_update_env_replaces_value_and_keeps_rest():
    text = "# шапка\nBOT_TOKEN=abc\nPANEL_URL=\nWEB_PORT=8090\n"
    updated, changes = configure_env.update_env(text, [("PANEL_URL", "http://1.2.3.4:54321/xyz")])

    assert "PANEL_URL=http://1.2.3.4:54321/xyz" in updated
    assert "# шапка" in updated
    assert "BOT_TOKEN=abc" in updated
    assert "WEB_PORT=8090" in updated
    assert changes == ["~ PANEL_URL= → PANEL_URL=http://1.2.3.4:54321/xyz"]


def test_update_env_appends_missing_key():
    updated, changes = configure_env.update_env("A=1\n", [("PANEL_TYPE", "xui")])

    assert updated == "A=1\nPANEL_TYPE=xui\n"
    assert changes == ["+ PANEL_TYPE=xui"]


def test_update_env_collapses_duplicates():
    """Два одинаковых ключа — источник неожиданностей: оставляем один (последнее значение)."""
    updated, _ = configure_env.update_env("A=1\nA=2\n", [("A", "3")])

    assert updated == "A=3\n"


def test_update_env_reports_unchanged_value():
    updated, changes = configure_env.update_env("A=1\n", [("A", "1")])

    assert updated == "A=1\n"
    assert changes == ["= A=1"]


def test_update_env_keeps_special_characters():
    """URL с query и значение с решёткой/пробелами пишутся как есть, без кавычек."""
    value = "http://1.2.3.4:54321/sub/?a=1#x"
    updated, _ = configure_env.update_env("PANEL_URL=\n", [("PANEL_URL", value)])
    assert updated == f"PANEL_URL={value}\n"


def test_update_env_preserves_no_trailing_newline_style():
    updated, _ = configure_env.update_env("A=1", [("B", "2")])
    assert updated == "A=1\nB=2"


def test_update_env_updates_several_keys_at_once():
    text = "PANEL_TYPE=fake\nPANEL_URL=\n"
    updated, changes = configure_env.update_env(
        text, [("PANEL_TYPE", "xui"), ("PANEL_URL", "http://ip:54321/p")]
    )

    assert updated == "PANEL_TYPE=xui\nPANEL_URL=http://ip:54321/p\n"
    assert len(changes) == 2


# ---------------------------------------------------------------- запись файла
def test_write_env_creates_backup_with_old_content(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PANEL_TYPE=fake\n", encoding="utf-8")

    changes, backup = configure_env.write_env(env, [("PANEL_TYPE", "xui")])

    assert changes == ["~ PANEL_TYPE=fake → PANEL_TYPE=xui"]
    assert backup is not None and backup.exists()
    assert backup.read_text(encoding="utf-8") == "PANEL_TYPE=fake\n"
    assert env.read_text(encoding="utf-8") == "PANEL_TYPE=xui\n"


def test_write_env_dry_run_does_not_touch_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PANEL_TYPE=fake\n", encoding="utf-8")

    changes, backup = configure_env.write_env(env, [("PANEL_TYPE", "xui")], dry_run=True)

    assert changes and backup is None
    assert env.read_text(encoding="utf-8") == "PANEL_TYPE=fake\n"
    assert list(tmp_path.iterdir()) == [env]


def test_write_env_no_changes_no_backup(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PANEL_TYPE=xui\n", encoding="utf-8")

    _, backup = configure_env.write_env(env, [("PANEL_TYPE", "xui")])

    assert backup is None
    assert list(tmp_path.iterdir()) == [env]


def test_write_env_new_file_is_private(tmp_path):
    """Новый .env создаётся с правами 600: внутри секреты."""
    env = tmp_path / ".env"

    configure_env.write_env(env, [("BOT_TOKEN", "secret")])

    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------- CLI
def test_cli_get_and_set(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("WEB_PORT=8090\n", encoding="utf-8")

    assert configure_env.main(["--env", str(env), "--get", "WEB_PORT"]) == 0
    assert capsys.readouterr().out.strip() == "8090"

    assert configure_env.main(["--env", str(env), "--set", "PANEL_TYPE=xui"]) == 0
    assert "PANEL_TYPE=xui" in env.read_text(encoding="utf-8")


def test_cli_get_missing_key_returns_error(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")

    assert configure_env.main(["--env", str(env), "--get", "PANEL_URL"]) == 1
    assert "не найдена" in capsys.readouterr().err


def test_cli_rejects_bad_assignment(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")

    assert configure_env.main(["--env", str(env), "--set", "ПЛОХОЙ=x"]) == 2
    assert "Ошибка" in capsys.readouterr().err
    assert env.read_text(encoding="utf-8") == "A=1\n"


def test_cli_dry_run_marks_output(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("PANEL_TYPE=fake\n", encoding="utf-8")

    assert configure_env.main(["--env", str(env), "--set", "PANEL_TYPE=xui", "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert env.read_text(encoding="utf-8") == "PANEL_TYPE=fake\n"
