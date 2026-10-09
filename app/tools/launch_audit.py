"""Проверить витрину: то, что человек видит ДО оплаты.

Зачем отдельный инструмент. Тесты сравнивают код с кодом — ``launch_copy``
против ``launch_copy``, лимиты BotFather, запрещённые слова. **Живое состояние**
Telegram и telegra.ph не проверяет ничто, поэтому расхождение копится молча.
Так вышло трижды, и каждый раз это находили глазами и случайно:

* описание бота обещало оплату **картой**, которой нет (``PLATEGA_METHODS=2,13``);
* опубликованный пост канала говорил «**2 локации**» при трёх работающих;
* в политике, оферте и прайсе клиент читал «**[ИСПОЛНИТЕЛЬ]**, ИНН **[ИНН]**».

Инструмент читает то, что реально лежит в Telegram и на страницах, и сравнивает
с тем, что собирает код. **Ничего не меняет и не отправляет** — только
ПРИНЯТО/НЕ ПРИНЯТО и что сделать. Код возврата 1, если есть расхождения, поэтому
его можно ставить в приёмку перед запуском рекламы.

Запуск::

    .venv/bin/python -m app.tools.launch_audit           # проверить витрину
    .venv/bin/python -m app.tools.launch_audit --json    # машиночитаемо

Что инструмент проверить НЕ может: опубликованы ли посты канала (Telegram не
отдаёт список сообщений боту) и что именно вписано в кабинете Platega. Это руками
— список в ``docs/ДЕНЬ-ЗАПУСКА.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field

import httpx

from app.bot import launch_copy

#: Что не должно встречаться в опубликованных документах.
PLACEHOLDERS: tuple[str, ...] = ("[ИСПОЛНИТЕЛЬ]", "[ИНН]", "[ПОДДЕРЖКА]", "[БОТ]")

#: Таймаут на страницу документа: медленный telegra.ph не повод падать.
PAGE_TIMEOUT = 20.0


def _squeeze(text: str) -> str:
    """Схлопнуть пробелы: Telegram отдаёт текст с другими переводами строк."""
    return re.sub(r"\s+", " ", (text or "")).strip()


@dataclass
class Check:
    """Один пункт витрины: что проверяли, чем закончилось, что делать."""

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


@dataclass
class Snapshot:
    """Слепок живого состояния. Заполняется сетью, проверяется без неё."""

    bot_description: str = ""
    bot_short_description: str = ""
    bot_commands: tuple[tuple[str, str], ...] = ()
    channel_description: str = ""
    channel_title: str = ""
    channel_admin: bool = False
    can_pin_messages: bool = False
    channel_pinned: bool = False
    legal_pages: dict[str, str] = field(default_factory=dict)
    legal_name: str = ""
    legal_inn: str = ""
    #: Чего не удалось прочитать: сеть, права, отсутствующий адрес.
    errors: list[str] = field(default_factory=list)


def audit(snapshot: Snapshot) -> list[Check]:
    """Сравнить живое состояние с тем, что собирает код. Без сети — чистая логика."""
    checks: list[Check] = []

    expected_description = _squeeze(launch_copy.bot_description())
    live_description = _squeeze(snapshot.bot_description)
    checks.append(
        Check(
            "описание бота",
            live_description == expected_description,
            "совпадает с кодом" if live_description == expected_description else "в Telegram лежит другой текст",
            "python -m app.tools.apply_launch_copy --apply --commands",
        )
    )

    expected_short = _squeeze(launch_copy.bot_short_description())
    live_short = _squeeze(snapshot.bot_short_description)
    checks.append(
        Check(
            "короткое описание бота",
            live_short == expected_short,
            "совпадает" if live_short == expected_short else ("пусто в Telegram" if not live_short else "другой текст"),
            "python -m app.tools.apply_launch_copy --apply",
        )
    )

    from app.tools.apply_launch_copy import COMMANDS

    expected_commands = tuple(COMMANDS)
    live_commands = tuple(snapshot.bot_commands)
    checks.append(
        Check(
            "команды в меню бота",
            live_commands == expected_commands,
            "совпадают" if live_commands == expected_commands else f"в Telegram: {len(live_commands)}, в коде: {len(expected_commands)}",
            "python -m app.tools.apply_launch_copy --apply --commands",
        )
    )

    expected_channel = _squeeze(launch_copy.channel_description())
    live_channel = _squeeze(snapshot.channel_description)
    checks.append(
        Check(
            "описание канала",
            live_channel == expected_channel,
            "совпадает с кодом" if live_channel == expected_channel else "в канале другой текст",
            "python -m app.tools.post_channel --fix-description --apply",
        )
    )

    checks.append(
        Check(
            "бот — администратор канала",
            snapshot.channel_admin,
            "да" if snapshot.channel_admin else "бот не администратор: гейт подписки и посты не работают",
            "добавить бота администратором канала в Telegram",
        )
    )

    checks.append(
        Check(
            "право закреплять сообщения",
            snapshot.can_pin_messages,
            "есть" if snapshot.can_pin_messages else "нет права can_pin_messages",
            "Telegram → управление каналом → администраторы → бот → включить «Закреплять сообщения»",
        )
    )

    checks.append(
        Check(
            "закреплённый пост в канале",
            snapshot.channel_pinned,
            "есть" if snapshot.channel_pinned else "закрепа нет: первый экран канала пуст",
            "python -m app.tools.post_channel --text first --apply --pin",
        )
    )

    checks.append(
        Check(
            "реквизиты исполнителя заполнены",
            bool(_squeeze(snapshot.legal_name)) and bool(_squeeze(snapshot.legal_inn)),
            "имя и ИНН заданы" if snapshot.legal_name and snapshot.legal_inn else "LEGAL_OPERATOR_NAME / LEGAL_OPERATOR_INN пусты",
            "заполнить LEGAL_OPERATOR_NAME и LEGAL_OPERATOR_INN в .env → scripts/publish_legal.py",
        )
    )

    bad_pages = sorted(
        url for url, text in snapshot.legal_pages.items() if any(mark in text for mark in PLACEHOLDERS)
    )
    empty_pages = sorted(url for url, text in snapshot.legal_pages.items() if not _squeeze(text))
    checks.append(
        Check(
            "документы без заглушек",
            not bad_pages and not empty_pages and bool(snapshot.legal_pages),
            "заглушек нет" if not bad_pages and snapshot.legal_pages else f"заглушки: {', '.join(bad_pages) or '—'}; пусто: {', '.join(empty_pages) or '—'}",
            "заполнить реквизиты и переопубликовать: scripts/publish_legal.py",
        )
    )

    lint = launch_copy.lint()
    checks.append(
        Check(
            "тексты проходят линт",
            lint.ok,
            "лимиты и факты в порядке" if lint.ok else "; ".join(lint.problems),
            "поправить app/bot/launch_copy.py",
        )
    )

    return checks


# ------------------------------------------------------------------ сеть
async def collect() -> Snapshot:
    """Прочитать живое состояние: Telegram и страницы документов. Только чтение."""
    from aiogram import Bot

    from app.config import get_settings

    settings = get_settings()
    snapshot = Snapshot(legal_name=settings.legal_operator_name, legal_inn=settings.legal_operator_inn)

    token = (settings.bot_token or "").strip()
    if not token:
        snapshot.errors.append("BOT_TOKEN пуст — состояние Telegram не прочитать")
    else:
        bot = Bot(token=token)
        try:
            me = await bot.get_me()
            snapshot.bot_description = (await bot.get_my_description()).description or ""
            snapshot.bot_short_description = (await bot.get_my_short_description()).short_description or ""
            snapshot.bot_commands = tuple((c.command, c.description) for c in await bot.get_my_commands())

            channel = settings.resolved_channel_id
            if channel:
                try:
                    chat = await bot.get_chat(channel)
                    snapshot.channel_title = chat.title or ""
                    snapshot.channel_description = chat.description or ""
                    # getChat отдаёт закреп только тому, кто вправе закреплять,
                    # поэтому «нет закрепа» и «нет права» различаем отдельно.
                    snapshot.channel_pinned = chat.pinned_message is not None
                except Exception as exc:  # noqa: BLE001 - канал мог быть не задан
                    snapshot.errors.append(f"канал {channel}: {exc}")
                try:
                    member = await bot.get_chat_member(chat_id=channel, user_id=me.id)
                    snapshot.channel_admin = getattr(member, "status", "") == "administrator"
                    snapshot.can_pin_messages = bool(getattr(member, "can_pin_messages", False))
                except Exception as exc:  # noqa: BLE001
                    snapshot.errors.append(f"права бота в канале: {exc}")
            else:
                snapshot.errors.append("CHANNEL_ID/CHANNEL_URL не заданы — канал не проверить")
        finally:
            await bot.session.close()

    pages = {
        "политика": settings.privacy_url,
        "соглашение": settings.terms_url,
        "цены": settings.pricing_url,
    }
    async with httpx.AsyncClient(timeout=PAGE_TIMEOUT, follow_redirects=True) as client:
        for name, url in pages.items():
            if not (url or "").strip():
                snapshot.errors.append(f"{name}: адрес не задан")
                continue
            try:
                response = await client.get(url)
                snapshot.legal_pages[url] = response.text
            except Exception as exc:  # noqa: BLE001 - страница могла не открыться
                snapshot.errors.append(f"{name}: {exc}")

    return snapshot


def render(checks: list[Check], errors: list[str]) -> str:
    """Человеческий отчёт: по строке на пункт и итог."""
    lines: list[str] = []
    failed = 0
    for check in checks:
        mark = "✅" if check.ok else "❌"
        lines.append(f"{mark} {check.name}: {check.detail}")
        if not check.ok:
            failed += 1
            if check.fix:
                lines.append(f"    → {check.fix}")
    if errors:
        lines.append("")
        lines.append("Не удалось прочитать (это тоже расхождение):")
        lines.extend(f"  ⚠️ {item}" for item in errors)
    lines.append("")
    if failed:
        lines.append(f"НЕ ПРИНЯТО: пунктов {len(checks)}, провалено {failed}.")
    else:
        lines.append(f"ПРИНЯТО: витрина совпадает с кодом ({len(checks)} пунктов).")
    return "\n".join(lines)


async def _run(as_json: bool) -> int:
    snapshot = await collect()
    checks = audit(snapshot)
    failed = [c for c in checks if not c.ok]

    if as_json:
        print(
            json.dumps(
                {
                    "ok": not failed and not snapshot.errors,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail, "fix": c.fix} for c in checks
                    ],
                    "unreadable": snapshot.errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(checks, snapshot.errors))

    return 1 if failed or snapshot.errors else 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - ручной запуск
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="машиночитаемый отчёт")
    args = parser.parse_args(argv)
    return asyncio.run(_run(as_json=args.json))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
