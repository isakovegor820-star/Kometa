"""Тесты ретенции персональных данных (B7).

Проверяем не «функция вызвалась», а обещание Политики конфиденциальности:

* пункт 6.3 — технические журналы (``events``/``alerts``/``broadcasts``) живут
  не больше 30 дней, свежие остаются;
* пункт 6.1 — аккаунт без активности 12 месяцев анонимизируется;
* пункт 6.2 — заказы и платежи остаются (4 года, налоговый учёт);
* пункт 6.4 — удаление по запросу работает, пишется в аудит, повтор безопасен;
* ни один публичный текст не обещает другого срока, чем в коде.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import models as db_models
from app.db.models import Alert, Broadcast, Event, Order, Subscription, User, UserNote
from app.services import audit, orders, retention, subscriptions
from app.web import security
from app.web.sub import build_app

settings = get_settings()
ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "test-admin-password"


@pytest.fixture(autouse=True)
def admin_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_panel_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-secret")
    security.login_throttle._attempts.clear()
    yield


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": PASSWORD})
    assert response.status_code == 303


def _ago(*, days: int = 0, months: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days + round(months * 30.44))


async def _make_user(session, tg_id: int, *, created_days_ago: int = 0, **kwargs) -> User:  # noqa: ANN003
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"u{tg_id}", first_name=f"Имя{tg_id}"
    )
    user.created_at = _ago(days=created_days_ago)
    for name, value in kwargs.items():
        setattr(user, name, value)
    await session.flush()
    return user


async def _paid_order(session, user: User, *, amount_rub: int = 120, created_days_ago: int = 400) -> Order:
    plan = (await orders.list_plans(session))[0]
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        kind="purchase",
        amount_rub=amount_rub,
        base_amount_rub=amount_rub,
        provider="manual",
        status="paid",
        paid_at=_ago(days=created_days_ago),
        created_at=_ago(days=created_days_ago),
        grant_target_at=_ago(days=created_days_ago),
        granted_at=_ago(days=created_days_ago),
        external_id=f"test-{user.id}-{created_days_ago}",
    )
    session.add(order)
    await session.flush()
    return order


# --------------------------------------------------------------- 30 дней (6.3)
async def test_old_events_are_purged(session):
    """Записи events/alerts/broadcasts старше 30 дней удаляются задачей.

    Свежие остаются: иначе панель потеряет текущую картину, а вместе с ней и
    разбор инцидента, который случился вчера.
    """
    old_event = Event(kind="start", created_at=_ago(days=45))
    fresh_event = Event(kind="start", created_at=_ago(days=3))
    old_alert = Alert(kind="node_down", title="давняя нода", created_at=_ago(days=40))
    fresh_alert = Alert(kind="node_down", title="свежая нода", created_at=_ago(days=1))
    old_broadcast = Broadcast(text="старая рассылка", created_at=_ago(days=31))
    fresh_broadcast = Broadcast(text="новая рассылка", created_at=_ago(days=2))
    session.add_all([old_event, fresh_event, old_alert, fresh_alert, old_broadcast, fresh_broadcast])
    await session.commit()

    result = await retention.purge_old_records(session)
    await session.commit()

    assert result.events == 1 and result.alerts == 1 and result.broadcasts == 1
    assert await session.get(Event, old_event.id) is None
    assert await session.get(Alert, old_alert.id) is None
    assert await session.get(Broadcast, old_broadcast.id) is None
    assert await session.get(Event, fresh_event.id) is not None
    assert await session.get(Alert, fresh_alert.id) is not None
    assert await session.get(Broadcast, fresh_broadcast.id) is not None


async def test_retention_window_matches_policy(session):
    """Срок из Политики (30 дней) — это и есть срок задачи, а не «примерно»."""
    boundary = Event(kind="start", created_at=datetime.now(timezone.utc) - timedelta(days=29))
    beyond = Event(kind="start", created_at=datetime.now(timezone.utc) - timedelta(days=30, hours=1))
    session.add_all([boundary, beyond])
    await session.commit()

    await retention.purge_old_records(session)
    await session.commit()

    assert await session.get(Event, boundary.id) is not None
    assert await session.get(Event, beyond.id) is None
    assert retention.EVENTS_RETENTION_DAYS == 30


# ------------------------------------------------------------- 12 месяцев (6.1)
async def test_user_is_anonymized_after_year(session):
    """Пользователь без активности 12 месяцев анонимизируется; персональные поля пусты."""
    user = await _make_user(session, 9101, created_days_ago=400)
    await session.commit()

    anonymized = await retention.anonymize_inactive_users(session)
    await session.commit()
    await session.refresh(user)

    assert anonymized == [user.id]
    assert user.anonymized_at is not None
    assert user.username is None
    assert user.first_name is None
    assert user.tags == ""
    assert user.tg_id < 0  # синтетический идентификатор
    assert user.display_name == db_models.ANONYMIZED_DISPLAY_NAME
    assert user.display_name == "удалён"


async def test_active_user_is_not_touched(session):
    """Свежий клиент и клиент с действующей подпиской остаются как были."""
    fresh = await _make_user(session, 9102, created_days_ago=10)
    plan = (await orders.list_plans(session))[0]
    long_silence = await _make_user(session, 9103, created_days_ago=400)
    session.add(
        Subscription(
            user_id=long_silence.id,
            plan_id=plan.id,
            status="active",
            subscription_token="tok-retention-active",
            expires_at=datetime.now(timezone.utc) + timedelta(days=20),
        )
    )
    await session.commit()

    anonymized = await retention.anonymize_inactive_users(session)
    await session.commit()

    assert anonymized == []
    assert fresh.anonymized_at is None
    assert long_silence.anonymized_at is None


async def test_repeat_run_does_not_touch_anonymized(session):
    """Повторный прогон задачи не анонимизирует дважды и не плодит события."""
    user = await _make_user(session, 9104, created_days_ago=400)
    await session.commit()

    first = await retention.anonymize_inactive_users(session)
    await session.commit()
    stamp = user.anonymized_at
    second = await retention.anonymize_inactive_users(session)
    await session.commit()

    assert first == [user.id] and second == []
    assert user.anonymized_at == stamp
    events = int(
        await session.scalar(
            select(func.count(Event.id)).where(Event.user_id == user.id, Event.kind == "user_anonymized")
        )
        or 0
    )
    assert events == 1


# ---------------------------------------------------------------- заказы (6.2)
async def test_orders_survive_anonymization(session):
    """Заказы и суммы остаются: админ видит «удалён», но деньги на месте."""
    user = await _make_user(session, 9105, created_days_ago=400)
    first = await _paid_order(session, user, amount_rub=120, created_days_ago=400)
    second = await _paid_order(session, user, amount_rub=539, created_days_ago=380)
    await session.commit()

    anonymized = await retention.anonymize_inactive_users(session)
    await session.commit()
    await session.refresh(user)

    assert anonymized == [user.id]
    assert (await session.get(Order, first.id)) is not None
    assert (await session.get(Order, second.id)) is not None
    total = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.user_id == user.id, Order.status == "paid"
            )
        )
        or 0
    )
    assert total == 659
    assert user.display_name == "удалён"

    # В отчётах заказы видны по-прежнему: суммы не зависят от того, что имя стёрто.
    finance_total = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount_rub), 0)).where(Order.status == "paid")
        )
        or 0
    )
    assert finance_total == 659


async def test_notes_and_events_are_erased_with_user(session):
    """Вместе с аккаунтом уходят заметки и события клиента — это тоже персональные данные."""
    user = await _make_user(session, 9106, created_days_ago=400)
    session.add(
        UserNote(
            user_id=user.id,
            author="модератор",
            author_role="moderator",
            text="звонил, просил удалить",
            created_at=_ago(days=400),
        )
    )
    session.add(Event(kind="start", user_id=user.id, created_at=_ago(days=400)))
    await session.commit()

    await retention.anonymize_inactive_users(session)
    await session.commit()

    assert await session.scalar(select(func.count(UserNote.id)).where(UserNote.user_id == user.id)) == 0
    assert await session.scalar(select(func.count(Event.id)).where(Event.user_id == user.id)) == 1  # только факт удаления


# ------------------------------------------------------- удаление по запросу
async def test_manual_erasure_is_audited(client, session):
    """Кнопка удаления по запросу: работает, пишет запись в аудит, повтор безопасен."""
    user = await _make_user(session, 9107, created_days_ago=30, username="erase_me", first_name="Егор")
    order = await _paid_order(session, user, amount_rub=299, created_days_ago=20)
    await session.commit()
    await _login(client)

    first = await client.post(f"/admin/users/{user.id}/erase", data={"reason": "запрос клиента"})
    assert first.status_code == 303

    await session.refresh(user)
    assert user.anonymized_at is not None
    assert user.username is None and user.first_name is None
    assert user.display_name == "удалён"
    assert (await session.get(Order, order.id)) is not None  # налоги важнее

    actions = list(
        (
            await session.scalars(
                select(Event).where(Event.kind == "admin.user_erased", Event.user_id == user.id)
            )
        ).all()
    )
    assert len(actions) == 1
    assert actions[0].actor_name  # кто удалил
    assert "запрос клиента" in (actions[0].payload or "")

    # Повторное нажатие безопасно: ни ошибки, ни второй записи в аудите.
    second = await client.post(f"/admin/users/{user.id}/erase", data={"reason": "повтор"})
    assert second.status_code == 303
    again = list(
        (
            await session.scalars(
                select(Event).where(Event.kind == "admin.user_erased", Event.user_id == user.id)
            )
        ).all()
    )
    assert len(again) == 1


async def test_erasure_button_is_hidden_from_support(client, session):
    """Поддержка смотрит, но не удаляет: право на удаление — у владельца и модератора."""
    account = db_models.AdminAccount(
        login="support-ret",
        display_name="Поддержка",
        role="support",
        password_hash=security.hash_password("support-pass-1"),
    )
    session.add(account)
    await session.commit()
    user = await _make_user(session, 9108, created_days_ago=30)
    await session.commit()

    login = await client.post("/admin/login", data={"login": "support-ret", "password": "support-pass-1"})
    assert login.status_code == 303
    denied = await client.post(f"/admin/users/{user.id}/erase")
    assert denied.status_code == 403

    page = await client.get(f"/admin/users/{user.id}")
    assert page.status_code == 200
    assert "/erase" not in page.text


async def test_erasure_route_requires_login(client, session):
    """Без входа кнопка недоступна — данные не удалит случайный запрос."""
    user = await _make_user(session, 9109, created_days_ago=30)
    await session.commit()

    response = await client.post(f"/admin/users/{user.id}/erase")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


# ---------------------------------------------- тексты и политика (B7, 6.x)
def test_policy_promises_match_code() -> None:
    """В Политике нет обещаний, которые код не исполняет.

    Сроки в тексте документа должны совпадать с константами ``retention``:
    иначе маркетинг обещает одно, а задача делает другое — и это уже не
    «неточность формулировки», а недостоверная информация для клиента.
    """
    policy = (ROOT / "docs" / "legal" / "ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ.md").read_text(encoding="utf-8")

    assert f"не более {retention.EVENTS_RETENTION_DAYS} дней" in policy
    assert f"{retention.INACTIVITY_MONTHS} месяцев после последней активности" in policy
    assert f"{retention.FINANCIAL_YEARS} года" in policy
    # Обещание «по запросу удаляются» подкреплено маршрутом кнопки.
    assert "По вашему запросу данные удаляются" in policy
    route = (ROOT / "app" / "web" / "admin" / "users.py").read_text(encoding="utf-8")
    assert '@router.post("/users/{user_id}/erase")' in route


def test_retention_constants_are_documented_in_one_place() -> None:
    """Сроки живут в retention.py и в текстах — правка одного места меняет всё."""
    source = (ROOT / "app" / "services" / "retention.py").read_text(encoding="utf-8")
    assert re.search(r"EVENTS_RETENTION_DAYS = 30", source)
    assert re.search(r"INACTIVITY_MONTHS = 12", source)
    assert re.search(r"FINANCIAL_YEARS = 4", source)


def test_public_bot_texts_do_not_promise_other_retention() -> None:
    """Публичные тексты бота не называют других сроков хранения данных."""
    from app.bot import texts

    public = "\n".join(
        value for value in vars(texts).values() if isinstance(value, str)
    )
    assert "30 дней" not in public or "журнал" in public.lower()
    # Никаких «храним вечно» и «удаляем сразу» — обещания расходятся с Политикой.
    assert "храним вечно" not in public.lower()
    assert "удаляем сразу" not in public.lower()
