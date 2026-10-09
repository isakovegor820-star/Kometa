"""Предохранитель дизайна бота: правила, которые нельзя молча откатить.

Зачем тест, а не памятка. Дизайн-система живёт в ``design/bot/`` и описывает
вещи, которые ломаются незаметно: кто-то допишет эмодзи в подпись, кто-то
добавит седьмую кнопку в меню, кто-то перекрасит опасное действие в зелёный.
Глазами это видно только на живом клиенте, а тест падает сразу.

Проверяем ровно то, что обещано в ``design/bot/GUIDE.md`` (раздел 9, приёмка):

* подписи кнопок ≤ 30 символов — иначе Telegram обрезает;
* главное действие на экране одно, опасное — одно, и они не соседи;
* в главном меню не больше шести кнопок;
* эмодзи не превращаются в салат: запрещённый декор и лимит на сообщение;
* hero-подпись влезает в лимит подписи к фото (1024 символа) и картинка на месте.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.bot import keyboards, texts, view
from app.bot.gate import markup as gate_markup

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "app" / "bot"

#: Предел длины подписи кнопки: дальше Telegram обрезает многоточием.
BUTTON_MAX = 30

#: Декор, который мы убрали осознанно. Появится снова — тест упадёт.
BANNED_EMOJI: dict[str, str] = {
    "👇": "указатель на интерфейс: текст должен объяснять сам",
    "🎉": "праздник: цена и скидка — не событие",
    "🚀": "клише: бот не «ракета», он сервис",
    "🟢": "цветной кружок: статус показываем словом и style кнопки",
    "🔵": "цветной кружок вне палитры",
    "🟣": "цветной кружок вне палитры",
    "👋": "приветствие-эмодзи: у нас есть заголовок-оффер",
    "💙": "декоративное сердце в сервисном сообщении",
    "🙂": "смайлик вместо текста",
}

#: Сколько эмодзи допустимо в одном текстовом блоке (сообщении).
EMOJI_PER_MESSAGE = 6

#: Эмодзи-диапазоны. Стрелки `←` и `→` сюда НЕ входят: в дизайн-системе это
#: служебные знаки навигации и направления цены, а не эмодзи-декор.
_NO_ARROWS = "[\U0001F000-\U0001FAFF\u2300-\u27BF\u2B00-\u2BFF]"
_EMOJI = re.compile(_NO_ARROWS)
#: Что именно считаем эмодзи в подписях кнопок: стрелка разрешена, всё прочее нет.
_BUTTON_EMOJI = re.compile(f"(?!←|→){_NO_ARROWS}")


def _all_inline_keyboards() -> dict[str, list[list]]:
    """Все inline-клавиатуры бота: имя → ряды кнопок."""
    from app.bot.handlers import gift as gift_handlers

    return {
        "main_menu:new": keyboards.main_menu(False, False, 120).inline_keyboard,
        "main_menu:active": keyboards.main_menu(True, True, 120).inline_keyboard,
        "main_menu:expired": keyboards.main_menu(True, False, 120).inline_keyboard,
        "connect": keyboards.connect_kb("https://vpn.example/sub/abc123").inline_keyboard,
        "subscription": keyboards.subscription_kb(True).inline_keyboard,
        "manual_order": keyboards.manual_order_kb(1).inline_keyboard,
        "crypto_order": keyboards.crypto_order_kb(1, "https://pay.example").inline_keyboard,
        "stars_order": keyboards.stars_order_kb(1, "https://pay.example", "https://r.example", 110).inline_keyboard,
        "referral": keyboards.referral_kb("https://t.me/share").inline_keyboard,
        "support": keyboards.support_kb().inline_keyboard,
        "back_to_menu": keyboards.back_to_menu_kb().inline_keyboard,
        "plans_button": keyboards.plans_button_kb().inline_keyboard,
        "docs_back": keyboards.docs_back_kb().inline_keyboard,
        "admin_order": keyboards.admin_order_kb(1).inline_keyboard,
        "admin_panel": keyboards.admin_panel_kb(2, True, "https://panel.example").inline_keyboard,
        "gift_recipient": keyboards.gift_recipient_kb().inline_keyboard,
        "gate": gate_markup().inline_keyboard,
        "gifts": keyboards.gifts_kb([("m1", "1 месяц", 190)]).inline_keyboard,
        "gift_pay": keyboards.gift_pay_kb(1, [("sbp", "СБП")]).inline_keyboard,
        "gift_step": gift_handlers,
    }


# ------------------------------------------------------------------ подписи
def test_button_labels_fit_telegram_limit():
    """Подпись кнопки не длиннее 30 символов — иначе Telegram её обрежет."""
    too_long: list[str] = []
    for name, rows in _all_inline_keyboards().items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            for button in row:
                if len(button.text) > BUTTON_MAX:
                    too_long.append(f"{name}: «{button.text}» ({len(button.text)})")
    assert not too_long, "слишком длинные подписи кнопок:\n" + "\n".join(too_long)


def test_button_labels_have_no_emoji():
    """В подписях кнопок эмодзи нет: смысл несут слово, роль и порядок."""
    offenders: list[str] = []
    for name, rows in _all_inline_keyboards().items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            for button in row:
                found = _BUTTON_EMOJI.findall(button.text)
                if found:
                    offenders.append(f"{name}: «{button.text}» → {''.join(found)}")
    assert not offenders, "эмодзи в кнопках:\n" + "\n".join(offenders)


def test_reply_menu_labels_have_no_emoji():
    """Нижняя клавиатура — тот же принцип, что и у inline-кнопок."""
    for row in keyboards.reply_menu().keyboard:
        for button in row:
            assert not _EMOJI.findall(button.text), f"эмодзи в нижнем меню: «{button.text}»"


# ------------------------------------------------------------------- роли
def _styles(rows: list[list]) -> list[list[str | None]]:
    return [[button.style for button in row] for row in rows]


def test_one_green_action_per_keyboard():
    """На экране ровно одно зелёное (главное) действие.

    Исключение — оплата: «Оплатить» и «Проверить оплату» стоят рядом, но
    зелёная там одна, вторая нейтральная.
    """
    problems: list[str] = []
    for name, rows in _all_inline_keyboards().items():
        if not isinstance(rows, list):
            continue
        green = sum(1 for row in _styles(rows) for style in row if style == "success")
        # Админская заявка — служебный экран: «подтвердить» и «отклонить» парой.
        # Админская заявка — служебный экран: «подтвердить» и «отклонить» парой.
        limit = 2 if name.startswith("admin_order") else 1
        if green > limit:
            problems.append(f"{name}: зелёных {green}, допустимо {limit}")
    assert not problems, "\n".join(problems)


def test_danger_is_alone_and_last():
    """Опасное действие: ровно одно, последним рядом, не рядом с зелёным."""
    problems: list[str] = []
    for name, rows in _all_inline_keyboards().items():
        if not isinstance(rows, list):
            continue
        flat = [style for row in rows for style in row]
        red_rows = [index for index, row in enumerate(rows) if "danger" in row]
        if not red_rows:
            continue
        if flat.count("danger") > 1:
            problems.append(f"{name}: опасных действий {flat.count('danger')}")
        if red_rows[-1] != len(rows) - 1:
            problems.append(f"{name}: опасное действие не в последнем ряду")
        for index in red_rows:
            if "success" in rows[index]:
                problems.append(f"{name}: красная кнопка в одном ряду с зелёной")
    assert not problems, "\n".join(problems)


def test_menu_is_short():
    """Главное меню — не свалка: не больше шести кнопок (было одиннадцать)."""
    for name in ("main_menu:new", "main_menu:active", "main_menu:expired"):
        rows = _all_inline_keyboards()[name]
        count = sum(len(row) for row in rows)
        assert count <= 6, f"{name}: {count} кнопок — больше шести"
        assert len(rows) <= 4, f"{name}: {len(rows)} рядов — меню выше четырёх рядов"


# ------------------------------------------------------------------ тексты
def _text_constants() -> dict[str, str]:
    """Все строковые константы texts.py: имя → значение."""
    result: dict[str, str] = {}
    for name in dir(texts):
        if name.startswith("_"):
            continue
        value = getattr(texts, name)
        if isinstance(value, str) and len(value) > 20:
            result[name] = value
    return result


def test_banned_emoji_do_not_come_back():
    """Запрещённый декор не возвращается ни в текст, ни в кнопки."""
    haystack = " ".join(_text_constants().values())
    for row in _all_inline_keyboards().values():
        if not isinstance(row, list):
            continue
        haystack += " " + " ".join(button.text for line in row for button in line)
    found = {emoji: why for emoji, why in BANNED_EMOJI.items() if emoji in haystack}
    assert not found, "вернулся декор: " + "; ".join(f"{k} — {v}" for k, v in found.items())


def test_no_emoji_salad_in_one_message():
    """В одном сообщении не больше шести эмодзи.

    Считаем и «сервисные» глифы: сообщение с десятком значков читается как
    набор картинок, а не как текст.
    """
    problems: list[str] = []
    for name, value in _text_constants().items():
        found = _EMOJI.findall(value)
        if len(found) > EMOJI_PER_MESSAGE:
            problems.append(f"{name}: {len(found)} эмодзи")
    assert not problems, "эмодзи-салат:\n" + "\n".join(problems)


def test_hero_caption_fits_photo_limit():
    """Подпись к hero-фото влезает в лимит caption — 1024 символа.

    Если не влезет, Telegram ответит ошибкой и человек не увидит первый экран:
    у фото нет отдельного ``text``, весь смысл живёт в подписи.
    """
    caption = texts.welcome("Egor")
    assert len(caption) <= 1024, f"подпись hero — {len(caption)} символов"
    assert "{" not in caption, "в подписи остался незаполненный плейсхолдер"
    # Заголовок-оффер, а не «Главное меню»: первая строка продаёт, а не называет экран.
    assert caption.startswith("<b>"), "первая строка hero должна быть заголовком"
    assert "бесплатно" in caption.lower()
    assert "1 устройство" in caption, "в триале одно устройство — обещать три нельзя"


def test_hero_photo_is_in_the_image():
    """Картинка hero лежит внутри пакета — иначе в образе её не будет."""
    assert view.START_HERO.exists(), f"нет файла {view.START_HERO}"
    assert view.START_HERO.parent == BOT_DIR / "assets"
    # 84 КБ при лимите 10 МБ: проверяем, что не подсунули гигантский исходник.
    assert view.START_HERO.stat().st_size < 2_000_000, "картинка hero слишком тяжёлая"


def test_welcome_works_without_name():
    """Имени может не быть (удалённый аккаунт) — бот не должен падать."""
    for name in (None, "", "Egor", "ОченьДлинноеИмяПользователя"):
        caption = texts.welcome(name)
        assert caption and "{" not in caption


def test_menu_text_is_state_not_heading():
    """Меню говорит о состоянии, а не «Главное меню»."""
    for value in (texts.MENU_NO_SUB, texts.MENU_ACTIVE, texts.MENU_EXPIRED):
        assert "главное меню" not in value.lower()
        assert len(value) <= 120


# ------------------------------------------------- фирменные эмодзи (custom emoji)
def test_decorate_turns_markup_into_entities():
    """Разметка и эмодзи уходят сущностями, а не через parse_mode.

    Проверено на живом боте: если отправить сообщение с ``parse_mode`` и своими
    сущностями, Telegram отбрасывает ``custom_emoji``. Поэтому весь текст бота
    переводится в сущности здесь.
    """
    from app.bot import emoji

    text, entities = emoji.decorate("<b>Жирный</b> и <code>код</code>")
    assert text == "Жирный и код"
    kinds = {entity["type"] for entity in entities}
    assert kinds == {"bold", "code"}
    assert "<" not in text and ">" not in text


def test_decorate_replaces_emoji_and_keeps_fallback():
    """Фирменный эмодзи подставляется, но юникод-заменитель остаётся в тексте.

    Заменитель нужен в уведомлениях и у тех, кто не видит набор: сообщение
    должно читаться и без фирменной иконки.
    """
    from app.bot import emoji

    text, entities = emoji.decorate("Оплата получена ✅")
    custom = [entity for entity in entities if entity["type"] == "custom_emoji"]
    assert len(custom) == 1
    assert custom[0]["custom_emoji_id"] == emoji.EMOJI["✅"]
    assert "✅" in text, "юникод-заменитель потерялся"
    # Смещение указывает ровно на эмодзи, а не на соседний символ.
    units = text.encode("utf-16-le")
    start = custom[0]["offset"] * 2
    end = start + custom[0]["length"] * 2
    assert units[start:end].decode("utf-16-le") == "✅"


def test_decorate_offsets_survive_html_tags():
    """Теги не сдвигают смещения: иначе эмодзи уезжает в середину слова."""
    from app.bot import emoji

    text, entities = emoji.decorate("<b>Готово</b>: доступ ✅ выдан")
    # Смещение считаем по видимому тексту, а не по HTML: теги в подсчёт не входят.
    visible = len("Готово: доступ ")
    custom = next(entity for entity in entities if entity["type"] == "custom_emoji")
    assert custom["offset"] == visible
    units = text.encode("utf-16-le")
    assert units[custom["offset"] * 2 : (custom["offset"] + 1) * 2].decode("utf-16-le") == "✅"


def test_decorate_limit_protects_from_emoji_salad():
    """В одном сообщении не больше MAX_PER_MESSAGE фирменных эмодзи."""
    from app.bot import emoji

    _, entities = emoji.decorate("✅" * 40)
    custom = [entity for entity in entities if entity["type"] == "custom_emoji"]
    assert len(custom) == emoji.MAX_PER_MESSAGE


def test_decorate_can_be_switched_off(monkeypatch):
    """Выключатель на случай, если Telegram начнёт отклонять сущности."""
    from app.bot import emoji

    monkeypatch.setenv("CUSTOM_EMOJI_ENABLED", "0")
    text, entities = emoji.decorate("<b>Готово</b> ✅")
    # Эмодзи остаются системными, а разметка всё равно уходит сущностями:
    # без них текст потерял бы жирный шрифт (parse_mode мы не используем).
    assert not [entity for entity in entities if entity["type"] == "custom_emoji"]
    assert [entity["type"] for entity in entities] == ["bold"]
    assert text == "Готово ✅"


def test_emoji_ids_match_created_pack():
    """Идентификаторы в коде совпадают с набором, который создан в Telegram."""
    import json
    from pathlib import Path

    from app.bot import emoji

    ids_file = Path(__file__).resolve().parent.parent / "design/bot/assets/emoji/production/ids.json"
    if not ids_file.exists():  # набор ещё не собирали — проверять нечего
        return
    pack = json.loads(ids_file.read_text(encoding="utf-8"))
    for slot, data in pack.items():
        assert emoji.EMOJI.get(data["fallback"]) == data["id"], f"{slot}: id разошёлся с набором"
