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
    Float,
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
    #: Источник привлечения: откуда человек пришёл — «ref», «promo», «placement».
    #: Ставится один раз, при первом входе. Нужен, чтобы считать стоимость
    #: привлечения по каналам, а не «в среднем по больнице».
    source: Mapped[str] = mapped_column(String(32), default="")
    #: Уточнение источника: код размещения, название канала, id кампании.
    source_detail: Mapped[str] = mapped_column(String(64), default="")
    #: Когда источник зафиксирован (первый вход).
    source_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Партнёр, который привёл человека (ссылка ``?start=src_<код>``).
    #: Пусто — пришёл сам, по рефералке друга или по акции.
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), default=None, index=True)
    #: Персональная ссылка, по которой пришёл человек (``?start=p_<код>``).
    #: Скидка такой ссылки применяется к его заказам автоматически.
    personal_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_links.id"), default=None, index=True
    )
    #: Когда последний раз отправляли автосценарий (подсказка, win-back, апселл).
    #: Нужно, чтобы бот не превращался в спамера: между сообщениями держим паузу.
    last_lifecycle_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Код последнего отправленного сценария — для отчёта «что сработало».
    last_lifecycle_kind: Mapped[str] = mapped_column(String(32), default="")

    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        # Явно: у orders два внешних ключа на users (покупатель и тот, кто
        # активировал подарок), и связь строится по покупателю.
        foreign_keys="Order.user_id",
    )

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

    #: Когда админам сообщили, что по **закрытому** заказу всё-таки пришли деньги
    #: (заказ отменён или истёк, а платёж состоялся: бот спал, вебхук не дошёл).
    #: Нужно, чтобы фоновая проверка не писала об одном платеже каждые 2 минуты,
    #: и чтобы админ вообще узнал о деньгах, за которые ничего не выдано.
    payment_alerted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    # --- выдача доступа после оплаты ---------------------------------------
    #: До какого срока подписка должна была продлиться. Считается в момент
    #: захвата заказа — **до** обращения к панели. По нему проверяем факт:
    #: «панель продлила хотя бы до этой даты». Без цели нельзя отличить
    #: «панель ничего не сделала» от «панель продлила, но ответ потерялся».
    grant_target_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Когда доступ фактически выдан (панель подтвердила срок). Пусто у
    #: оплаченного заказа — значит выдача не состоялась, и её повторит
    #: фоновая задача: деньги приняты, клиент без доступа не остаётся.
    granted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Сколько раз пытались выдать доступ. Ограничиваем: бесконечно бить в
    #: мёртвую панель нельзя, после лимита нужен человек (алерт уже поднят).
    grant_attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: Текст последней ошибки выдачи: виден в админке и в алерте — «панель не
    #: ответила», «выдала до 12.11, а ожидали минимум 12.12» и т.п.
    grant_last_error: Mapped[str] = mapped_column(String(300), default="")

    # --- подарочный сертификат ---------------------------------------------
    #: Токен подарка (``KOMETA-GIFT-XXXXXXXX``). Заполнен — заказ подарочный:
    #: оплата покупателя выдаёт дни не ему, а получателю по этому токену.
    gift_token: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    #: tg_id получателя, если покупатель его указал. Пусто — подарили ссылкой.
    gift_recipient_tg_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    #: Что написать на открытке.
    gift_message: Mapped[str | None] = mapped_column(String(200), default=None)
    #: Когда получатель активировал подарок. Пусто — ещё не активирован.
    gift_activated_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Партнёр, с платежа которого начисляется выплата. Снимок на момент оплаты:
    #: если партнёра потом удалят, история расчётов не поедет.
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), default=None, index=True)
    #: Сколько начислено партнёру за этот платёж (рубли). Копится один раз.
    partner_reward_rub: Mapped[float] = mapped_column(Float, default=0.0)

    #: Кто активировал (id пользователя), чтобы подарок нельзя было использовать дважды.
    #: Отдельный внешний ключ на users: из-за него связь «заказ → покупатель»
    #: ниже задаётся явно через ``foreign_keys``.
    gift_activated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)

    #: Покупатель заказа. foreign_keys указан явно: у таблицы есть второй
    #: внешний ключ на users (``gift_activated_by``), и без этого SQLAlchemy
    #: не понимает, по какому из них строить связь.
    user: Mapped[User] = relationship(back_populates="orders", foreign_keys=[user_id])

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
    #: Сколько дней пригласивший получил за продления друга (вторая и следующие
    #: оплаты). Привязывает рефералку к удержанию: пока друг платит — дни идут.
    renewal_bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    #: Сколько раз друг оплатил продление. По счётчику видно, кто из
    #: приглашённых остался, а кто ушёл после первого месяца.
    renewals_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Когда последний раз начисляли награду за продление.
    renewal_rewarded_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Partner(Base):
    """Партнёр сервиса: реферал, блогер, админ чата, реселлер.

    Зачем отдельная сущность, а не промокод. У партнёра есть **своя ссылка**,
    свой процент скидки и, главное, **выплата ему** — процент с платежей
    приведённых людей или фиксированная сумма за каждую оплату. Промокод без
    партнёра этого не выражает: он даёт скидку, но не отвечает на вопрос
    «сколько мы должны этому человеку».

    Что отслеживается (см. ``app/services/partners.py``):

      * **переходы** — сколько людей открыли бота по ссылке ``?start=src_<код>``;
      * **регистрации** — сколько из них осталось в боте (``users.partner_id``);
      * **оплаты** — сколько привели денег (``orders`` с их платежами);
      * **выручка** — сумма оплат этих людей;
      * **наша выплата** — процент с платежей или фикс за каждую оплату;
      * **ROI** — сколько рублей пришло на каждый рубль выплаты.

    Выплата растёт с **каждым** платежом приведённого человека, а не только с
    первым: партнёру выгодно приводить тех, кто остаётся, а нам — платить за
    удержание, а не за регистрацию.
    """

    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Название для админки: «Иван, канал про удалёнку».
    name: Mapped[str] = mapped_column(String(64))
    #: Код в ссылке: ``t.me/<bot>?start=src_<slug>``. Латиница, цифры, дефис.
    slug: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: Скидка, которую получает приведённый человек, %. 0 — без скидки.
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    #: Потолок скидки в рублях (0 — без потолка).
    discount_max_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Как считаем выплату партнёру: ``percent`` — процент с платежей,
    #: ``fixed`` — фиксированная сумма за каждую оплату, ``none`` — без выплаты.
    reward_kind: Mapped[str] = mapped_column(String(16), default="percent")
    #: Значение выплаты: проценты (``percent``) или рубли (``fixed``).
    reward_value: Mapped[float] = mapped_column(Float, default=30.0)
    #: Сколько уже выплачено вручную (рубли). Остальное — текущий долг.
    paid_out_rub: Mapped[float] = mapped_column(Float, default=0.0)
    #: Когда последний раз рассчитывались с партнёром.
    paid_out_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    @property
    def reward_text(self) -> str:
        """Выплата словами — для таблицы в админке."""
        if self.reward_kind == "none":
            return "без выплаты"
        if self.reward_kind == "fixed":
            return f"{self.reward_value:.0f} ₽ за оплату"
        return f"{self.reward_value:g} % с платежей"


