"""Клавиатуры бота: одно место для всей навигации.

**Роли кнопок.** Цвет в Telegram-клиенте задаёт поле ``style``: доступны ровно
три значения — ``primary``, ``success``, ``danger``. Синий ``primary`` мы не
используем: бренд изумрудный, и синяя кнопка рядом с ним даёт раздвоенный стиль
(разбор — ``design/bot/DESIGN-SYSTEM.md``, §3.1.1). Поэтому роли такие:

* ``primary`` / ``success`` — действие, которого мы ждём от человека: зелёное.
    На экране такое действие одно;
* ``danger`` — отмена и отклонение: красное, ровно одно на сообщение, всегда
    последним рядом и никогда рядом с зелёной;
* ``button`` — навигация и всё вторичное: цвет клиента по умолчанию.

Роль — это не только цвет: по ней строится порядок кнопок и проверка «одно
главное действие на экран» (``tests/test_bot_design.py``).
"""

from __future__ import annotations

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.config import get_settings
from app.db.models import Plan

settings = get_settings()


class StyledKeyboardBuilder(InlineKeyboardBuilder):
        """Конструктор клавиатур, который знает про роли кнопок.

        Обычный ``InlineKeyboardBuilder`` уже умеет ``style``, но роли были бы
        размазаны по вызовам. Здесь три именованные роли — и цвет проставляется
        в одном месте, а не в тридцати.
        """

        def _styled(self, style: str | None, text: str, **kwargs):
                """Добавить кнопку с заданным стилем (или без него)."""
                return self.button(text=text, style=style, **kwargs)

        def primary(self, text: str, **kwargs):
                """Главное действие экрана: зелёное."""
                return self._styled(ButtonStyle.SUCCESS, text, **kwargs)

        def success(self, text: str, **kwargs):
                """Подтверждение и оплата: тот же зелёный — это тоже «действие»."""
                return self._styled(ButtonStyle.SUCCESS, text, **kwargs)

        def danger(self, text: str, **kwargs):
                """Отмена и отклонение: красное, ровно одно на сообщение."""
                return self._styled(ButtonStyle.DANGER, text, **kwargs)

        def neutral(self, text: str, **kwargs):
                """Навигация и вторичное: цвет клиента по умолчанию."""
                return self._styled(None, text, **kwargs)


BTN_TRIAL = "Попробовать бесплатно"
BTN_PLANS = "Тарифы"
BTN_MY_SUB = "Моя подписка"
#: Кнопка профиля. Подпись говорит, что внутри: в главном меню профиль занял
#: место «Моей подписки» (иначе стало бы семь кнопок, а дизайн-система держит
#: шесть), и ссылка-подписка живёт именно там — зелёной кнопкой «Моя подписка».
#: Без второго слова человек, шедший за ссылкой, видел бы кнопку «не про то».
BTN_PROFILE = "Профиль и подписка"
BTN_HELP = "Помощь"
BTN_REFERRAL = "Пригласить друга"
BTN_HOWTO = "Как подключить"
BTN_RESERVE = "Если не открывается"
BTN_LEGAL = "Документы и цены"
BTN_GIFT = "Подарить подписку"


def main_menu(has_subscription: bool, is_active: bool, min_price: int = 0) -> InlineKeyboardMarkup:
        """Главное меню: шесть кнопок и ровно одно главное действие.

        Порядок неслучаен: сначала то, за чем человек вернулся (подписка или
        пробный доступ), потом цена, потом «как это включить», и только в конце —
        помощь и резервный профиль. Было одиннадцать кнопок в пяти рядах — стало
        шесть (``design/bot/INVENTORY.md``, топ-1 слабых мест).

        :param min_price: цена самого дешёвого тарифа. Показываем её в кнопке —
                клиент видит стоимость, не открывая раздел с тарифами.
        """
        plans_label = f"{BTN_PLANS} — от {min_price} ₽" if min_price else BTN_PLANS
        kb = StyledKeyboardBuilder()
        if is_active:
                # Профиль занял место «Моя подписка»: он и есть личный кабинет,
                # а статус доступа виден в нём сразу. Иначе кнопок стало бы семь —
                # предел дизайн-системы шесть (tests/test_bot_design.py).
                kb.button(text=BTN_PROFILE, callback_data="profile:show")
                kb.button(text="Продлить", callback_data="plans")
                # Подарок видит только действующий клиент: он уже понял ценность
                # сервиса и решает задачу «что подарить», а не «нужен ли мне доступ».
                if settings.gift_enabled:
                        kb.button(text=BTN_GIFT, callback_data="gift:show")
                kb.button(text=BTN_HOWTO, callback_data="sub:howto")
                kb.button(text=BTN_HELP, callback_data="help")
                kb.button(text=BTN_RESERVE, callback_data="sub:reserve")
                kb.adjust(2, 1, 2, 1)
                return kb.as_markup()

        if has_subscription:
                kb.primary(text="Продлить подписку", callback_data="plans")
        else:
                # Единственное главное действие нового человека — бесплатный доступ.
                kb.primary(text=BTN_TRIAL, callback_data="trial:start")
                kb.button(text=plans_label, callback_data="plans")
        kb.button(text=BTN_PROFILE, callback_data="profile:show")
        kb.button(text=BTN_REFERRAL, callback_data="ref:show")
        kb.button(text=BTN_HOWTO, callback_data="sub:howto")
        kb.button(text=BTN_HELP, callback_data="help")
        if has_subscription:
                kb.adjust(1, 2, 2)
        else:
                kb.adjust(1, 1, 2, 2)
        return kb.as_markup()


