"""Сквозной тест автоплатежа на настоящем CSV-файле выписки.

Здесь нет заглушек источника: пишем файл так, как его выгружает банк,
и проверяем, что система сама находит платёж и выдаёт подписку.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.services import autopay, orders, subscriptions

settings = get_settings()


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):  # noqa: ANN003
        self.messages.append((chat_id, text))

    def texts(self) -> str:
        return " ".join(text for _, text in self.messages)


@pytest.fixture(autouse=True)
def csv_env(monkeypatch, tmp_path):
    """Автоплатёж включён, выписка читается из tmp-папки."""
    statements = tmp_path / "statements"
    statements.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "autopay_enabled", True)
    monkeypatch.setattr(settings, "statement_state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(settings, "statement_csv_glob", str(statements / "*.csv"))
    monkeypatch.setattr(settings, "statement_state_file", str(tmp_path / "state.json"))
    return statements


def write_csv(directory, content: str, name: str = "statement.csv"):
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


async def make_order(session, tg_id: int):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"u{tg_id}")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.flush()
    return user, order


async def test_csv_payment_is_found_and_subscription_issued(session, panel, csv_env):
    user, order = await make_order(session, 8001)
    await session.commit()

    write_csv(
        csv_env,
        "Дата операции;Сумма;Назначение;Плательщик\n"
        f"05.10.2026 21:30;{order.pay_amount_text};Kometa {order.id};ИВАН И.\n",
    )

    bot = RecordingBot()
    # sources=None → источники собираются из настроек, то есть настоящий CSV-источник
    result = await autopay.reconcile(session, panel, bot)
    await session.commit()

    assert result.confirmed == [order.id], result.errors
    await session.refresh(order)
    assert order.status == "paid"

    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"
    assert "/sub/" in bot.texts()


async def test_same_csv_row_is_not_confirmed_twice(session, panel, csv_env):
    _, order = await make_order(session, 8002)
    await session.commit()

    write_csv(
        csv_env,
        "Дата операции;Сумма;Назначение;Плательщик\n"
        f"05.10.2026 21:30;{order.pay_amount_text};Kometa {order.id};ИВАН И.\n",
    )

    first = await autopay.reconcile(session, panel, None)
    await session.commit()
    assert first.confirmed == [order.id]

    second = await autopay.reconcile(session, panel, None)
    await session.commit()
    assert second.confirmed == []  # повторно подписку не продлеваем


async def test_outgoing_row_is_ignored(session, panel, csv_env):
    _, order = await make_order(session, 8003)
    await session.commit()

    write_csv(
        csv_env,
        "Дата операции;Сумма;Направление;Назначение\n"
        f"05.10.2026 21:30;{order.pay_amount_text};Списание;Kometa {order.id}\n",
    )

    result = await autopay.reconcile(session, panel, None)
    await session.commit()

    assert result.confirmed == []
    await session.refresh(order)
    assert order.status == "pending"


async def test_payment_without_kopecks_matches_by_comment(session, panel, csv_env):
    """Банк не отдал копейки — выручает код заказа в комментарии."""
    _, order = await make_order(session, 8004)
    await session.commit()

    write_csv(
        csv_env,
        "Дата операции;Сумма;Назначение;Плательщик\n"
        f"05.10.2026 21:30;{order.amount_rub};Kometa {order.id};ПЁТР С.\n",
    )

    result = await autopay.reconcile(session, panel, None)
    await session.commit()

    assert result.confirmed == [order.id]
    await session.refresh(order)
    assert order.status == "paid"


async def test_unknown_payment_is_reported_to_admin(session, panel, csv_env):
    write_csv(
        csv_env,
        "Дата операции;Сумма;Назначение;Плательщик\n"
        "05.10.2026 21:35;500,00;перевод от мамы;МАРИЯ П.\n",
    )

    bot = RecordingBot()
    result = await autopay.reconcile(session, panel, bot)
    await session.commit()

    assert result.unmatched
    assert "не найден" in bot.texts().lower()


async def test_broken_csv_does_not_crash_bot(session, panel, csv_env, monkeypatch):
    """Битый файл выписки — предупреждение админу, а не падение бота."""
    write_csv(csv_env, "\xff\xfe\x00 бинарный мусор", name="broken.csv")

    bot = RecordingBot()
    result = await autopay.reconcile(session, panel, bot)
    await session.commit()

    assert result.confirmed == []
    assert result.errors or result.fetched == 0  # главное — не упало
