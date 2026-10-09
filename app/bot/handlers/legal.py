"""Раздел «📄 Документы и цены»: политика, соглашение, тарифы, поддержка.

Зачем отдельный роутер: банк-партнёр проверяет, что политика, пользовательское
соглашение, контакт поддержки и актуальные тарифы доступны клиенту **постоянно**
и отдельными кнопками, а не только в момент оплаты.

Если постоянная ссылка на документ ещё не задана (PRIVACY_URL / TERMS_URL),
бот отдаёт полный текст прямо в чате: доступность документа важнее ссылки,
а после публикации кнопки автоматически становятся ссылками.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import legal_texts
from app.bot import keyboards, texts
from app.config import get_settings
from app.legal_texts import LegalContext
from app.payments.registry import payments
from app.services import documents, orders

logger = logging.getLogger(__name__)
router = Router(name="legal")
settings = get_settings()

#: Telegram не принимает сообщения длиннее 4096 знаков. Режем с запасом,
#: чтобы HTML-разметка и подписи не упирались в лимит.
TELEGRAM_LIMIT = 3800

#: Человеческие названия способов оплаты для экрана цен.
PROVIDER_LABELS = {
        "manual": "перевод по СБП",
        "crypto": "криптовалюта",
        "stars": "Telegram Stars",
        "wata": "карта или СБП",
        "platega_sbp": "СБП или QR-код",
        "platega_card": "карта МИР",
        "platega_intl": "зарубежная карта",
}

FALLBACK_PROVIDERS = "перевод по СБП, криптовалюта или Telegram Stars"


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
        """Разбить длинный документ на части по абзацам, не разрывая слова."""
        if len(text) <= limit:
                return [text]

        parts: list[str] = []
        current = ""
        for block in text.split("\n\n"):
                candidate = f"{current}\n\n{block}" if current else block
                if len(candidate) <= limit:
                        current = candidate
                        continue
                if current:
                        parts.append(current)
                while len(block) > limit:
                        cut = block.rfind("\n", 0, limit)
                        if cut <= 0:
                                cut = limit
                        parts.append(block[:cut])
                        block = block[cut:].lstrip("\n")
                current = block
        if current:
                parts.append(current)
        return parts


def providers_line() -> str:
        """Способы оплаты, реально доступные клиенту прямо сейчас."""
        labels = [PROVIDER_LABELS.get(provider.code, provider.title) for provider in payments.available()]
        return ", ".join(dict.fromkeys(labels)) if labels else FALLBACK_PROVIDERS


async def build_context(session: AsyncSession) -> LegalContext:
        """Контекст документов: реквизиты из .env + актуальные тарифы из БД."""
        plans = await orders.list_plans(session)
        devices = plans[0].devices_limit if plans else 3
        return documents.build_context(prices=documents.prices_from_plans(plans), devices=devices)


async def pricing_text(session: AsyncSession) -> str:
        """Экран «Цены и тарифы»: актуальные цены и что за них входит."""
        plans = await orders.list_plans(session)
        context = await build_context(session)
        items = "\n".join(
                texts.PRICING_ITEM.format(
                        title=plan.title,
                        price=texts.format_rub(plan.price_rub),
                        per_month=texts.format_rub(round(plan.price_rub / max(1, plan.days) * 30)),
                )
                for plan in plans
        )
        if not items:
                items = "• Тарифы загружаются — загляните через минуту."

        return texts.PRICING.format(
                items=items,
                devices=context.devices,
                locations_line=texts.PRICING_LOCATIONS_LINE.format(locations=context.locations),
                providers=providers_line(),
                support=settings.support_contact or "кнопка «☎️ Поддержка» ниже",
                updated=context.updated,
                trial_days=context.trial_days,
        )


def _message_of(event: Message | CallbackQuery) -> Message:
        return event.message if isinstance(event, CallbackQuery) else event


async def _reply(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Показать текст: документы длиннее лимита Telegram уходят несколькими сообщениями."""
        message = _message_of(event)
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
                await message.answer(
                        chunk,
                        reply_markup=markup if index == len(chunks) - 1 else None,
                        disable_web_page_preview=True,
                )
        if isinstance(event, CallbackQuery):
                await event.answer()


def _docs_markup() -> InlineKeyboardMarkup:
        return keyboards.legal_kb(privacy_url=settings.privacy_url, terms_url=settings.terms_url)


async def show_legal(event: Message | CallbackQuery) -> None:
        """Главный экран раздела: кнопки на все документы, цены и поддержку."""
        markup = _docs_markup()
        if isinstance(event, CallbackQuery):
                await event.message.edit_text(texts.LEGAL_HEADER, reply_markup=markup, disable_web_page_preview=True)
                await event.answer()
        else:
                await event.answer(texts.LEGAL_HEADER, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "legal:show")
@router.message(F.text == keyboards.BTN_LEGAL)
async def cb_show(event: Message | CallbackQuery) -> None:
        await show_legal(event)


@router.callback_query(F.data == "legal:pricing")
async def cb_pricing(call: CallbackQuery, session: AsyncSession) -> None:
        text = await pricing_text(session)
        await call.message.edit_text(
                text,
                reply_markup=keyboards.docs_back_kb(),
                disable_web_page_preview=True,
        )
        await call.answer()


@router.callback_query(F.data == "legal:privacy")
async def cb_privacy(call: CallbackQuery, session: AsyncSession) -> None:
        context = await build_context(session)
        await _reply(call, legal_texts.build_privacy(context), _docs_markup())


@router.callback_query(F.data == "legal:terms")
async def cb_terms(call: CallbackQuery, session: AsyncSession) -> None:
        context = await build_context(session)
        await _reply(call, legal_texts.build_terms(context), _docs_markup())


@router.callback_query(F.data == "legal:support")
async def cb_support(call: CallbackQuery) -> None:
        contact = settings.support_contact
        text = texts.SUPPORT.format(support=contact) if contact else texts.SUPPORT_NO_CONTACT
        await call.message.edit_text(text, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)
        await call.answer()
