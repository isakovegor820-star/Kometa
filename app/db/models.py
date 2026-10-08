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
    Index,
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

    #: Метки модератора: «шеринг», «vip», «конфликт». Через запятую, ищутся в панели.
    tags: Mapped[str] = mapped_column(String(128), default="")

    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    #: Промокод, который пользователь ввёл руками (скидка на первую оплату).
    #: Пусто — работаем по скидке за приглашение, если она есть.
    promo_code: Mapped[str | None] = mapped_column(String(32), default=None)
    #: Накопленные бонусные дни. Нужны тем, у кого ещё нет подписки:
    #: награда за друга не теряется, а «докапывается» до первой подписки.
    bonus_days_balance: Mapped[int] = mapped_column(Integer, default=0)
    #: Когда последний раз подтвердили подписку на канал (гейт обязательной
    #: подписки). Кэш, чтобы не спрашивать Telegram API на каждый апдейт;
    #: пусто — ещё не подтверждали или срок доверия истёк.
    channel_verified_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

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
    __table_args__ = (Index("ix_orders_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), default=None)

    kind: Mapped[str] = mapped_column(String(16), default="purchase")  # purchase|renew|trial
    #: Сколько реально платит клиент (цена тарифа минус скидка).
    amount_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Цена тарифа до скидки и сама скидка — для отчётов и чеков.
    base_amount_rub: Mapped[int] = mapped_column(Integer, default=0)
    discount_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Промокод, по которому дана скидка (снимок на момент заказа).
    promo_code: Mapped[str | None] = mapped_column(String(32), default=None)
    #: Цена этого заказа в звёздах (со скидкой). 0 — заказ не звёздный.
    stars_amount: Mapped[int] = mapped_column(Integer, default=0)
    #: Уникальная надбавка в копейках (1…99) для автоматического сопоставления
    #: перевода с заказом: 199 ₽ + 13 копеек = 199.13 ₽.
    pay_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(16), default="manual")  # manual|crypto|stars
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|paid|canceled|expired
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True, default=None)

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    comment: Mapped[str | None] = mapped_column(Text, default=None)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, default=None)

    # --- возврат денег -----------------------------------------------------
    #: Когда вернули деньги. Статус заказа при этом становится ``refunded``,
    #: и заказ перестаёт попадать в выручку (её считают только по ``paid``).
    refunded_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Кто из команды оформил возврат (имя учётной записи панели).
    refunded_by: Mapped[str | None] = mapped_column(String(64), default=None)
    #: Причина: «клиент передумал», «чарджбэк», «двойная оплата».
    refund_note: Mapped[str | None] = mapped_column(Text, default=None)

    user: Mapped[User] = relationship(back_populates="orders")

    @property
    def price_before_discount(self) -> int:
        """Цена тарифа без скидки (у старых заказов поле пустое)."""
        return self.base_amount_rub or (self.amount_rub + (self.discount_rub or 0))

    @property
    def pay_amount_kopecks(self) -> int:
        """Точная сумма к переводу в копейках (с учётом уникальной надбавки)."""
        return self.amount_rub * 100 + (self.pay_kopecks or 0)

    @property
    def pay_amount_text(self) -> str:
        """Сумма для показа пользователю: «199.13» или «199»."""
        kopecks = self.pay_amount_kopecks
        if kopecks % 100 == 0:
            return str(kopecks // 100)
        return f"{kopecks // 100}.{kopecks % 100:02d}"


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
    #: Когда награда начислена. По этой дате считается лимит «не больше N в месяц».
    rewarded_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class PromoCode(Base):
    """Промокод на первую оплату.

    Виды:
      * ``referral`` — персональный код пригласившего (``KOMETA-<его код>``):
        друг получает скидку, пригласивший — бонусные дни;
      * ``admin`` — код, который владелец создаёт руками: акция в канале,
        компенсация, договорённость с блогером.
    """

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="admin")  # referral|admin
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    percent: Mapped[int] = mapped_column(Integer, default=50)
    #: Потолок скидки в рублях: 0 — без потолка.
    max_discount_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Скидка только на первую оплату клиента (для акций можно выключить).
    first_only: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Сколько раз код может сработать: 0 — без ограничения.
    uses_limit: Mapped[int] = mapped_column(Integer, default=0)
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    note: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    @property
    def uses_left(self) -> int | None:
        """Сколько активаций осталось (None — без ограничения)."""
        if not self.uses_limit:
            return None
        return max(0, self.uses_limit - (self.uses_count or 0))