def reply_menu() -> ReplyKeyboardMarkup:
        """Постоянное меню снизу — быстрый доступ к главному."""
        return ReplyKeyboardMarkup(
                keyboard=[
                        [KeyboardButton(text=BTN_MY_SUB), KeyboardButton(text=BTN_PROFILE)],
                        [KeyboardButton(text=BTN_PLANS), KeyboardButton(text=BTN_HOWTO)],
                        [KeyboardButton(text=BTN_RESERVE), KeyboardButton(text=BTN_HELP)],
                        [KeyboardButton(text=BTN_LEGAL)],
                ],
                resize_keyboard=True,
                is_persistent=False,
        )


def profile_kb() -> InlineKeyboardMarkup:
        """Кнопки экрана «Мой профиль»: главное действие и навигация.

        Главное — «Моя подписка»: из профиля человек идёт за ссылкой.
        Приглашение, инструкция и помощь нейтральны: это вторичные пути, а две
        зелёные кнопки читались бы как «выбери любую» (design/bot/GUIDE.md, §3).
        """
        kb = StyledKeyboardBuilder()
        kb.primary(text=BTN_MY_SUB, callback_data="sub:show")
        kb.button(text=BTN_REFERRAL, callback_data="ref:show")
        kb.button(text=BTN_HOWTO, callback_data="sub:howto")
        kb.button(text=BTN_HELP, callback_data="help")
        kb.adjust(1, 1, 2)
        return kb.as_markup()


