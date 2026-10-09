"""Накатить тексты «до старта» на бота: описание, «о боте», команды.

Зачем инструмент, если это делается руками в BotFather. Ровно потому, что
руками оно и расходится: 08.10.2026 в описании бота и канала стояли «2 локации:
Германия и Нидерланды», а работали три. BotFather ничего не проверяет — он
молча обрезает текст по лимиту и сохраняет то, что дали. Поэтому тексты лежат
в коде (``app/bot/launch_copy.py``), проверяются на лимиты и факты (``--check``)
и накатываются одной командой.

**По умолчанию — сухой прогон**: печатает, что будет отправлено, и ничего не
меняет. Запись — только с ``--apply``.

Запуск::

    .venv/bin/python -m app.tools.apply_launch_copy              # показать
    .venv/bin/python -m app.tools.apply_launch_copy --check      # только проверка
    .venv/bin/python -m app.tools.apply_launch_copy --apply      # накатить
    .venv/bin/python -m app.tools.apply_launch_copy --apply --commands

**Как проверить после наката** (без BotFather и без сторонних библиотек):
открой чат с ботом в Telegram, НЕ нажимая «Начать» — текст над кнопкой
«Начать» и есть описание; «о боте» видно в профиле бота (⋮ → «О боте», или
``https://t.me/<бот>?profile``). Команды проверяются в меню рядом с полем ввода.
Отдельно: ``curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMyDescription"``
вернёт то, что реально записано в Telegram, — это и есть источник правды.

**Чего инструмент не делает:** не публикует посты в канал и не меняет описание
канала. Это — ``app/tools/post_channel.py``: после включения гейта подписки бот стал
администратором канала и права на пост и описание у него появились (проверено
08.10.2026 через ``getChatMember``). Готовые тексты для вставки руками лежат в
``docs/КАНАЛ.md`` (разделы 1–3), они собираются из тех же констант этим же кодом
(``--channel`` печатает их в файл).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.bot import launch_copy

#: Команды в меню Telegram (``/setcommands``). Список короткий осознанно: меню
#: команд читают, чтобы найти нужное, а не чтобы изучить бота. Всё остальное
#: живёт кнопками. ``/profile`` ведёт на экран профиля клиента.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Открыть меню"),
    ("profile", "Мой профиль"),
    ("menu", "Главное меню"),
    ("support", "Написать в поддержку"),
)


def _token() -> str:
    from app.config import get_settings

    token = (get_settings().bot_token or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не заполнен в .env — накатывать некуда")
    return token


def _print_lint(report: launch_copy.LintReport) -> None:
    print("Проверка текстов:")
    print(report.as_text())


def _print_texts() -> None:
    print("\n=== Описание бота (видно до нажатия «Начать») ===")
    print(launch_copy.bot_description())
    print(f"\n[знаков: {len(launch_copy.bot_description())} из {launch_copy.DESCRIPTION_LIMIT}]")
    print("\n=== О боте (строка в профиле) ===")
    print(launch_copy.bot_short_description())
    print(
        f"\n[знаков: {len(launch_copy.bot_short_description())} "
        f"из {launch_copy.SHORT_DESCRIPTION_LIMIT}]"
    )
    print("\n=== Описание канала ===")
    print(launch_copy.channel_description())
    print(
        f"\n[знаков: {len(launch_copy.channel_description())} "
        f"из {launch_copy.CHANNEL_DESCRIPTION_LIMIT}]"
    )


async def _apply(commands: bool) -> int:
    from aiogram import Bot
    from aiogram.types import BotCommand

    bot = Bot(token=_token())
    try:
        await bot.set_my_description(description=launch_copy.bot_description())
        print("✅ Описание бота обновлено")
        await bot.set_my_short_description(
            short_description=launch_copy.bot_short_description()
        )
        print("✅ «О боте» обновлено")

        if commands:
            await bot.set_my_commands(
                [BotCommand(command=name, description=title) for name, title in COMMANDS]
            )
            print(f"✅ Команды обновлены: {', '.join('/' + name for name, _ in COMMANDS)}")

        # Читаем обратно: Telegram мог обрезать или не применить — тогда «накат»
        # выглядел бы успешным, а в чате остался старый текст.
        stored = await bot.get_my_description()
        actual = (stored.description or "").strip()
        if actual != launch_copy.bot_description().strip():
            print("⚠️ Telegram вернул другое описание — сверь вручную:")
            print(actual)
            return 1
        print("Проверено обратным чтением: в Telegram лежит ровно тот текст.")
        return 0
    finally:
        await bot.session.close()


def _write_channel_copy(path: Path) -> None:
    """Выгрузить тексты канала файлом — чтобы копировать, а не искать глазами."""
    content = (
        "# Тексты канала (сгенерировано app/tools/apply_launch_copy --channel)\n\n"
        f"## Название\n\n{launch_copy.CHANNEL_TITLE}\n\n"
        f"## Описание (≤ {launch_copy.CHANNEL_DESCRIPTION_LIMIT}, "
        f"сейчас {len(launch_copy.channel_description())})\n\n"
        f"{launch_copy.channel_description()}\n\n"
        "## Первый пост (закреп)\n\n"
        f"{launch_copy.channel_first_post()}\n"
    )
    path.write_text(content, encoding="utf-8")
    print(f"✅ Тексты канала выгружены: {path}")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - ручной запуск
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="записать в Telegram (без флага — сухой прогон)")
    parser.add_argument("--check", action="store_true", help="только проверить лимиты и обязательные факты")
    parser.add_argument("--commands", action="store_true", help="обновить и список команд")
    parser.add_argument("--channel", action="store_true", help="выгрузить тексты канала файлом")
    args = parser.parse_args(argv)

    report = launch_copy.lint()
    if args.check:
        _print_lint(report)
        return 0 if report.ok else 1

    if args.channel:
        _write_channel_copy(Path("docs/ТЕКСТЫ-КАНАЛА.md"))
        return 0

    _print_lint(report)
    if not report.ok:
        print("\nСначала почини лимиты: обрезанный BotFather текст вернуть нельзя.")
        return 1

    _print_texts()
    if not args.apply:
        print("\nСухой прогон: ничего не изменено. Для записи добавь --apply.")
        return 0

    print("\nЗаписываю в Telegram…")
    return asyncio.run(_apply(commands=args.commands))


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
