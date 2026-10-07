"""Сборка юридических документов и прайса из настроек и тарифов.

Единая точка правды: бот, скрипт публикации в Telegra.ph и проверки банка
берут текст отсюда, поэтому политика в чате и на опубликованной странице
не расходятся. Никакой сети и БД в чистых функциях — их удобно тестировать.

Реквизиты исполнителя живут в .env (LEGAL_OPERATOR_NAME, LEGAL_OPERATOR_INN):
пока они не заполнены, документы остаются черновиком с плейсхолдерами.
"""

from __future__ import annotations

from app import legal_texts
from app.config import get_settings
from app.legal_texts import LegalContext, PriceRow

#: Тарифы по умолчанию — те же, что сеются в БД (app/db/session.py).
#: Нужны, когда документы собираются без обращения к базе (скрипт публикации, тесты).
DEFAULT_PRICES: tuple[PriceRow, ...] = (
    PriceRow(title="1 месяц", price_rub=199, days=30),
    PriceRow(title="3 месяца", price_rub=499, days=90),
    PriceRow(title="6 месяцев", price_rub=890, days=180),
    PriceRow(title="12 месяцев", price_rub=1590, days=365),
)


def _support_contact() -> str:
    contact = get_settings().support_contact
    return contact or legal_texts.SUPPORT_PLACEHOLDER


def build_context(prices: tuple[PriceRow, ...] | list[PriceRow] = (), devices: int = 3) -> LegalContext:
    """Собрать контекст документов из .env и переданных тарифов."""
    settings = get_settings()
    return LegalContext(
        operator=settings.legal_operator_name.strip() or legal_texts.OPERATOR_PLACEHOLDER,
        inn=settings.legal_operator_inn.strip() or legal_texts.INN_PLACEHOLDER,
        support=_support_contact(),
        bot=("@" + settings.bot_username.lstrip("@")) if settings.bot_username else legal_texts.BOT_PLACEHOLDER,
        updated=settings.legal_updated_at,
        devices=devices,
        locations=settings.locations_note,
        trial_days=settings.trial_days,
        trial_gb=settings.trial_gb,
        order_ttl_minutes=settings.order_ttl_minutes,
        prices=tuple(prices) or DEFAULT_PRICES,
        privacy_url=settings.privacy_url,
        terms_url=settings.terms_url,
        pricing_url=settings.pricing_url,
    )


def prices_from_plans(plans: list) -> tuple[PriceRow, ...]:  # noqa: ANN001 - Plan из БД
    """Превратить тарифы из БД в строки документов (цены всегда актуальные)."""
    return tuple(
        PriceRow(
            title=plan.title,
            price_rub=plan.price_rub,
            days=plan.days,
            per_month=round(plan.price_rub / max(1, plan.days) * 30),
        )
        for plan in plans
    )


def privacy_text(context: LegalContext | None = None) -> str:
    return legal_texts.build_privacy(context or build_context())


def terms_text(context: LegalContext | None = None) -> str:
    return legal_texts.build_terms(context or build_context())


def price_list_text(context: LegalContext | None = None) -> str:
    return legal_texts.build_price_list(context or build_context())


def requisites_are_ready() -> bool:
    """Заполнены ли реквизиты исполнителя настолько, чтобы публиковать документы.

    Обязательно только имя: партнёр подтвердил, что ИП/ООО для модерации не
    требуется. ИНН и упоминание НПД попадают в текст, когда они есть, —
    выдумывать реквизиты нельзя.
    """
    return bool(get_settings().legal_operator_name.strip())
