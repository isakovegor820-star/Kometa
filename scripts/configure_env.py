#!/usr/bin/env python3
"""Безопасная правка ``.env``: заменить или добавить переменные, не ломая файл.

Зачем отдельный инструмент, а не ``nano``:

  * на сервере ``.env`` правится один раз и почти всегда с опечаткой — лишний
    слэш в ``PANEL_URL``, пробел внутри ``PANEL_INBOUND_IDS=1, 2``, кавычки
    вокруг значения;
  * здесь значение записывается ровно таким, каким его передали, файл получает
    резервную копию, а комментарии и порядок остальных строк не меняются;
  * инструмент ничего не знает про панель и сеть — только стандартная
    библиотека, поэтому работает до создания ``.venv``:
    ``python3 scripts/configure_env.py --set KEY=VALUE``.

Примеры:

    python3 scripts/configure_env.py --set PANEL_TYPE=xui \\
        --set PUBLIC_BASE_URL=http://203.0.113.10:8090
    python3 scripts/configure_env.py --set PANEL_URL=http://1.2.3.4:54321/abc --dry-run
    python3 scripts/configure_env.py --get PANEL_URL        # напечатать значение

Код возврата: 0 — успех, 1 — переменная не найдена (для ``--get``),
2 — ошибка аргументов (некорректный ключ, перевод строки в значении).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

#: Имя переменной окружения: буква/подчёркивание, дальше буквы, цифры, подчёркивания.
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvError(ValueError):
    """Некорректный ключ или значение — править файл нельзя."""


def parse_updates(pairs: list[str]) -> list[tuple[str, str]]:
    """Разобрать список строк ``KEY=VALUE`` в пары.

    :param pairs: значения из ``--set``.
    :raises EnvError: если нет ``=``, имя переменной некорректно или значение
        содержит перевод строки (одна переменная — одна строка файла).
    """
    updates: list[tuple[str, str]] = []
    for item in pairs:
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator:
            raise EnvError(f"нужно KEY=VALUE, получено: {item!r}")
        if not KEY_RE.match(key):
            raise EnvError(f"некорректное имя переменной: {key!r}")
        if "\n" in value or "\r" in value:
            raise EnvError(f"значение {key} содержит перевод строки")
        updates.append((key, value.strip()))
    return updates


def get_value(text: str, key: str) -> str | None:
    """Значение переменной из текста ``.env`` или ``None``.

    Берём **последнее** вхождение: так же читают файл pydantic-settings и наши
    shell-скрипты, поэтому результат совпадает с тем, что увидит приложение.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$")
    result: str | None = None
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            result = match.group(1).strip()
    return result


def update_env(text: str, updates: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Вернуть новый текст ``.env`` и список человекочитаемых изменений.

    Существующие строки переменной заменяются (все вхождения — дубликатов не
    остаётся), отсутствующие дописываются в конец файла. Остальные строки,
    комментарии и порядок не трогаются.
    """
    lines = text.splitlines()
    changes: list[str] = []

    for key, value in updates:
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        new_line = f"{key}={value}"
        hits = [index for index, line in enumerate(lines) if pattern.match(line)]
        if not hits:
            lines.append(new_line)
            changes.append(f"+ {new_line}")
            continue
        was = lines[hits[-1]].strip()
        # Значение ставим на место последнего вхождения (именно его читает
        # приложение), а более ранние дубликаты удаляем — дублей не остаётся.
        lines[hits[-1]] = new_line
        for index in reversed(hits[:-1]):
            del lines[index]
        if was == new_line:
            changes.append(f"= {new_line}")
        else:
            changes.append(f"~ {was} → {new_line}")

    result = "\n".join(lines)
    if text.endswith("\n") or not text:
        result += "\n"
    return result, changes


def write_env(
    path: Path,
    updates: list[tuple[str, str]],
    *,
    dry_run: bool = False,
) -> tuple[list[str], Path | None]:
    """Записать изменения в ``.env`` (с резервной копией) и вернуть их описание.

    :returns: (список изменений, путь к резервной копии или ``None``).
    """
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    updated, changes = update_env(original, updates)
    if dry_run or updated == original:
        return changes, None

    backup: Path | None = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)

    path.write_text(updated, encoding="utf-8")
    if backup is None:
        # Новый файл: сразу закрываем права, в .env лежат секреты.
        os.chmod(path, 0o600)
    return changes, backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Аккуратно поменять переменные в .env (с резервной копией).",
    )
    parser.add_argument("--env", default=".env", help="путь к .env (по умолчанию .env)")
    parser.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="что записать; можно повторять",
    )
    parser.add_argument("--get", metavar="KEY", help="напечатать значение переменной и выйти")
    parser.add_argument("--dry-run", action="store_true", help="показать изменения, но не писать")
    args = parser.parse_args(argv)

    path = Path(args.env)

    if args.get:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        value = get_value(text, args.get)
        if value is None:
            print(f"{args.get} не найдена в {path}", file=sys.stderr)
            return 1
        print(value)
        return 0

    if not args.assignments:
        parser.error("нужен хотя бы один --set KEY=VALUE (или --get KEY)")

    try:
        updates = parse_updates(args.assignments)
    except EnvError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    changes, backup = write_env(path, updates, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    for change in changes:
        print(f"{prefix}{change}")
    if backup is not None:
        print(f"{prefix}резервная копия: {backup}")
    if not args.dry_run:
        print(f"{prefix}обновлено переменных: {len(updates)} → {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - тонкая обёртка CLI
    raise SystemExit(main())