def referral_share_kb(link: str, share_text: str = "") -> InlineKeyboardMarkup:
        """Кнопки экрана «Пригласить друга»: поделиться готовым сообщением.

        Ссылку отдаём через ``t.me/share/url`` — это обычный https-адрес, права
        бота не нужны (та же схема, что в :func:`referral_kb`). Текст для друга
        тоже готовый: Telegram склеивает его со ссылкой в один черновик, и
        человеку не нужно ничего сочинять.
        """
        from urllib.parse import quote

        kb = StyledKeyboardBuilder()
        if link:
                kb.button(
                        text="Поделиться с другом",
                        url=f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}",
                )
        kb.button(text="Мой промокод", callback_data="ref:code")
        kb.button(text="Мои друзья", callback_data="ref:friends")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def plans_kb(
        plans: list[Plan],
        show_stars: bool = False,
        discount_percent: int = 0,
        max_discount_rub: int = 0,
        show_promo_button: bool = False,
) -> InlineKeyboardMarkup:
        """Тарифы. Если есть скидка — она уже в цене на кнопках."""
        from app.services.promo import calc_discount_rub

        kb = StyledKeyboardBuilder()
        for plan in plans:
                discount_rub = calc_discount_rub(plan.price_rub, discount_percent, max_discount_rub)
                price = plan.price_rub - discount_rub
                per_month = round(price / max(1, plan.days) * 30)
                if discount_rub:
                        label = f"{plan.title} — {texts.format_rub(price)} ₽ вместо {texts.format_rub(plan.price_rub)} ₽ "
                elif show_stars and plan.price_stars:
                        label = f"{plan.title} — {texts.format_rub(plan.price_rub)} ₽ или {plan.price_stars} ⭐"
                else:
                        label = f"{plan.title} — {texts.format_rub(plan.price_rub)} ₽ ({texts.format_rub(per_month)} ₽/мес)"
                kb.button(text=label, callback_data=f"plan:{plan.id}")
        if show_promo_button:
                kb.button(text="У меня есть промокод", callback_data="promo:enter")
        kb.button(text="← Назад", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def plans_button_kb() -> InlineKeyboardMarkup:
        """Кнопка «выбрать тариф» для приветствия приглашённого."""
        kb = StyledKeyboardBuilder()
        kb.primary(text="Выбрать тариф", callback_data="plans")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def providers_kb(
        plan_id: int,
        providers: list[tuple[str, str]],
        sbp_soon: bool = False,
) -> InlineKeyboardMarkup:
        """Способы оплаты для выбранного тарифа.

        :param sbp_soon: оплата ещё не подключена — показываем СБП и ведём на
                заглушку «скоро», а не на счёт.
        """
        kb = StyledKeyboardBuilder()
        for code, title in providers:
                kb.button(text=title, callback_data=f"pay:{plan_id}:{code}")
        if sbp_soon:
                kb.button(text=texts.SBP_SOON_BTN, callback_data=f"sbp:soon:{plan_id}")
        kb.button(text="У меня есть промокод", callback_data="promo:enter")
        kb.button(text="← К тарифам", callback_data="plans")
        kb.adjust(1)
        return kb.as_markup()


def referral_kb(share_url: str) -> InlineKeyboardMarkup:
        """Кнопки экрана «Пригласи друга»: поделиться, показать код, список друзей."""
        from urllib.parse import quote

        kb = StyledKeyboardBuilder()
        if share_url:
                text = quote(
                        f"Забирай скидку {settings.referral_discount_percent}% на первое подключение",
                        safe="",
                )
                kb.button(
                        text="Поделиться ссылкой",
                        url=f"https://t.me/share/url?url={quote(share_url, safe='')}&text={text}",
                )
        kb.button(text="Мой промокод", callback_data="ref:code")
        kb.button(text="Мои друзья", callback_data="ref:friends")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def manual_order_kb(order_id: int) -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        kb.button(text="Оплатил, доступа нет", callback_data=f"order:manual:{order_id}")
        kb.danger(text="← Отменить заказ", callback_data=f"order:cancel:{order_id}")
        kb.adjust(1)
        return kb.as_markup()


def crypto_order_kb(order_id: int, pay_url: str) -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        kb.primary(text="Оплатить", url=pay_url)
        kb.button(text="Проверить оплату", callback_data=f"order:check:{order_id}")
        kb.danger(text="← Отменить", callback_data=f"order:cancel:{order_id}")
        kb.adjust(1)
        return kb.as_markup()


def stars_order_kb(order_id: int, pay_url: str, reseller_url: str = "", stars: int = 0) -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        kb.primary(text="Оплатить звёздами", url=pay_url)
        if reseller_url:
                label = f"Купить {stars} звёзд" if stars else "Купить звёзды"
                kb.button(text=label, url=reseller_url)
        kb.danger(text="← Отменить", callback_data=f"order:cancel:{order_id}")
        kb.adjust(1)
        return kb.as_markup()


def connect_kb(sub_url: str) -> InlineKeyboardMarkup:
        """Кнопки подключения: одно нажатие — профиль уже в приложении.

        ВАЖНО: Telegram **запрещает** нестандартные схемы (``happ://``,
        ``v2rayng://``, ``hiddify://``) в inline-кнопках — на такую клавиатуру он
        отвечает ``Bad Request: Unsupported URL protocol``, и сообщение с кнопками
        не уходит вовсе (проверено на живом боте 06.10.2026). Поэтому кнопки ведут
        на нашу https-страницу ``/connect/<token>``: она открывает приложение по
        схеме, а если приложение не установлено — показывает ссылку для копирования.

        Имя профиля клиенты берут из фрагмента ссылки (``#Kometa``) — страница
        подключения подставляет его сама.
        """
        from app.config import get_settings

        kb = StyledKeyboardBuilder()
        token = sub_url.rstrip("/").rsplit("/", 1)[-1] if sub_url else ""
        base = (get_settings().public_base_url or "").rstrip("/")

        if token and base:
                page = f"{base}/connect/{token}"
                kb.button(text="Подключить в Happ", url=f"{page}?app=happ")
                kb.button(text="Подключить в v2rayNG", url=f"{page}?app=v2rayng")
                kb.button(text="Подключить в Hiddify", url=f"{page}?app=hiddify")
        if sub_url:
                kb.button(text="Скопировать ссылку", callback_data="sub:copy")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1, 1, 1, 1, 1)
        return kb.as_markup()


