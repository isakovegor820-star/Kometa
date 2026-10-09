"""Экран «Мой профиль»: что клиент видит и совпадает ли это с настройками.

Проверяем три вещи:

* карточка собирается из фактов — подписки, тарифа, последнего платежа,
  приглашений (а не из красивых слов);
* обещания берутся из настроек: скидка за друга, дни за приглашение, лимит
  наград в месяц, сроки триала (как в tests/test_marketing_texts_consistency.py);
* честность: «без ограничений» появляется только при ``traffic_limit_gb == 0``,
  а «локации доступны» — только если проба это подтвердила. Ровно на обратном
  поведении владелец поймал систему 08.10.2026 (docs/ПРОФИЛЬ-И-РЕФЕРАЛКА.md).

Плюс дизайн-правила экрана: одно зелёное действие, подписи ≤ 30 символов, без
эмодзи в кнопках.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.bot import keyboards, texts
from app.bot.handlers import profile as profile_handler
from app.config import get_settings
from app.db.models import Node, Order
from app.services import orders, profile as profile_service, subscriptions
from tests.fakes import make_update

#: Тот же объект настроек, что держат модули приложения: они читают его один раз
#: при импорте — значит, и сверять обещания нужно с ним.
settings = get_settings()

#: Эмодзи-диапазоны — те же, что в tests/test_bot_design.py: стрелки не считаем.
EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2300-\u27BF\u2B00-\u2BFF]")

#: Полоса остатка срока. Формально `█` и `░` попадают в диапазон U+2300–U+27BF
#: (блоки и рамки), но это не эмодзи, а типографские знаки: дизайн-система
#: требует именно такую полосу (design/bot/GUIDE.md, «прогресс и метрика»).
BAR_GLYPHS = "█░"


async def make_paid_subscription(session, panel, tg_id: int):
    """Человек с оплаченным тарифом — на нём проверяем блок «Подключение»."""
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    plan = (await orders.list_plans(session))[0]
    sub = await subscriptions.activate_plan(session, user, plan, panel)
    return user, plan, sub


# ------------------------------------------------------------------ что видит клиент
async def test_profile_command_shows_card_without_subscription(bot, dispatcher, session):
    """Экран есть и без подписки: оффер, лимиты триала и что делать дальше."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9001))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/profile", user_id=9001))

    text = bot.session.all_text()
    assert "Мой профиль" in text
    assert "Статус: <b>нет доступа</b>" in text
    assert "С нами с" in text
    assert f"Первые <b>{settings.trial_days} дня</b> бесплатно" in text
    assert "Карта не нужна" in text
    assert keyboards.BTN_TRIAL in text, "человек должен видеть, что нажать дальше"
    # Триал ограничен: «без ограничений» здесь было бы неправдой.
    assert "без ограничений" not in text


