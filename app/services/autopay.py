"""Автоматическое подтверждение переводов по выписке банка.

Как это работает:

1. Бот выдаёт заказ с уникальной суммой (199.13 ₽) и просит указать в
      комментарии «Kometa <номер заказа>».
2. Раз в несколько минут этот модуль читает поступления из настроенных
      источников (CSV-выписка, почтовые уведомления банка).
3. Платёж сопоставляется с заказом: сначала по коду в комментарии, потом
      по точной сумме. Совпало — заказ подтверждается, подписка выдаётся,
      пользователь получает ссылку. Всё без участия человека.
4. Если поступление не удалось сопоставить, оно не теряется: админ получает
      уведомление с суммой и комментарием.

Состояние (время последней проверки и уже обработанные платежи) хранится в
JSON-файле: так повторный опрос не подтвердит один платёж дважды.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import get_settings
from app.db.models import Order, User
from app.panels.base import PanelClient
from app.payments.matching import parse_order_code
from app.payments.statements import IncomingPayment, StatementError, StatementSource
from app.services import alerts as alerts_service, events, notifications
from app.services import orders as orders_service

logger = logging.getLogger(__name__)
settings = get_settings()

#: Сколько ключей обработанных платежей помним (защита от повторного подтверждения)
MAX_REMEMBERED_KEYS = 1000

#: Причина «не сопоставлено»: под сумму подходит больше одного заказа.
#: Подтверждать автоматически нельзя — деньги одного клиента оплатят доступ
#: другому. Такой платёж уходит админам как «неопознанный».
AMBIGUOUS_MATCH = "несколько заказов на ту же сумму — нужен человек"


@dataclass
class ReconcileResult:
        """Итог одной проверки выписки."""

        fetched: int = 0
        confirmed: list[int] = field(default_factory=list)
        #: Оплаченные заказы, по которым панель не подтвердила выдачу доступа:
        #: деньги приняты, выдачу повторит фоновая задача (B2).
        pending_grant: list[int] = field(default_factory=list)
        unmatched: list[IncomingPayment] = field(default_factory=list)
        #: Почему платёж не сопоставлен (ключ → текст): «несколько заказов на
        #: ту же сумму» и т.п. Нужно для уведомления и алерта.
        unmatched_reasons: dict[str, str] = field(default_factory=dict)
        errors: list[str] = field(default_factory=list)
        skipped: int = 0

        def as_text(self) -> str:
                return (
                        f"проверено поступлений: {self.fetched}, подтверждено: {len(self.confirmed)}, "
                        f"ждут выдачи: {len(self.pending_grant)}, "
                        f"не сопоставлено: {len(self.unmatched)}, пропущено (уже видели): {self.skipped}"
                )


# ------------------------------------------------------------------ состояние
def _state_path() -> Path:
        return Path(settings.statement_state_file)


def load_state() -> dict:
        path = _state_path()
        if not path.exists():
                return {}
        try:
                return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Не смог прочитать состояние автоплатежа: %s", exc)
                return {}


def save_state(state: dict) -> None:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
                path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - диск может быть только для чтения
                logger.error("Не смог сохранить состояние автоплатежа: %s", exc)


def _last_check(state: dict) -> datetime:
        raw = state.get("last_check")
        if isinstance(raw, str):
                try:
                        moment = datetime.fromisoformat(raw)
                        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
                except ValueError:
                        pass
        # Первый запуск: смотрим сутки назад, чтобы не потерять свежие переводы
        return datetime.now(timezone.utc) - timedelta(days=1)


# ------------------------------------------------------------------ источники
def _sources_state_path() -> Path:
        """Отдельный файл прогресса для источников выписки.

        «state.json» держит время последней проверки и обработанные платежи,
        а этот файл — докуда прочитан каждый файл выписки / какие письма разобраны.
        """
        return Path(settings.statement_state_file).with_suffix(".sources.json")


def build_statement_sources() -> list[StatementSource]:
        """Собрать источники выписок из настроек. Пусто — если ничего не настроено."""
        try:
                from app.payments.statements_sources import CsvStatementSource, ImapStatementSource
        except ImportError:  # pragma: no cover - модуль появляется вместе с фичей
                logger.warning("Модуль источников выписок недоступен")
                return []

        sources: list[StatementSource] = []

        csv_glob = settings.statement_csv_glob.strip()
        if csv_glob:
                try:
                        sources.append(CsvStatementSource(csv_glob, state_file=_sources_state_path()))
                except StatementError as exc:
                        logger.warning("CSV-источник выписки не настроен: %s", exc)

        if settings.bank_imap_host and settings.bank_imap_user and settings.bank_imap_password:
                try:
                        sources.append(
                                ImapStatementSource(
                                        host=settings.bank_imap_host,
                                        user=settings.bank_imap_user,
                                        password=settings.bank_imap_password,
                                        port=settings.bank_imap_port,
                                        folder=settings.bank_imap_folder,
                                )
                        )
                except StatementError as exc:
                        logger.warning("IMAP-источник выписки не настроен: %s", exc)

        return sources


# ------------------------------------------------------------------ сопоставление
async def find_order_for_payment(
        session: AsyncSession,
        payment: IncomingPayment,
        *,
        tolerance_kopecks: int | None = None,
) -> tuple[Order | None, str]:
        """Найти заказ для поступления.

        :returns: (заказ или None, причина совпадения — для логов и уведомлений).
        """
        tolerance = settings.autopay_tolerance_kopecks if tolerance_kopecks is None else tolerance_kopecks

        pending = list(
                (
                        await session.scalars(
                                select(Order)
                                .where(Order.status == "pending", Order.provider == "manual")
                                .order_by(Order.created_at)
                        )
                ).all()
        )
        if not pending:
                return None, ""

        # 1. Код заказа в комментарии — самый надёжный признак
        code = parse_order_code(payment.comment)
        if code is not None:
                for order in pending:
                        if order.id == code:
                                return order, "по коду заказа в комментарии"

        # 2. Точная сумма с уникальными копейками
        exact = [order for order in pending if abs(payment.amount_kopecks - order.pay_amount_kopecks) <= tolerance]
        if len(exact) == 1:
                return exact[0], "по точной сумме"
        if len(exact) > 1:
                # Совпало несколько заказов (например, допуск копеек разрешён
                # настройкой): выбирать «первый по дате» нельзя — это оплата
                # чужого заказа.
                return None, AMBIGUOUS_MATCH

        # 3. Сумма без копеек — банк мог не передать надбавку. Подтверждаем
        # ТОЛЬКО когда такой заказ ровно один: иначе платёж одного клиента
        # выдаст доступ другому (PoC ревью: платёж клиента №2 подтвердил заказ №1).
        bare = [order for order in pending if payment.amount_kopecks == order.amount_rub * 100]
        if len(bare) == 1:
                return bare[0], "по сумме без копеек (уточнить вручную)"
        if len(bare) > 1:
                return None, AMBIGUOUS_MATCH

        return None, ""


# ------------------------------------------------------------------ основной цикл
async def reconcile(
        session: AsyncSession,
        panel: PanelClient,
        bot: Bot | None,
        *,
        sources: list[StatementSource] | None = None,
) -> ReconcileResult:
        """Прочитать выписку, подтвердить найденные оплаты, выдать подписки."""
        result = ReconcileResult()
        if not settings.autopay_enabled:
                return result

        active_sources = sources if sources is not None else build_statement_sources()
        if not active_sources:
                return result

        state = load_state()
        since = _last_check(state)
        processed: list[str] = list(state.get("processed", []))

        payments: list[IncomingPayment] = []
        for source in active_sources:
                try:
                        payments.extend(await source.fetch(since))
                except StatementError as exc:
                        # Источник мог разобрать часть файлов/писем до ошибки — эти
                        # поступления нельзя терять, обрабатываем их в этом же цикле.
                        recovered = list(getattr(exc, "payments", []) or [])
                        if recovered:
                                payments.extend(recovered)
                        message = f"{source.name}: {exc}"
                        result.errors.append(message)
                        logger.warning("Источник выписки не сработал — %s (спасено поступлений: %s)", message, len(recovered))
                except Exception as exc:  # noqa: BLE001 - источник не должен ронять бота
                        message = f"{source.name}: неожиданная ошибка {exc}"
                        result.errors.append(message)
                        logger.exception("Ошибка источника выписки")

        result.fetched = len(payments)

        for payment in payments:
                if payment.key in processed:
                        result.skipped += 1
                        continue

                order, reason = await find_order_for_payment(session, payment)
                if order is None:
                        result.unmatched.append(payment)
                        if reason:
                                result.unmatched_reasons[payment.key] = reason
                else:
                        user = await session.get(User, order.user_id)
                        try:
                                sub, already = await orders_service.mark_paid(
                                        session, order, panel, confirmed_by=None,
                                        provider_payment_id=payment.key, bot=bot,
                                )
                        except Exception as exc:  # noqa: BLE001 - панель могла отвалиться
                                logger.error("Не смог выдать доступ по заказу %s: %s", order.id, exc)
                                result.errors.append(f"заказ #{order.id}: {exc}")
                                continue

                        result.confirmed.append(order.id)
                        if sub is None and not already:
                                # Оплата зафиксирована, но панель не подтвердила
                                # выдачу: заказ подхватит job_grant_paid. В ошибки
                                # выписки не пишем — окно проверки сдвигать можно.
                                logger.warning(
                                        "Заказ #%s оплачен, но доступ не выдан (%s) — повторю автоматически",
                                        order.id,
                                        order.grant_last_error or "панель не ответила",
                                )
                                result.pending_grant.append(order.id)
                        await events.log_event(
                                session,
                                events.ORDER_PAID,
                                user_id=order.user_id,
                                payload={
                                        "order_id": order.id,
                                        "amount_kopecks": payment.amount_kopecks,
                                        "auto": True,
                                        "reason": reason,
                                        "source": payment.source,
                                },
                        )
                        if bot is not None and user is not None and sub is not None and not already:
                                await _notify_user(bot, user, sub, order)
                                await notifications.notify_admins(
                                        bot,
                                        f"🤖 <b>Автоподтверждение оплаты</b>\n"
                                        f"Заказ #{order.id}: {payment.amount_kopecks / 100:.2f} ₽ "
                                        f"({notifications.safe(reason)})\n"
                                        f"Пользователь: {notifications.safe(user.display_name)} "
                                        f"(<code>{user.tg_id}</code>)",
                                )

                processed.append(payment.key)

        # --- несопоставленные платежи: не теряем, а показываем админам
        if result.unmatched and bot is not None and settings.autopay_notify_unmatched:
                lines = [
                        "⚠️ <b>Пришло поступление, но заказ не найден</b>",
                        "",
                ]
                for payment in result.unmatched[:10]:
                        why = result.unmatched_reasons.get(payment.key, "")
                        lines.append(
                                f"• {payment.amount_kopecks / 100:.2f} ₽"
                                f"{' от ' + payment.counterparty if payment.counterparty else ''}"
                                f"{' — «' + payment.comment + '»' if payment.comment else ''}"
                                f" ({payment.received_at:%d.%m %H:%M}, {payment.source})"
                                + (f"\n  ⚠️ {why}" if why else "")
                        )
                lines.append("")
                lines.append("Проверь выписку и подтверди заказ вручную в /admin.")
                await notifications.notify_admins(bot, "\n".join(lines))

        # Алерт в панели: сообщение в Telegram легко пропустить, а «деньги пришли,
        # заказ не найден» — работа, которая не должна потеряться. Для неоднозначной
        # суммы это ещё и защита: без человека доступ не выдаётся никому (B3).
        for payment in result.unmatched[:10]:
                why = result.unmatched_reasons.get(payment.key, "")
                await alerts_service.raise_alert(
                        session,
                        "payment_unmatched",
                        title=f"Поступление {payment.amount_kopecks / 100:.2f} ₽ без заказа",
                        message=(
                                f"{payment.amount_kopecks / 100:.2f} ₽, {payment.received_at:%d.%m %H:%M}, "
                                f"источник: {payment.source or '—'}"
                                f"{' — ' + why if why else ''}"
                        ),
                        fingerprint=f"payment:{payment.key}",
                )

        if result.errors and bot is not None:
                await notifications.notify_admins(
                        bot,
                        "⚠️ Автопроверка оплаты не смогла прочитать выписку:\n" + "\n".join(f"• {e}" for e in result.errors[:5]),
                )

        # --- сохраняем состояние
        # Окно проверки двигаем ТОЛЬКО когда все источники прочитаны без ошибок:
        # иначе платёж из непрочитанного файла/письма окажется старше нового since
        # и будет отфильтрован — то есть потерян.
        if result.errors:
                state["last_check"] = since.isoformat()
                logger.warning("Окно автоплатежа не сдвинуто из-за ошибок чтения выписки")
        else:
                state["last_check"] = datetime.now(timezone.utc).isoformat()
        state["processed"] = processed[-MAX_REMEMBERED_KEYS:]
        save_state(state)
        return result


async def _notify_user(bot: Bot, user: User, sub, order=None) -> None:  # noqa: ANN001 - Subscription, Order
        from app.services import subscriptions as subs_service

        expires = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
        with_link = getattr(sub, "subscription_token", "")
        text = f"✅ Оплата получена! Подписка активна до <b>{expires}</b> ({sub.days_left} дн.)."
        reward = getattr(order, "referral_reward", None)
        if reward is not None and reward.invited_days:
                text += texts.ORDER_PAID_REFERRAL_BONUS.format(days=reward.invited_days)
        try:
                await bot.send_message(user.tg_id, text)
                if with_link:
                        from app.bot import keyboards

                        link = subs_service.subscription_link(with_link)
                        await bot.send_message(
                                user.tg_id,
                                texts.SUBSCRIPTION_LINK_HINT.format(link=link),
                                reply_markup=keyboards.connect_kb(link),
                                disable_web_page_preview=True,
                        )
        except Exception as exc:  # noqa: BLE001
                logger.warning("Не смог уведомить пользователя %s: %s", user.tg_id, exc)