def subscription_kb(has_panel_user: bool) -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        # Главное действие экрана — забрать ссылку. «Продлить» рядом нейтрально:
        # две зелёные кнопки читаются как «выбери любую», а продление здесь —
        # запасной путь, а не причина, по которой экран открыли.
        kb.primary(text="Ссылка-подписка", callback_data="sub:link")
        kb.button(text="Продлить", callback_data="plans")
        kb.button(text="Обновить", callback_data="sub:refresh")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1 if not has_panel_user else 2, 1, 1)
        return kb.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        kb.button(text="← В меню", callback_data="menu:main")
        return kb.as_markup()


def support_kb() -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        if settings.support_username:
                kb.primary(text="Написать в поддержку", url=f"https://t.me/{settings.support_username.lstrip('@')}")
        if settings.channel_url:
                kb.button(text="Наш канал", url=settings.channel_url)
        kb.button(text=BTN_LEGAL, callback_data="legal:show")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def legal_kb(
        privacy_url: str = "",
        terms_url: str = "",
) -> InlineKeyboardMarkup:
        """Документы и цены: отдельные кнопки, доступные в любой момент.

        Банк-партнёр проверяет именно это: политика, соглашение, контакт поддержки
        и актуальные тарифы должны открываться у клиента в один тап. Если ссылка
        на документ ещё не опубликована, кнопка показывает текст внутри бота —
        доступность важнее ссылки.
        """
        kb = StyledKeyboardBuilder()
        kb.button(text=texts.LEGAL_PRICING_BTN, callback_data="legal:pricing")
        kb.button(
                text=texts.LEGAL_PRIVACY_BTN,
                url=privacy_url or None,
                callback_data=None if privacy_url else "legal:privacy",
        )
        kb.button(
                text=texts.LEGAL_TERMS_BTN,
                url=terms_url or None,
                callback_data=None if terms_url else "legal:terms",
        )
        kb.button(text=texts.LEGAL_SUPPORT_BTN, callback_data="legal:support")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def docs_back_kb() -> InlineKeyboardMarkup:
        """Кнопки под прайсом и документами: тарифы, назад в раздел, в меню."""
        kb = StyledKeyboardBuilder()
        kb.button(text=BTN_PLANS, callback_data="plans")
        kb.button(text=BTN_LEGAL, callback_data="legal:show")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def admin_order_kb(order_id: int, *, namespace: str = "admin") -> InlineKeyboardMarkup:
        """Кнопки решения по заявке.

        ``namespace`` разводит обработчиков: кнопки из основного бота приходят с
        ``admin:…``, а из бота уведомлений — с ``adm:…``. Иначе тап по кнопке в
        чате уведомлений попал бы в диспетчер другого бота и «ничего не произошло».
        """
        kb = StyledKeyboardBuilder()
        kb.success(text="Подтвердить", callback_data=f"{namespace}:confirm:{order_id}")
        kb.danger(text="Отклонить", callback_data=f"{namespace}:reject:{order_id}")
        kb.adjust(2)
        return kb.as_markup()


def admin_panel_kb(pending_count: int = 0, nodes_ok: bool = True, panel_url: str | None = None) -> InlineKeyboardMarkup:
        kb = StyledKeyboardBuilder()
        kb.button(text=f"Заявки ({pending_count})", callback_data="admin:orders")
        kb.button(text="Статистика", callback_data="admin:stats")
        kb.button(text="Ноды", callback_data="admin:nodes")
        if panel_url:
                kb.button(text="Веб-панель", url=panel_url)
        kb.adjust(1)
        return kb.as_markup()


# ------------------------------------------------------------------ подарки
def gifts_kb(plans: list[tuple[str, str, int]]) -> InlineKeyboardMarkup:
        """Выбор срока подарочного сертификата.

        :param plans: список (код, название, цена) — цена уже с наценкой за подарок.
        """
        kb = StyledKeyboardBuilder()
        for code, title, price in plans:
                kb.button(text=f"{title} — {price} ₽", callback_data=f"gift:plan:{code}")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()


def gift_recipient_kb() -> InlineKeyboardMarkup:
        """Шаг «кому дарим»: можно указать имя или сразу получить ссылку."""
        kb = StyledKeyboardBuilder()
        kb.button(text="Пропустить — пришлю сам", callback_data="gift:skip_name")
        kb.button(text="← Назад", callback_data="gift:show")
        kb.adjust(1)
        return kb.as_markup()


def gift_pay_kb(order_id: int, providers: list[tuple[str, str]]) -> InlineKeyboardMarkup:
        """Способы оплаты подарочного сертификата."""
        kb = StyledKeyboardBuilder()
        for code, title in providers:
                kb.button(text=title, callback_data=f"gift:pay:{order_id}:{code}")
        kb.button(text="← В меню", callback_data="menu:main")
        kb.adjust(1)
        return kb.as_markup()