async def test_profile_button_from_reply_menu_works(bot, dispatcher, session):
    """Кнопка «Мой профиль» в нижней клавиатуре открывает тот же экран."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9002))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(keyboards.BTN_PROFILE, user_id=9002))

    assert "Мой профиль" in bot.session.all_text()


async def test_profile_shows_trial_status_and_real_limits(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=9003))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9003))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="profile:show", user_id=9003))

    user = await subscriptions.get_user_by_tg(session, 9003)
    sub = await subscriptions.get_subscription(session, user.id)
    text = bot.session.all_text()
    assert "Статус: <b>пробный</b>" in text
    # Лимиты — из самой подписки, а не из настроек: так тест ловит расхождение.
    assert f"Устройств: до {sub.devices_limit}" in text
    assert f"из {sub.traffic_limit_gb} ГБ" in text
    assert "без ограничений" not in text
    # Полоса остатка срока — и рядом цифра, иначе полоса ничего не значит.
    assert "<code>" in text and "остаток срока" in text


async def test_profile_shows_plan_and_last_payment(session, panel, bot, dispatcher):
    user, plan, _sub = await make_paid_subscription(session, panel, 9010)
    session.add(
        Order(
            user_id=user.id,
            plan_id=plan.id,
            amount_rub=plan.price_rub,
            base_amount_rub=plan.price_rub,
            provider="stars",
            status="paid",
        )
    )
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/profile", user_id=9010))

    text = bot.session.all_text()
    assert "Статус: <b>активен</b>" in text
    assert f"Тариф: <b>{plan.title}</b>" in text
    assert f"Последняя оплата: <b>{texts.format_rub(plan.price_rub)} ₽</b>" in text
    assert "Трафик: без ограничений" in text


async def test_profile_expired_explains_how_to_come_back(session, panel, bot, dispatcher):
    _user, _plan, sub = await make_paid_subscription(session, panel, 9020)
    sub.status = "expired"
    sub.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/profile", user_id=9020))

    text = bot.session.all_text()
    assert "Статус: <b>истёк</b>" in text
    assert "доступ вернётся сразу после оплаты" in text


async def test_profile_forever_subscription_says_forever(session, panel, bot, dispatcher):
    _user, _plan, sub = await make_paid_subscription(session, panel, 9030)
    sub.expires_at = datetime.now(timezone.utc) + timedelta(days=365 * 20)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/profile", user_id=9030))

    text = bot.session.all_text()
    assert "бессрочно" in text
    assert "остаток срока" not in text, "для бессрочной подписки полоса не нужна"


# ------------------------------------------------------------------ честность цифр
def test_profile_card_promises_match_settings():
    """Скидка, дни и лимит наград в профиле — из настроек, а не из головы."""
    text = texts.profile_card(
        name="Аня",
        status="active",
        expires="07.11.2026",
        days_left=30,
        bar="█" * 10,
        devices=3,
        traffic_limit_gb=0,
        rewards_left=settings.referral_max_rewards_per_month,
        max_rewards=settings.referral_max_rewards_per_month,
        percent=settings.referral_discount_percent,
        referrer_days=settings.referral_bonus_days_referrer,
    )
    plain = re.sub("<[^>]+>", "", text)

    assert f"скидка {settings.referral_discount_percent}%" in plain
    assert f"+{settings.referral_bonus_days_referrer} дн." in plain
    assert f"из {settings.referral_max_rewards_per_month}" in plain
    assert "без ограничений" in plain


def test_profile_offer_uses_trial_settings():
    text = texts.profile_card(
        name="Тест",
        status="none",
        devices=settings.trial_devices,
        traffic_limit_gb=settings.trial_gb,
        trial_days=settings.trial_days,
        trial_btn=keyboards.BTN_TRIAL,
    )

    assert f"Первые <b>{settings.trial_days} дня</b> бесплатно" in text
    assert keyboards.BTN_TRIAL in text


def test_traffic_is_unlimited_only_when_limit_is_zero():
    """«Без ограничений» — только по факту: иначе лимит, и без выдуманного расхода."""
    limited_without_data = texts.profile_card(
        name="Тест", status="trial", traffic_limit_gb=10, traffic_used_gb=None
    )
    assert "без ограничений" not in limited_without_data
    assert "Трафик: до 10 ГБ" in limited_without_data

    limited_with_data = texts.profile_card(
        name="Тест", status="trial", traffic_limit_gb=10, traffic_used_gb=2.5
    )
    assert "2,5 ГБ из 10 ГБ" in limited_with_data

    unlimited = texts.profile_card(name="Тест", status="active", traffic_limit_gb=0)
    assert "Трафик: без ограничений" in unlimited


def test_profile_message_has_few_emoji():
    """В карточке не больше шести эмодзи: иконка на блок, а не на строку."""
    text = texts.profile_card(
        name="Аня",
        username="anya",
        status="active",
        expires="07.11.2026",
        days_left=30,
        bar="█" * 10,
        locations_line=texts.profile_locations_line(ok=["Германия"], measured=True),
        rewards_left=5,
        max_rewards=10,
    )

    emoji = [glyph for glyph in EMOJI.findall(text) if glyph not in BAR_GLYPHS]
    assert len(emoji) <= 6, f"эмодзи-салат: {''.join(emoji)}"


# ------------------------------------------------------------------ локации в профиле
class FakeNode:
    """Мини-нода для проверки строки локаций: те же поля, что у app.db.models.Node."""

    def __init__(
        self,
        title: str,
        *,
        ok: bool | None = None,
        stage: str = "",
        ms: int = 0,
        channel: str = "main",
    ) -> None:
        self.title = title
        self.channel = channel
        self.last_probe_ok = bool(ok)
        self.last_probe_stage = stage
        self.last_probe_ms = ms
        self.last_probe_at = datetime.now(timezone.utc) if ok is not None else None


def test_locations_line_lists_all_nodes_when_every_probe_is_ok():
    line = profile_service.locations_line(
        [FakeNode("Германия", ok=True, ms=40), FakeNode("Финляндия", ok=True), FakeNode("Нидерланды", ok=True)]
    )

    for title in ("Германия", "Финляндия", "Нидерланды"):
        assert title in line
    assert "доступны" in line


def test_locations_line_names_the_node_that_fails():
    line = profile_service.locations_line(
        [
            FakeNode("Германия", ok=True, ms=40),
            FakeNode("Финляндия", ok=False, stage="tcp"),
            FakeNode("Нидерланды", ok=True, ms=80),
        ]
    )

    assert "Финляндия" in line and "не отвечает" in line
    assert "Германия" in line and "Нидерланды" in line


def test_locations_line_never_claims_available_without_measurement():
    """Нет замеров — нет слова «доступны»: честное «статус появится»."""
    line = profile_service.locations_line([FakeNode("Германия"), FakeNode("Финляндия"), FakeNode("Нидерланды")])

    assert "доступны" not in line
    assert "статус появится после первой проверки" in line
    for title in ("Германия", "Финляндия", "Нидерланды"):
        assert title in line


def test_locations_line_without_nodes_is_empty():
    assert profile_service.locations_line([]) == ""


async def test_locations_line_is_hidden_without_access(session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9040, first_name="Без доступа")
    session.add(
        Node(
            code="de-1",
            title="Германия",
            host="10.0.0.7",
            last_probe_at=datetime.now(timezone.utc),
            last_probe_ok=True,
            last_probe_ms=40,
        )
    )
    await session.flush()

    card = await profile_service.build_card(session, user, panel=panel)

    assert card.locations_line == "", "без доступа смотреть нечего"


async def test_profile_never_shows_node_internals(session, panel):
    """Ни служебных кодов, ни адресов нод: клиенту это ничего не говорит."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9041, first_name="Клиент")
    await subscriptions.start_trial(session, user, panel)
    session.add(
        Node(
            code="nl-secret-1",
            title="Нидерланды",
            host="10.0.0.7",
            last_probe_at=datetime.now(timezone.utc),
            last_probe_ok=True,
            last_probe_ms=55,
        )
    )
    await session.flush()

    card = await profile_service.build_card(session, user, panel=panel)
    text = profile_handler.render_card(card)

    assert "Нидерланды" in text
    assert "nl-secret-1" not in text
    assert "10.0.0.7" not in text


