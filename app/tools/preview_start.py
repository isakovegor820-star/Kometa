#!/usr/bin/env python3
"""Превью экранов бота: собрать настоящий payload и показать/отправить его.

Зачем. Тесты проверяют «что бот отправит», но не показывают, **как это выглядит
в Telegram**. Здесь мы собираем реальные методы Bot API (``sendPhoto`` с подписью
и кнопками) и либо печатаем их как JSON, либо отправляем себе в Telegram — чтобы
посмотреть глазами и не запускать бота целиком с боевым токеном.

Запуск::

    # показать, что уйдёт на /start (JSON, ничего не отправляется)
    .venv/bin/python -m app.tools.preview_start --dump

    # отправить себе в Telegram: нужен BOT_TOKEN и PREVIEW_CHAT_ID
    .venv/bin/python -m app.tools.preview_start --send 123456789

    # какой именно экран: new (по умолчанию), active, expired
    .venv/bin/python -m app.tools.preview_start --state active --send 123456789

Отправка идёт **вашему** chat_id: бот ничего не рассылает клиентам. Оплаты и
панель не затрагиваются: скрипт только формирует сообщение.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE))

# Демо-окружение: как в design/build.py — временная БД, заглушка панели.
_DB = Path(tempfile.gettempdir()) / "kometa-preview.db"
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{_DB}"
# BOT_TOKEN берём из .env, а не подставляем заглушку: иначе превью не сможет
# ничего отправить, а pydantic-settings прочитает значение из окружения и
# перекроет файл. Переменная окружения, если она задана явно, всё равно главнее.
def _token_from_env_file() -> str:
    env_file = WORKSPACE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*BOT_TOKEN\s*=\s*(\S+)", line)
            if match:
                return match.group(1)
    return "000000:PREVIEW"


os.environ.setdefault("BOT_TOKEN", _token_from_env_file())
os.environ.setdefault("PANEL_TYPE", "fake")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("SALES_ENABLED", "true")


async def build(state: str, name: str = "Egor"):
    """Собрать экран меню в нужном состоянии: ``new`` / ``active`` / ``expired``."""
    from app.bot import view
    from app.bot.handlers import start as start_handlers
    from app.db.session import SessionMaker, init_db, seed_plans
    from app.panels.registry import registry
    from app.services import orders, subscriptions

    if _DB.exists():
        _DB.unlink()
    await init_db()
    async with SessionMaker() as session:
        await seed_plans()
        user, _ = await subscriptions.get_or_create_user(
            session, tg_id=1, username=name.lower(), first_name=name
        )
        if state != "new":
            plans = await orders.list_plans(session)
            plan = next(p for p in plans if p.code == "m1")
            sub = await subscriptions.activate_plan(session, user, plan, registry.primary())
            if state == "expired":
                sub.status = "expired"
        await session.commit()
        menu = await start_handlers.main_menu_view(session, user, hero=(state == "new"))
        return menu, view


def payload(menu, view_mod) -> dict:
    """Payload ровно в том виде, в каком его увидит Telegram (без отправки)."""
    keyboard = [
        [
            {k: v for k, v in {"text": b.text, "style": b.style, "callback_data": b.callback_data,
                               "url": b.url}.items() if v}
            for b in row
        ]
        for row in menu.markup.inline_keyboard
    ]
    if menu.photo is None:
        return {"method": "sendMessage", "text": menu.text, "reply_markup": {"inline_keyboard": keyboard}}
    return {
        "method": "sendPhoto",
        "photo": str(menu.photo.relative_to(WORKSPACE)),
        "photo_bytes": menu.photo.stat().st_size,
        "caption": menu.text,
        "caption_length": len(menu.text),
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard},
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Превью экрана бота")
    parser.add_argument("--state", default="new", choices=["new", "active", "expired"])
    parser.add_argument("--name", default="Egor", help="имя для приветствия")
    parser.add_argument("--dump", action="store_true", help="напечатать payload и выйти")
    parser.add_argument("--send", type=int, default=0, help="chat_id, куда отправить превью")
    args = parser.parse_args()

    menu, view_mod = await build(args.state, args.name)
    data = payload(menu, view_mod)
    if args.dump or not args.send:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if not args.send:
            print("\n(отправка не запрошена: добавь --send <chat_id>)", file=sys.stderr)
        return 0

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from app.config import get_settings

    settings = get_settings()
    async with Bot(
        token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    ) as bot:
        sent = await view_mod.send_view(
            _Answerer(bot, args.send),
            menu,
            reply_markup=None,
        )
    print(f"отправлено в {args.send}: message_id={getattr(sent, 'message_id', '?')}")
    return 0


class _Answerer:
    """Минимальная обёртка: ``send_view`` ждёт объект с ``answer*``-методами.

    Так превью использует **тот же код**, что и бот в бою, — иначе смысл превью
    теряется: можно показать красивое сообщение, которого бот не отправляет.
    """

    def __init__(self, bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def answer(self, text: str, **kwargs):
        return await self.bot.send_message(self.chat_id, text, **kwargs)

    async def answer_photo(self, photo, **kwargs):
        return await self.bot.send_photo(self.chat_id, photo, **kwargs)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