class PromoRedemption(Base):
    """Факт использования скидки. Один пользователь — одна скидка в жизни."""

    __tablename__ = "promo_redemptions"
    __table_args__ = (UniqueConstraint("user_id", name="uq_promo_redemption_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), default=None)
    code: Mapped[str] = mapped_column(String(32))
    discount_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Проставляется, когда заказ оплачен: до этого скидка «забронирована».
    confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
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
    #: Адрес сервиса подписок панели ноды, например
    #: ``http://1.2.3.4:2096/<subPath>/``. У каждой панели свой subPath, поэтому
    #: путь задаётся явно: без него бот не соберёт конфиги с этой ноды.
    sub_base: Mapped[str] = mapped_column(String(255), default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)

    last_check_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Канал ноды: ``main`` — обычный режим, ``reserve`` — аварийный профиль.
    #: Резервные локации подписка отдаёт отдельной группой автовыбора: когда
    #: обычные адреса не отвечают, клиенту нужен живой профиль в один тап.
    channel: Mapped[str] = mapped_column(String(8), default="main")

    #: Свой test-URL для локаций этого канала (пусто — общий из настроек).
    #: Нужен каналу CDN: замер идёт через адрес, доступный клиенту, иначе
    #: приложение считает профиль мёртвым, хотя он рабочий.
    test_url: Mapped[str] = mapped_column(String(255), default="")

    #: Проба «глазами клиента»: TCP-соединение и TLS-рукопожатие до инбаунда.
    #: Панель может отвечать, а порт для клиента — нет. Замер нужен, чтобы в
    #: админке и у клиента был виден реальный пинг, а не «нода активна».
    last_probe_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_probe_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Задержка успешной пробы в миллисекундах (0 — ещё не измеряли).
    last_probe_ms: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    """Журнал событий: отладка, аналитика, разбор инцидентов.

    Сюда же пишутся действия администраторов (kind начинается с ``admin.``):
    поля ``actor_*`` отвечают на вопрос «кто это сделал», ``user_id`` — над кем.
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_kind_created", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[str | None] = mapped_column(Text, default=None)

    # --- кто действовал (для клиентских событий пусто) ----------------------
    actor_name: Mapped[str | None] = mapped_column(String(64), default=None)
    actor_role: Mapped[str | None] = mapped_column(String(16), default=None)
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    #: Откуда пришло действие: ``web`` (панель), ``bot``, ``auto`` (фоновая задача).
    source: Mapped[str | None] = mapped_column(String(8), default=None)
    #: IP администратора — для разбора «кто заходил и откуда».
    ip: Mapped[str | None] = mapped_column(String(45), default=None)

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)


class AdminAccount(Base):
    """Учётная запись панели: владелец, модератор или поддержка.

    Пароль хранится только хэшем (PBKDF2-HMAC-SHA256). Пока таблица пуста,
    работает старый способ входа — пароль из ``ADMIN_PANEL_PASSWORD`` (владелец).
    """

    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="moderator")
    #: Telegram-ID: по нему действие можно связать с человеком в боте.
    tg_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    @property
    def name(self) -> str:
        return self.display_name or self.login


class UserNote(Base):
    """Заметка модератора о клиенте: контекст не теряется между сменами."""

    __tablename__ = "user_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    author: Mapped[str] = mapped_column(String(64), default="")
    author_role: Mapped[str] = mapped_column(String(16), default="")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)


class Alert(Base):
    """Алерт панели: «нода недоступна», «поступление без заказа», всплеск ошибок.

    Одинаковые алерты не дублируются: пока открыт алерт с тем же
    ``fingerprint``, новый не создаётся — вместо этого растёт ``repeat_count``.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(8), default="warn")  # err|warn|info
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str | None] = mapped_column(Text, default=None)
    #: Ключ дедупликации: например ``node:de`` или ``payment:199.13``.
    fingerprint: Mapped[str] = mapped_column(String(120), index=True, default="")
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), default=None)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|ack|resolved
    repeat_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    resolve_note: Mapped[str | None] = mapped_column(Text, default=None)


class Broadcast(Base):
    """Рассылка: переживает перезапуск бота и показывает результат в панели."""

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(String(16), default="active")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|failed|canceled
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class Downtime(Base):
    """Период простоя: дни, когда связь у абонентов не работала.

    Зачем отдельная таблица, а не запись в журнале: компенсация — это деньги
    (дни подписки), её нужно уметь посчитать, показать и не начислить дважды.
    Открытый период (``ended_at is None``) в системе может быть только один.
    """

    __tablename__ = "downtimes"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Сколько полных суток насчитали и начислили (0 — меньше суток или ещё идёт).
    days: Mapped[int] = mapped_column(Integer, default=0)
    #: Когда начисление выполнено. Стоит — значит период закрыт и повторно
    #: ничего не начислится, даже если команду повторить.
    granted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(160), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