# ------------------------------------------------------------------ экран «Локации»
async def test_locations_screen_shows_country_channel_and_probe(session, panel, bot, dispatcher):
    """Страна, канал и честный вердикт пробы. Ни служебных кодов, ни портов."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9050))
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Node(
                code="de-internal",
                title="Германия",
                host="10.0.0.7",
                channel="main",
                last_probe_at=now,
                last_probe_ok=True,
                last_probe_ms=42,
            ),
            Node(
                code="nl-internal",
                title="Нидерланды",
                host="10.0.0.8",
                channel="reserve",
                last_probe_at=now,
                last_probe_ok=False,
                last_probe_stage="tcp",
            ),
            Node(code="fi-internal", title="Финляндия", host="10.0.0.9", channel="cdn"),
        ]
    )
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="sub:locations", user_id=9050))

    sent = bot.session.all_text()
    assert "Германия" in sent and "отвечает, 42 мс" in sent
    assert "Нидерланды" in sent and "порт не пускает" in sent and "резерв" in sent
    assert "Финляндия" in sent and "нет данных пробы" in sent and "CDN" in sent
    assert "de-internal" not in sent and "10.0.0.7" not in sent
    assert "443" not in sent, "порты клиенту не показываем"


async def test_locations_screen_has_honest_empty_state(session, panel, bot, dispatcher):
    """Нод нет — не выдумываем список, а говорим, что пока пусто."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9051))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="sub:locations", user_id=9051))

    sent = bot.session.all_text()
    assert "Локации" in sent
    assert "Пока нечего показать" in sent
    assert "отвечает" not in sent


# ------------------------------------------------------------------ дизайн экрана
def test_profile_keyboard_follows_design_rules():
    rows = keyboards.profile_kb().inline_keyboard
    buttons = [button for row in rows for button in row]

    assert len(buttons) <= 6, "предел дизайн-системы — шесть кнопок"
    assert len(rows) <= 4
    assert sum(1 for button in buttons if button.style == "success") == 1, "главное действие одно"
    for button in buttons:
        assert len(button.text) <= 30, f"подпись не влезет: «{button.text}»"
        assert not EMOJI.findall(button.text), f"эмодзи в кнопке: «{button.text}»"


def test_profile_button_is_in_main_menu_of_every_state():
    for has_sub, is_active in ((False, False), (True, True), (True, False)):
        rows = keyboards.main_menu(has_sub, is_active, 120).inline_keyboard
        labels = [button.text for row in rows for button in row]

        assert keyboards.BTN_PROFILE in labels
        assert sum(len(row) for row in rows) <= 6
        assert len(rows) <= 4


def test_profile_button_is_in_reply_menu():
    labels = [button.text for row in keyboards.reply_menu().keyboard for button in row]

    assert keyboards.BTN_PROFILE in labels


def test_progress_bar_edges():
    """Полоса остатка: ноль дней — пусто, полный срок — целиком.

    Последний день доступа не должен выглядеть как полный срок: это ровно та
    «оптимистичная» картинка, из-за которой человек узнаёт об окончании, когда
    уже отключился.
    """
    from app.services.profile import progress_bar

    assert progress_bar(0, 30) == "░" * 10
    assert progress_bar(30, 30) == "█" * 10
    assert progress_bar(15, 30) == "█" * 5 + "░" * 5
    # Больше срока в базе быть не может, но полоса не должна уезжать за край.
    assert progress_bar(99, 30) == "█" * 10
    # Нулевой период (данных нет) — не деление на ноль и не полная полоса.
    assert progress_bar(1, 0) == "█" * 10
