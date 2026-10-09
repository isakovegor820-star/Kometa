"""Отрисовка экранов бота: где текст, где картинка, где кнопки.

Зачем отдельный модуль. Главный экран бота — не текст, а **фото с подписью и
кнопками**: картинка задаёт тон, подпись объясняет, кнопки предлагают
(разбор — ``design/bot/GUIDE.md``, раздел 4). Раньше это было раскидано по
хендлерам, а картинка не использовалась вообще.

Здесь одна точка: «что показать» → «как показать». Хендлер описывает состояние
(``MenuView``), а этот модуль решает, отправить фото или текст.

Про картинку. Файл лежит в ``app/bot/assets/`` — внутри пакета, поэтому в образ
он попадает тем же ``COPY app ./app`` (проверено по ``Dockerfile``): отдельная
раздача статики не нужна. Telegram при первой отправке загружает файл и
возвращает ``file_id`` — его мы запоминаем в памяти процесса и дальше шлём
ссылкой: это быстрее и не тратит трафик. После перезапуска кэш пустой, первая
отправка снова загрузит файл — это нормально.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup

from app.bot import emoji

logger = logging.getLogger(__name__)

#: Каталог с картинками бота. Лежит рядом с кодом, чтобы попадать в образ.
ASSETS = Path(__file__).resolve().parent / "assets"

#: Баннер приветствия 16:9 (1280×720, 84 КБ). Собран под /start: знак сверху,
#: «Добро пожаловать» и три факта, а место под кнопки оставлено свободным.
START_HERO = ASSETS / "start-hero.jpg"


@dataclass(frozen=True, slots=True)
class MenuView:
    """Что показать на экране меню.

    :param text: текст сообщения (он же подпись, если есть ``photo``);
    :param markup: inline-клавиатура экрана;
    :param photo: путь к картинке, если экран показываем фото-сообщением.
        ``None`` — обычный текст (ответ на кнопку, повторный вход в меню).
    """

    text: str
    markup: InlineKeyboardMarkup
    photo: Path | None = None


#: file_id картинки в Telegram: заполняется после первой успешной отправки.
_photo_file_ids: dict[str, str] = {}


def photo_file_id(path: Path) -> str | None:
    """Известный Telegram ``file_id`` для файла, если он уже загружался."""
    return _photo_file_ids.get(path.name)


def remember_photo(path: Path, message: Message) -> None:
    """Запомнить ``file_id`` после успешной отправки — чтобы не грузить файл снова."""
    photos = getattr(message, "photo", None) or []
    if not photos:
        return
    # Берём самый крупный размер: он и уйдёт в кэш клиента.
    best = max(photos, key=lambda item: (item.width or 0) * (item.height or 0))
    if best.file_id:
        _photo_file_ids[path.name] = best.file_id


def hero_photo(path: Path | None = None) -> str | FSInputFile:
    """Картинка hero-экрана: ``file_id``, если уже загружали, иначе сам файл.

    Нужна там, где сообщение отправляет не ``Message.answer``, а бот напрямую
    (например из callback, когда исходное сообщение недоступно).
    """
    target = path or START_HERO
    cached = photo_file_id(target)
    return cached if cached else FSInputFile(target)


async def send_view(
    message: Message,
    view: MenuView,
    *,
    reply_markup: ReplyKeyboardMarkup | None = None,
) -> Message | None:
    """Показать экран: фото с подписью или текст. Затем — постоянное меню снизу.

    Ошибку отправки картинки не пробрасываем: если файла нет или Telegram его
    не принял, человек всё равно должен увидеть меню. Текст важнее картинки.

    :param reply_markup: постоянная клавиатура снизу. Ставится отдельным
        сообщением, потому что в Telegram у одного сообщения может быть только
        один ``reply_markup``: и inline-кнопки, и нижнее меню в одно не влезают.
    """
    # Фирменные эмодзи подставляем здесь — в одном месте на все экраны.
    # Если Telegram сущности не примет, уйдёт обычный текст: см. emoji.decorate.
    text, entities = emoji.decorate(view.text)
    sent: Message | None = None
    if view.photo is None:
        sent = await message.answer(
            text, reply_markup=view.markup, entities=entities or None, parse_mode=None
        )
    else:
        photo: str | FSInputFile
        cached = photo_file_id(view.photo)
        if cached:
            photo = cached
        elif view.photo.exists():
            photo = FSInputFile(view.photo)
        else:  # pragma: no cover — только если ассет потеряли при сборке образа
            logger.warning("нет картинки экрана: %s — показываю текстом", view.photo)
            photo = None  # type: ignore[assignment]
        if photo is None:
            sent = await message.answer(
            text, reply_markup=view.markup, entities=entities or None, parse_mode=None
        )
        else:
            try:
                sent = await message.answer_photo(
                    photo,
                    caption=text,
                    caption_entities=entities or None,
                    reply_markup=view.markup,
                    parse_mode=None,
                )
            except Exception:  # noqa: BLE001 — картинка не должна ломать экран
                logger.exception("не удалось отправить картинку экрана — показываю текстом")
                sent = await message.answer(
            text, reply_markup=view.markup, entities=entities or None, parse_mode=None
        )
            else:
                if isinstance(photo, FSInputFile):
                    remember_photo(view.photo, sent)

    if reply_markup is not None:
        await message.answer(texts_menu_hint(), reply_markup=reply_markup)
    return sent


async def edit_screen(
    message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    entities: list[dict] | None = None,
    disable_web_page_preview: bool = True,
) -> None:
    """Заменить текущий экран бота: подпись у фото, текст у обычного сообщения.

    Почему не ``edit_text`` напрямую. Главный экран — **фото с подписью**
    (hero), и на правку текста такого сообщения Telegram отвечает
    ``Bad Request: there is no text in the message to edit``. Из-за этого кнопки
    под hero («Пригласить друга», «Профиль и подписка», «Помощь», «Тарифы»…)
    выглядели мёртвыми: нажатие не давало ничего, а в лог уходила ошибка
    (боевой лог 09.10.2026, 08:43 — пять таких подряд).

    Заодно закрыты две соседние особенности Telegram, из-за которых кнопка тоже
    «не работает»:

    * повторное нажатие той же кнопки → ``message is not modified`` — это не
      ошибка, экран уже такой, просто выходим;
    * сообщение уже недоступно для правки (``message to edit not found``) —
      показываем экран новым сообщением, а не молчим.

    Подпись есть у фото, видео, документа и гифки — ориентируемся на неё, а не
    только на ``photo``.
    """
    as_caption = getattr(message, "caption", None) is not None and getattr(message, "text", None) is None
    try:
        if as_caption:
            await message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                caption_entities=entities or None,
                parse_mode=None,
            )
        else:
            await message.edit_text(
                text,
                reply_markup=reply_markup,
                entities=entities or None,
                disable_web_page_preview=disable_web_page_preview,
                parse_mode=None,
            )
    except TelegramBadRequest as exc:
        reason = str(exc).lower()
        if "message is not modified" in reason:
            return
        logger.info("Экран не отредактировался (%s) — отправляю новым сообщением", exc)
        await message.answer(
            text,
            reply_markup=reply_markup,
            entities=entities or None,
            disable_web_page_preview=disable_web_page_preview,
            parse_mode=None,
        )


async def edit_view(call, markup_text: str, keyboard) -> bool:
    """Заменить текст сообщения, сохранив фирменные эмодзи.

    Отдельная функция нужна потому, что ``edit_text`` не проходит через
    :func:`send_view`, а эмодзи в меню должны быть одинаковыми и там.
    """
    text, entities = emoji.decorate(markup_text)
    await edit_screen(call.message, text, keyboard, entities=entities or None)
    return True


def texts_menu_hint() -> str:
    """Короткая подпись под постоянным меню снизу.

    Отдельного сообщения «Быстрое меню 👇» больше нет: клавиатура ставится
    сразу под hero, а строка нужна, только чтобы человек понял, что это меню.
    """
    from app.bot import texts

    return texts.QUICK_MENU_HINT
