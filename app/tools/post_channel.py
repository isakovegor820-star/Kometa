"""Опубликовать пост в канал от бота: с кнопкой, закрепом и правкой описания.

Зачем инструмент, если пост можно набрать руками из ``docs/КАНАЛ.md``.
Три причины, и все три проверяемые:

* **Кнопка.** ``docs/ПРОДАЖИ.md``: «пост без кнопки — потерянный клиент».
  У поста, набранного руками с личного аккаунта, кнопки нет — только ссылка
  строкой. Пост от бота отправляется с inline-кнопкой «Попробовать бесплатно».
* **Пост и описание канала больше не только руками.** Бот — администратор канала
  (права ``can_post_messages``, ``can_change_info`` и ``can_post_stories``
  проверены 08.10.2026 через ``getChatMember``). Значит, и пост, и описание
  канала накатываются из кода, а ``apply_launch_copy`` до этого писал «прав на
  канал у бота нет».
* **Тексты не расходятся.** Пост собирается в ``app/bot/launch_copy.py``: цены,
  локации и срок пробного доступа — оттуда же, откуда описание бота и канала.

**По умолчанию — сухой прогон**: печатает текст, кнопку и адрес канала и ничего
не отправляет. Отправка — только с ``--apply``.

Запуск::

    .venv/bin/python -m app.tools.post_channel                  # что уйдёт в канал
    .venv/bin/python -m app.tools.post_channel --check          # права бота и описание канала
    .venv/bin/python -m app.tools.post_channel --apply          # отправить пост
    .venv/bin/python -m app.tools.post_channel --apply --pin    # отправить и закрепить
    .venv/bin/python -m app.tools.post_channel --text first --apply
    .venv/bin/python -m app.tools.post_channel --file post.txt --apply
    .venv/bin/python -m app.tools.post_channel --fix-description --apply

**Как проверить:** инструмент печатает ссылку на отправленное сообщение
(``t.me/<канал>/<id>``) и читает описание канала обратно: «накатилось» и «в
Telegram лежит ровно это» — разные вещи.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.bot import launch_copy

#: Что умеет публиковать инструмент: имя → что это за пост.
POSTS: dict[str, str] = {
    "offer": "тарифы и пробный доступ: короткий пост с одной кнопкой",
    "first": "первый пост-знакомство — тот, что стоит в закрепе",
}

#: Лимит одного сообщения Telegram.
POST_LIMIT = 4096

#: Подпись кнопки по умолчанию. Пустая строка — пост без кнопки.
BUTTON_TEXT = "🚀 Попробовать бесплатно"


def channel_post(kind: str = "offer") -> str:
    """Текст поста по имени. Неизвестное имя — понятная ошибка, а не пустой пост."""
    if kind == "offer":
        return launch_copy.channel_offer_post()
    if kind == "first":
        return launch_copy.channel_first_post()
    raise SystemExit(f"неизвестный пост «{kind}»: есть {', '.join(sorted(POSTS))}")


def read_post_file(path: str | Path) -> str:
    """Прочитать текст поста из файла и снять забор ```, если он там один.

    Готовые тексты лежат в ``docs`` внутри блоков ```: скопированный вместе с
    забором текст Telegram покажет как есть — вместе с забором.
    """
    file = Path(path)
    if not file.is_file():
        raise SystemExit(f"файла нет: {file}")
    text = file.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        text = "\n".join(lines[1:-1]).strip()
    return text


def lint_post(text: str) -> list[str]:
    """Что мешает публикации. Пустой список — можно отправлять."""
    problems: list[str] = []
    if not text.strip():
        problems.append("пустой текст")
    if len(text) > POST_LIMIT:
        problems.append(
            f"{len(text)} знаков при лимите {POST_LIMIT} — Telegram отклонит сообщение"
        )
    if "```" in text:
        problems.append("в тексте остался забор ``` — Telegram покажет его как есть")
    return problems


def channel_target() -> str:
    """Адрес канала из настроек. Пусто — канал не настроен, публиковать некуда."""
    from app.config import get_settings

    return (get_settings().resolved_channel_id or "").strip()


def post_button(text: str | None = None) -> tuple[str, str]:
    """Подпись и адрес кнопки под постом: ведём в бота, а не в канал."""
    from app.config import get_settings

    label = (text if text is not None else BUTTON_TEXT).strip()
    username = (get_settings().bot_username or "").strip() or "kometavpnservise_bot"
    return label, f"https://t.me/{username}"


def location_words() -> list[str]:
    """Названия локаций из ``launch_copy.locations()`` — без флагов и разделителей.

    Список не дублируем: появится четвёртая локация — проверка описания канала
    поедет за ней сама.
    """
    words: list[str] = []
    for chunk in launch_copy.locations().split("·"):
        clean = "".join(ch for ch in chunk if ch.isalpha() or ch in " -").strip()
        if clean:
            words.append(clean)
    return words


def description_problems(current: str, target: str | None = None) -> list[str]:
    """Что не так с описанием канала: готовый текст против того, что в Telegram.

    Ловит именно тот случай, из-за которого описание «протухает» молча:
    08.10.2026 в канале стояло «2 локации» без названий, а работали три.
    """
    current = (current or "").strip()
    target = (target if target is not None else launch_copy.channel_description()).strip()
    problems: list[str] = []
    if not current:
        problems.append("описание канала пустое")
    if len(current) > launch_copy.CHANNEL_DESCRIPTION_LIMIT:
        problems.append(
            f"{len(current)} знаков при лимите {launch_copy.CHANNEL_DESCRIPTION_LIMIT}"
        )
    lowered = current.lower()
    for needle, why in launch_copy.CHANNEL_REQUIRED:
        if needle not in lowered:
            problems.append(f"нет «{needle}» — {why}")
    missing = [word for word in location_words() if word not in current]
    if missing:
        problems.append("в описании нет локаций: " + ", ".join(missing))
    if current != target:
        problems.append(
            "текст отличается от launch_copy.channel_description() — "
            "накатить: --fix-description --apply"
        )
    return problems


def message_link(chat_id: str, message_id: int) -> str:
    """Ссылка на сообщение: публичный канал — по имени, приватный — по id.

    Нужна, чтобы после отправки было что открыть и проверить глазами, а не
    искать пост в ленте.
    """
    chat = (chat_id or "").strip()
    if chat.startswith("@"):
        return f"https://t.me/{chat.lstrip('@')}/{message_id}"
    if chat.startswith("-100"):
        return f"https://t.me/c/{chat[4:]}/{message_id}"
    return f"(канал {chat}, сообщение {message_id})"


def _token() -> str:
    from app.config import get_settings

    token = (get_settings().bot_token or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не заполнен в .env — публиковать нечем")
    return token


async def _check(chat_id: str) -> int:  # pragma: no cover - ручной запуск
    from aiogram import Bot

    bot = Bot(token=_token())
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        rights = {
            "can_post_messages": getattr(member, "can_post_messages", False),
            "can_change_info": getattr(member, "can_change_info", False),
            "can_pin_messages": getattr(member, "can_pin_messages", False),
            "can_post_stories": getattr(member, "can_post_stories", False),
        }
        print(f"Статус бота в канале {chat_id}: {getattr(member, 'status', '?')}")
        for name, value in rights.items():
            print(f"  {'✅' if value else '❌'} {name}")

        chat = await bot.get_chat(chat_id)
        current = (chat.description or "").strip()
        print(f"\nОписание канала сейчас ({len(current)} знаков):")
        print(current or "(пусто)")
        problems = description_problems(current)
        if problems:
            print("\nЧем плохо:")
            for problem in problems:
                print(f"  · {problem}")
            return 1
        print("\n✅ Описание канала актуально")
        return 0 if all(rights.values()) else 1
    finally:
        await bot.session.close()


async def _apply(  # pragma: no cover - ручной запуск
    *,
    chat_id: str,
    text: str,
    pin: bool,
    fix_description: bool,
    button: tuple[str, str] | None,
) -> int:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    bot = Bot(token=_token())
    try:
        if fix_description:
            target = launch_copy.channel_description()
            await bot.set_chat_description(chat_id=chat_id, description=target)
            chat = await bot.get_chat(chat_id)
            actual = (chat.description or "").strip()
            if actual != target.strip():
                print("⚠️ Telegram вернул другое описание — сверь вручную:")
                print(actual)
                return 1
            print("✅ Описание канала обновлено (проверено обратным чтением)")

        if not text:
            return 0

        markup = None
        if button is not None:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=button[0], url=button[1])]]
            )
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        print(f"✅ Пост отправлен: {message_link(chat_id, sent.message_id)}")
        if pin:
            await bot.pin_chat_message(
                chat_id=chat_id, message_id=sent.message_id, disable_notification=True
            )
            print("✅ Закреплён без уведомления — новый человек видит его первым")
        return 0
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - ручной запуск
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--text", choices=sorted(POSTS), default="offer", help="какой пост публиковать"
    )
    parser.add_argument("--file", help="свой текст файлом: перебивает --text")
    parser.add_argument("--apply", action="store_true", help="отправить (без флага — сухой прогон)")
    parser.add_argument("--pin", action="store_true", help="закрепить отправленный пост")
    parser.add_argument("--check", action="store_true", help="права бота и описание канала")
    parser.add_argument(
        "--fix-description", action="store_true", help="накатить описание канала из текстов"
    )
    parser.add_argument("--button", default=BUTTON_TEXT, help="подпись кнопки; пусто — без кнопки")
    args = parser.parse_args(argv)

    text = read_post_file(args.file) if args.file else channel_post(args.text)
    problems = lint_post(text)
    if problems:
        print("Пост не готов:")
        for problem in problems:
            print(f"  · {problem}")
        return 1

    target = channel_target()
    print("=== Пост ===")
    print(text)
    print(f"\n[знаков: {len(text)} из {POST_LIMIT}]")
    label, url = post_button(args.button)
    if label:
        print(f"Кнопка: {label} → {url}")
    else:
        print("Кнопка: не ставим (--button \"\")")
    print(f"Канал: {target or '(CHANNEL_ID не заполнен в .env)'}")

    if args.check:
        if not target:
            print("\nНечего проверять: CHANNEL_ID не заполнен в .env")
            return 1
        print()
        return asyncio.run(_check(target))

    if not args.apply:
        print("\nСухой прогон: ничего не отправлено. Для отправки добавь --apply.")
        return 0

    if not target:
        print("\nCHANNEL_ID не заполнен в .env — отправлять некуда")
        return 1

    print("\nОтправляю в Telegram…")
    return asyncio.run(
        _apply(
            chat_id=target,
            text=text,
            pin=args.pin,
            fix_description=args.fix_description,
            button=(label, url) if label else None,
        )
    )


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
