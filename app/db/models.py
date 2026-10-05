"""Модель данных.

Принципы:
  * минимум персональных данных — только Telegram-идентификаторы;
  * деньги — целые числа (копейки/рубли без float);
  * всё, что меняется без программиста (цены, лимиты), живёт в БД.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """DateTime, который всегда возвращает время с зоной UTC.

    SQLite не хранит часовой пояс, поэтому без этого типа сравнение
    «наивной» даты из БД с aware-датой из кода падает с TypeError.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    first_name: Mapped[str | None] = mapped_column(String(128), default=None)
    language: Mapped[str] = mapped_column(String(8), default="ru")

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)

    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def display_name(self) -> str:
        return self.first_name or (f"@{self.username}" if self.username else f"id{self.tg_id}")


class Plan(Base):
    """Тариф. Цены меняются в БД, а не в коде."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    price_rub: Mapped[int] = mapped_column(Integer)  # рубли, целое
    price_stars: Mapped[int] = mapped_column(Integer, default=0)
    devices_limit: Mapped[int] = mapped_column(Integer, default=3)
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, default=0)  # 0 = безлимит
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), default=None)

    status: Mapped[str] = mapped_column(String(16), default="trial")  # trial|active|expired|blocked
    starts_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)

    devices_limit: Mapped[int] = mapped_column(Integer, default=3)
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, default=0)

    panel_user_uuid: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    subscription_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="subscription")

    @property
    def is_active(self) -> bool:
        return self.status in {"trial", "active"} and self.expires_at > utcnow()

    @property
    def days_left(self) -> int:
        delta = self.expires_at - utcnow()
        return max(0, delta.days + (1 if delta.seconds else 0))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), default=None)

    kind: Mapped[str] = mapped_column(String(16), default="purchase")  # purchase|renew|trial
    amount_rub: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(16), default="manual")  # manual|crypto|stars
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|paid|canceled|expired
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True, default=None)

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    comment: Mapped[str | None] = mapped_column(Text, default=None)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, default=None)

    user: Mapped[User] = relationship(back_populates="orders")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    raw_payload: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("invited_id", name="uq_referral_invited"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invited_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    bonus_days_referrer: Mapped[int] = mapped_column(Integer, default=0)
    bonus_days_invited: Mapped[int] = mapped_column(Integer, default=0)
    paid_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(64))
    country: Mapped[str] = mapped_column(String(8), default="")
    host: Mapped[str] = mapped_column(String(128), default="")

    panel_type: Mapped[str] = mapped_column(String(16), default="xui")
    panel_url: Mapped[str] = mapped_column(String(255), default="")
    panel_token: Mapped[str] = mapped_column(String(255), default="")
    inbound_ids: Mapped[str] = mapped_column(String(64), default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)

    last_check_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    """Журнал событий: отладка, аналитика, разбор инцидентов."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