class PersonalLink(Base):
    """Персональная ссылка под конкретного человека.

    Чем отличается от партнёрской ссылки и от промокода.

    * **Партнёрская ссылка** одна на партнёра: любой, кто по ней пришёл, получает
      условия партнёра, и партнёру платим за каждого.
    * **Промокод** человек должен ввести руками.
    * **Персональная ссылка** — под одного адресата: своя скидка, свой срок, свой
      лимит. Скидка применяется автоматически, вводить ничего не нужно.

    Зачем это нужно на практике:

      * блогер просит «дай ссылку под меня с 40 %» — под каждый пост своя ссылка,
        и видно, какой именно пост сработал, а не «канал в целом»;
      * другу, коллеге или админу чата можно дать особые условия, не меняя общие
        правила для всех клиентов;
      * у ссылки есть срок: акция на выходные не останется рабочей через полгода;
      * у ссылки есть лимит активаций: «только для первых 20 человек».

    Ссылка вида ``t.me/<bot>?start=p_<код>``. Владелец — либо партнёр
    (``partner_id``), либо просто подпись (``owner_name``) для памяти.
    """

    __tablename__ = "personal_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Код в ссылке: ``?start=p_<код>``. Латиница, цифры, дефис.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: Название для админки: «Пост у Ивана 12.10» или «Сергей, коллега».
    title: Mapped[str] = mapped_column(String(64))
    #: Кому выдали — для памяти. Клиент этого не видит.
    owner_name: Mapped[str] = mapped_column(String(64), default="")
    #: Партнёр, под которого сделана ссылка (пусто — частное лицо).
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), default=None, index=True)
    #: Скидка по этой ссылке, %. Своя: у каждого адресата может быть разная.
    discount_percent: Mapped[int] = mapped_column(Integer, default=20)
    #: Потолок скидки в рублях (0 — без потолка).
    discount_max_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Сколько человек могут активировать ссылку (0 — без ограничения).
    uses_limit: Mapped[int] = mapped_column(Integer, default=0)
    #: Сколько уже активировали.
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    #: До какого момента ссылка работает (пусто — бессрочно).
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    @property
    def uses_left(self) -> int | None:
        """Сколько активаций осталось (None — без ограничения)."""
        if not self.uses_limit:
            return None
        return max(0, self.uses_limit - (self.uses_count or 0))

    @property
    def is_expired(self) -> bool:
        """Срок ссылки вышел."""
        return self.expires_at is not None and self.expires_at <= utcnow()

    @property
    def is_usable(self) -> bool:
        """Ссылку ещё можно активировать."""
        if not self.is_active or self.is_expired:
            return False
        if self.uses_limit and (self.uses_count or 0) >= self.uses_limit:
            return False
        return True


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
    #: referral — код клиента, admin — код из админки, partner — код партнёра,
    #: personal — код персональной ссылки под конкретного человека.
    kind: Mapped[str] = mapped_column(String(16), default="admin")
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    #: Партнёрский код: привязан к партнёру, а не к обычному пользователю.
    #: ``owner_user_id`` у таких кодов пусто — заслуга идёт партнёру.
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), default=None)
    #: Код персональной ссылки: своя скидка под конкретного адресата.
    personal_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_links.id"), default=None
    )
    percent: Mapped[int] = mapped_column(Integer, default=50)
    #: Потолок скидки в рублях: 0 — без потолка.
    max_discount_rub: Mapped[int] = mapped_column(Integer, default=0)
    #: Скидка только на первую оплату клиента (для акций можно выключить).
    first_only: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Многоразовый код: работает даже если клиент уже пользовался скидкой.
    #: Нужен для тестовых прогонов и компенсаций: обычное правило «одна скидка
    #: на аккаунт за всю жизнь» блокирует повторное применение любого кода.
    repeatable: Mapped[bool] = mapped_column(Boolean, default=False)
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

    @property
    def subscription_base(self) -> str:
        """Адрес сервиса подписок: явный ``sub_base`` или собранный из ``host``.

        Пусто — конфиги этой локации собрать нечем (``get_configs`` панели
        требует адрес подписок), и страна **молча** пропадёт из подписки
        клиента. Поэтому значение видно в админке, а включить такую ноду форма
        не даёт: пустой адрес — это не «мелочь», а потерянная локация.
        """
        if self.sub_base:
            return self.sub_base
        return f"http://{self.host}:2096/sub/" if self.host else ""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)

    last_check_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Причина последней неудачной проверки: «в панели не найдены инбаунды [3]…»,
    #: «HTTP 401». Без неё админка показывала зелёное «отвечает» рядом с
    #: алертом и не давала понять, что именно чинить.
    last_check_error: Mapped[str] = mapped_column(String(300), default="")

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
    #: Шаг, на котором остановилась последняя проба: ``tcp``/``tls`` — порт
    #: проверен, ``panel``/``config`` — проба не состоялась (панель не отдала
    #: инбаунды, не заполнен host, нет TCP-инбаундов). По нему интерфейсы
    #: отличают «порт не пускает» от «проба не выполнена»: по тексту ошибки это
    #: делать нельзя — таймаут TCP тоже пишет текст.
    last_probe_stage: Mapped[str] = mapped_column(String(16), default="")
    #: Причина последней неудачной пробы — и «порт не пускает», и «проба не
    #: состоялась». Пусто — проба прошла.
    last_probe_error: Mapped[str] = mapped_column(String(300), default="")
    #: Замер по КАЖДОМУ TCP-порту ноды, JSON-строкой:
    #: ``[{"port": 443, "ok": true, "ms": 12}, {"port": 8443, "ok": false, …}]``.
    #: Зачем, если есть ``last_probe_ok``: он отвечает «жив ли хоть один порт»,
    #: а оператору нужен ответ «какой именно не пускает». У ноды в подписке
    #: несколько портов, и «ок» при одном открытом скрывало закрытый второй —
    #: из-за этого диагноз «работает» и жалоба клиента не сходились.
    last_probe_ports: Mapped[str] = mapped_column(Text, default="")


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
