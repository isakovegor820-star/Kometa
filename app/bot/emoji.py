"""Фирменные эмодзи бота: подстановка custom emoji вместо системных смайликов.

Зачем. Системные эмодзи рисует клиент: на iPhone они объёмные, на Android
плоские, на десктопе третьи — «фирменный стиль» рассыпается на трёх платформах.
Свой набор (``custom_emoji``) выглядит одинаково везде и совпадает с брендом.

Что здесь есть:

* ``EMOJI`` — карта «юникод-знак → ``custom_emoji_id``». Идентификаторы получены
  из набора, который создан ботом и принадлежит владельцу
  (``https://t.me/addemoji/kometa_8958117679_by_kometavpnservise_bot``);
* :func:`decorate` — превращает обычный текст с эмодзи в текст + сущности
  ``custom_emoji``. Если Telegram сущности не примет, бот отправит текст как
  есть: подстановка никогда не должна ломать сообщение.

Почему подстановка на отправке, а не в текстах. Эмодзи остаются в ``texts.py``
как обычные знаки: если набор отключить или Telegram откажет, сообщения
по-прежнему читаются. Замена — косметика на выходе, а не часть текста.

Важное ограничение Telegram: кастомный эмодзи **нельзя** вставить в подпись
inline-кнопки (там плоская строка). Для кнопок есть отдельное поле
``icon_custom_emoji_id`` — одна ведущая иконка, см. ``keyboards.icon``.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

#: Юникод-заменитель → идентификатор фирменного эмодзи.
#: Юникод обязателен: Telegram показывает его в уведомлениях и у тех, кто
#: не видит фирменный набор (например, переслал сообщение без Premium).
EMOJI: dict[str, str] = {
    "🎁": "5350747772926605160",  # gift — пробный доступ и подарки
    "⭐": "5350629124455051333",  # star — тарифы
    "⭐\ufe0f": "5350629124455051333",
    "🎟": "5350621586787444773",  # ticket — промокоды
    "💳": "5350583812550079563",  # card — оплата
    "💰": "5350539690351046762",  # wallet — баланс
    "💎": "5350498149427357927",  # crown — премиум-тариф
    "✅": "5350403827650573240",  # check — готово
    "❌": "5350381210352798218",  # cross — ошибка
    "⚠️": "5350288499188739041",  # warn — внимание
    "⚠️\ufe0f": "5350288499188739041",
    "⏳": "5350674573798973999",  # clock — срок
    "🔄": "5350699364350208778",  # refresh — обновить
    "🔒": "5350371404942454217",  # lock — приватность
    "🛡": "5350587974373389526",  # shield — защита
    "🚀": "5350459606390843607",  # rocket — скорость
    "🛟": "5350364966786478356",  # lifebuoy — поддержка и резерв
    "📱": "5350800016908790334",  # phone — приложение
    "📈": "5350769793223929610",  # growth — рост
    "👥": "5350522574906371276",  # users — друзья
    "📋": "5350572040044719438",  # clipboard — скопировать
    "🔗": "5350622995536718317",  # link — ссылка
    "🛒": "5350533368159184764",  # cart — купить
    "📣": "5350585337263469438",  # megaphone — канал
    "⌛": "5350832413847107276",  # hourglass — истекает
    "🧭": "5350780350253545051",  # compass — локации
    "❓": "5350354431231697886",  # qmark — помощь
    "✨": "5350608104885103347",  # kometa — знак бренда
}

#: Сколько эмодзи допустимо подставить в одно сообщение. Telegram ограничивает
#: число сущностей; заодно это защита от «салата» — сообщение с десятком
#: фирменных знаков читается как набор картинок, а не как текст.
MAX_PER_MESSAGE = 12


def enabled() -> bool:
    """Включена ли подстановка.

    Выключатель на случай, если Telegram начнёт отклонять сущности: тогда
    достаточно убрать переменную окружения, а не править код.
    """
    import os

    return os.environ.get("CUSTOM_EMOJI_ENABLED", "1").strip().lower() not in {"0", "false", "no"}


@lru_cache(maxsize=512)
def _longest_first() -> tuple[str, ...]:
    """Ключи, отсортированные от длинных к коротким.

    Нужно из-за ``⭐️`` (звезда + модификатор): если проверять ``⭐`` раньше,
    от пары останется «висячий» модификатор.
    """
    return tuple(sorted(EMOJI, key=len, reverse=True))


#: HTML-теги бота → типы сущностей Telegram. Набор маленький и закрытый:
#: всё остальное (ссылки) в текстах бота не встречается.
TAGS: dict[str, str] = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "s": "strikethrough",
    "strike": "strikethrough",
    "del": "strikethrough",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
}

#: Теги, которые в текстах бота не разметка, а часть сообщения (например
#: примеры команд в справке администратора). Их не вырезаем.
PLAIN_TAGS = {"tg-user", "tg-emoji"}


def decorate(html: str) -> tuple[str, list[dict]]:
    """Превратить HTML бота в текст + сущности Telegram, включая фирменные эмодзи.

    Почему не ``parse_mode="HTML"``. Проверено на живом боте: если отправить
    сообщение с ``parse_mode`` и своими сущностями, Telegram **отбрасывает**
    ``custom_emoji`` и оставляет только те, что разобрал сам. Поэтому разметку
    переводим в сущности здесь, а ``parse_mode`` при отправке не задаём.

    :param html: текст с HTML-разметкой (как в ``texts.py``).
    :return: ``(текст, сущности)``.
    """
    if not html:
        return html, []

    text: list[str] = []
    entities: list[dict] = []
    stack: list[tuple[str, int]] = []      # (тип сущности, начало) в UTF-16
    offset = 0
    index = 0
    length = len(html)
    emoji_left = MAX_PER_MESSAGE if enabled() else 0
    keys = _longest_first()

    def close(kind: str, start: int, end: int) -> None:
        if end > start:
            entities.append({"type": kind, "offset": start, "length": end - start})

    while index < length:
        char = html[index]

        if char == "<":
            close_at = html.find(">", index)
            if close_at == -1:
                text.append(html[index:])
                break
            tag = html[index + 1 : close_at].strip()
            index = close_at + 1
            name = tag.lstrip("/").split()[0].lower() if tag else ""
            if name in PLAIN_TAGS:
                text.append(html[index - len(tag) - 2 : index])
                continue
            kind = TAGS.get(name)
            if kind is None:
                continue                      # неизвестный тег просто убираем
            if tag.startswith("/"):
                for position in range(len(stack) - 1, -1, -1):
                    if stack[position][0] == kind:
                        open_kind, open_at = stack.pop(position)
                        close(open_kind, open_at, offset)
                        break
            else:
                stack.append((kind, offset))
            continue

        if emoji_left > 0:
            matched = next((key for key in keys if html.startswith(key, index)), None)
            if matched:
                size = _utf16_len(matched)
                text.append(matched)
                entities.append(
                    {
                        "type": "custom_emoji",
                        "offset": offset,
                        "length": size,
                        "custom_emoji_id": EMOJI[matched],
                    }
                )
                offset += size
                index += len(matched)
                emoji_left -= 1
                continue

        text.append(char)
        offset += _utf16_len(char)
        index += 1

    # Незакрытые теги: закрываем в конце, чтобы разметка не пропала молча.
    while stack:
        kind, open_at = stack.pop()
        close(kind, open_at, offset)

    # Blockquote в Telegram должен начинаться с начала строки. Если тег стоял
    # посреди абзаца, сдвигаем начало к началу строки — иначе клиент его не
    # отрисует, а Telegram может отклонить сообщение.
    plain = "".join(text)
    for entity in entities:
        if entity["type"] != "blockquote":
            continue
        start = _char_index(plain, entity["offset"])
        line_start = plain.rfind("\n", 0, start) + 1
        shift = _utf16_len(plain[line_start:start])
        entity["offset"] -= shift
        entity["length"] += shift

    entities.sort(key=lambda item: item["offset"])
    return plain, entities


def _char_index(text: str, utf16_offset: int) -> int:
    """Индекс в строке Python по смещению в кодовых единицах UTF-16."""
    if utf16_offset <= 0:
        return 0
    units = 0
    for index, char in enumerate(text):
        units += _utf16_len(char)
        if units > utf16_offset:
            return index
    return len(text)


def _utf16_len(text: str) -> int:
    """Длина строки в кодовых единицах UTF-16 — так Telegram считает смещения."""
    return len(text.encode("utf-16-le")) // 2


def teletype(text: str) -> str:
    """Готовая строка с подставленными фирменными эмодзи (для тестов и логов).

    Возвращает текст, где юникод-знаки заменены на «``:brand:``» — так видно,
    что подстановка сработала, не заглядывая в сущности.
    """
    result = text
    for key in _longest_first():
        result = result.replace(key, ":brand:")
    return result
