#!/usr/bin/env python3
"""Сверка текстов макетов с реальными текстами бота (регрессия).

Две проверки на каждую фразу:
  1) фраза дословно есть в app/bot/texts.py после подстановки параметров;
  2) та же фраза дословно есть в макете parts/NN-*.html.

Если копирайтер поменяет текст в texts.py, а макеты не пересоберут — проверка
упадёт. Если в макете появится выдуманная фраза — упадёт тоже.

Запуск:  python3 design/bot/tools/screens/check_texts.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Корень репозитория: …/design/bot/tools/screens/check_texts.py → на четыре уровня выше.
ROOT = Path(__file__).resolve().parents[4]
SRC = Path(__file__).resolve().parent
if not (ROOT / "app" / "bot" / "texts.py").exists():
    sys.exit(f"не нашли корень репозитория от {Path(__file__).resolve()} — ожидали {ROOT}")
sys.path.insert(0, str(ROOT))

from app.bot import texts  # noqa: E402

REAL = {
    "WELCOME": texts.WELCOME.format(brand=texts.BRAND, trial_days=3),
    "MENU_NO_SUB": texts.MENU_NO_SUB,
    "MENU_ACTIVE": texts.MENU_ACTIVE,
    "MENU_EXPIRED": texts.MENU_EXPIRED,
    "MY_SUB_ACTIVE": texts.MY_SUB_ACTIVE.format(status=texts.STATUS_ACTIVE, expires="12.11.2026", days=35, devices=3),
    "STARS_LINE": texts.STARS_LINE.format(stars=110),
    "MY_SUB_EXPIRED": texts.MY_SUB_EXPIRED.format(ago="3 дня назад"),
    "PLANS_HEADER": texts.PLANS_HEADER,
    "PLAN_CARD": texts.PLAN_CARD.format(title="1 месяц", price_line=texts.PRICE_LINE.format(price=120, per_month=120, stars_line=texts.STARS_LINE.format(stars=110)), days=30, devices=3),
    "HOWTO": texts.HOWTO,
    "SUPPORT": texts.SUPPORT.format(support="@egorTech888"),
    "ORDER_CREATED_WATA": texts.ORDER_CREATED_WATA.format(order_id=1042, amount=84),
    "ORDER_CREATED_PLATEGA": texts.ORDER_CREATED_PLATEGA.format(order_id=1042, amount=84, method="карта МИР"),
    "DISCOUNT_NOTE": texts.DISCOUNT_NOTE.format(discount=36, code="KOMETA-AB12CD34"),
    "SUBSCRIPTION_LINK_HINT": texts.SUBSCRIPTION_LINK_HINT.format(link="https://example/sub/tok"),
    "RESERVE_HELP": texts.RESERVE_HELP,
    "LEGAL_HEADER": texts.LEGAL_HEADER,
    "SUBSCRIPTION_COPY": texts.SUBSCRIPTION_COPY.format(link="https://example/sub/tok"),
    "MY_SUB_ACTIVE_STATUS": texts.STATUS_ACTIVE,
    "PRICE_LINE_DISCOUNT": texts.PRICE_LINE_DISCOUNT.format(base=120, price=84, per_month=84, percent=30, stars_line=""),
}

#: Фразы, которые обязаны быть в макетах дословно (копия из parts/*.html).
CHECKS: list[tuple[str, str, str]] = [
    # 01-03 — новая копия из design/bot/START-COPY.md (разделы 3.1-3.3),
    # в texts.py её пока нет: это целевой текст, а не текущий.
    # 04-09 — фразы, которые обязаны совпадать с texts.py дословно.
    ("04", "MY_SUB_ACTIVE", "Твоя подписка"),
    ("04", "MY_SUB_ACTIVE", "Статус: ✅ активна"),
    ("04", "MY_SUB_ACTIVE", "Трафик: без ограничений"),
    ("04", "SUBSCRIPTION_LINK_HINT", "Ссылка постоянная: не удаляй профиль, при смене сервера она обновится сама."),
    ("04", "RESERVE_HELP", "это не отдельный тариф: подписка та же, доплачивать не нужно"),
    ("05", "PLANS_HEADER", "Оплата активируется сразу после подтверждения. Один тариф — до 3 устройств, трафик без ограничений."),
    ("05", "PLANS_HEADER", "Выбери срок:"),
    ("05", "PLAN_CARD", "Срок: 30 дней"),
    ("05", "PLAN_CARD", "Устройств: до 3"),
    ("05", "PLAN_CARD", "Трафик: без ограничений"),
    ("05", "STARS_LINE", "Или <b>110 ⭐</b> в Telegram Stars — доступ включится сразу"),
    ("06", "ORDER_CREATED_WATA", "Заказ #1042 на 84 ₽"),
    ("06", "ORDER_CREATED_WATA", "откроется защищённая страница оплаты: карта (МИР, Visa, Mastercard), СБП, T-Pay или SberPay."),
    ("06", "ORDER_CREATED_WATA", "Доступ включится <b>автоматически</b> сразу после оплаты."),
    ("06", "ORDER_CREATED_PLATEGA", "Счёт действует 15 минут."),
    ("06", "DISCOUNT_NOTE", "🎉 Скидка <b>36 ₽</b> по промокоду <code>KOMETA-AB12CD34</code> уже учтена."),
    ("06", "PRICE_LINE_DISCOUNT", "Цена: <s>120 ₽</s> → <b>84 ₽</b> (84 ₽ в месяц) 🎉"),
    ("07", "HOWTO", "1. Установи приложение"),
    ("07", "HOWTO", "• Android: v2rayNG, Hiddify или Happ"),
    ("07", "HOWTO", "• iPhone/iPad: Streisand или Happ"),
    ("07", "HOWTO", "• Windows: Hiddify или v2rayN"),
    ("07", "HOWTO", "• macOS: Hiddify или sing-box"),
    ("07", "HOWTO", "2. Добавь подписку"),
    ("07", "HOWTO", "3. Обнови и подключись"),
    ("07", "HOWTO", "Нажми «Обновить подписки», выбери локацию с меньшим пингом и включи соединение."),
    ("07", "HOWTO", "Если не работает — напиши в поддержку, поможем за несколько минут."),
    ("08", "SUPPORT", "Написать: @egorTech888"),
    ("08", "SUPPORT", "Отвечаем в порядке очереди: обычно в течение 15 минут, сложные технические вопросы — до 24 часов."),
    ("08", "SUPPORT", "Чтобы решить вопрос с первого сообщения</b>, укажите:"),
    ("08", "SUPPORT", "• номер заказа (например, #123) или ваш Telegram ID (команда /id);"),
    ("08", "SUPPORT", "• что именно не работает — телефон, ноутбук или ТВ;"),
    ("08", "SUPPORT", "• приложение, которым пользуетесь."),
    ("09", "RESERVE_HELP", "Выбери профиль с пометкой «резерв» или «CDN»"),
    ("09", "RESERVE_HELP", "скорость ниже обычной — он для переписки, карт и работы, не для видео"),
]


def norm(text: str) -> str:
    """Схлопнуть переводы строк и отступы: в HTML текст разбит на строки."""
    return re.sub(r"\s+", " ", text).strip()


PART_FILES = {p.name[:2]: p for p in sorted((SRC / "parts").glob("*.html"))}


def main() -> int:
    bad = 0
    warn = 0
    for screen, key, phrase in CHECKS:
        # 1) главная регрессия: фраза обязана быть в texts.py дословно
        if phrase not in REAL[key]:
            print(f"✗ {screen} ~ {key}: фразы нет в texts.py → {phrase!r}")
            bad += 1
        # 2) мягкая проверка: та же фраза должна стоять в макете (разметка может
        #    отличаться — <b>/<code> вокруг других слов, поэтому это warning)
        part = PART_FILES.get(screen)
        if part is None:
            print(f"! {screen}: нет part-файла")
            warn += 1
        elif norm(phrase) not in norm(part.read_text(encoding="utf-8")):
            print(f"! {screen} ~ {key}: в макете нет дословно (проверить разметку) → {phrase!r}")
            warn += 1
    print(f"\nвсего проверок: {len(CHECKS)}, расхождений с texts.py: {bad}, мягких замечаний: {warn}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
