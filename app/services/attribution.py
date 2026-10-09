"""Источник привлечения: откуда пришёл клиент и сколько он нам стоил.

Зачем это нужно. Рефералка и промокоды уже считают, *сколько* людей пришло, но
не отвечают на вопрос «какой канал стоит своих денег». Без метки источника
нельзя посчитать стоимость привлечения: любая платная акция превращается в
трату вслепую (разбор — ``docs/МАРКЕТИНГ-ЭКОНОМИКА.md``).

Метка ставится **один раз**, при первом входе, и не перезаписывается: так
канал, который нашёл человека, получает заслуженную заслугу, даже если потом
он пришёл по другой ссылке.

Формат deep-link: ``t.me/<bot>?start=<payload>``

* ``ref_<код>``    — пришёл по чужой реферальной ссылке;
* ``src_<канал>``  — размещение или кампания, например ``src_yt_ivanov``;
* ``gift_<код>``   — активировал подарочный сертификат;
* ``promo_<код>``  — пришёл за конкретным промокодом;
* ``sub_<токен>``  — вернулся из страницы подписки.

Источниками считаются только «первые касания». Прямые заходы в бота
(``/start`` без параметра) помечаются как ``direct`` — это обычно сарафан
и поиск по названию, их тоже полезно видеть отдельно.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Subscription, User, utcnow

#: Прямой заход: бот открыли по названию или из поиска.
SOURCE_DIRECT = "direct"
#: Пришёл по реферальной ссылке.
SOURCE_REFERRAL = "ref"
#: Пришёл из размещения или кампании (метка задаётся вручную в ссылке).
SOURCE_CAMPAIGN = "src"
#: Активировал подарочный сертификат.
SOURCE_GIFT = "gift"
#: Пришёл по промокоду из акции.
SOURCE_PROMO = "promo"
#: Вернулся со страницы подписки.
SOURCE_SUBSCRIPTION = "sub"
#: Пришёл по персональной ссылке под конкретного человека.
SOURCE_PERSONAL = "personal"

#: Все известные префиксы deep-link. Порядок важен: проверяем по очереди.
PREFIXES: tuple[tuple[str, str], ...] = (
    ("ref_", SOURCE_REFERRAL),
    ("src_", SOURCE_CAMPAIGN),
    ("p_", SOURCE_PERSONAL),
    ("gift_", SOURCE_GIFT),
    ("promo_", SOURCE_PROMO),
    ("sub_", SOURCE_SUBSCRIPTION),
)

#: Человекочитаемые названия источников для админки и отчётов.
SOURCE_TITLES: dict[str, str] = {
    SOURCE_DIRECT: "Пришли сами (сарафан, поиск)",
    SOURCE_REFERRAL: "Приглашение друга",
    SOURCE_CAMPAIGN: "Размещение / кампания",
    SOURCE_GIFT: "Подарочный сертификат",
    SOURCE_PROMO: "Промокод из акции",
    SOURCE_SUBSCRIPTION: "Со страницы подписки",
    SOURCE_PERSONAL: "Персональная ссылка",
}


def parse_payload(payload: str) -> tuple[str, str]:
    """Разобрать параметр ``start`` на (источник, уточнение).

    Пустой или неизвестный payload — это ``direct``: человек пришёл сам.
    """
    raw = (payload or "").strip()
    if not raw:
        return SOURCE_DIRECT, ""
    for prefix, source in PREFIXES:
        if raw.startswith(prefix):
            detail = raw[len(prefix):].strip()
            # Уточнение ограничено длиной колонки: обрезаем, а не падаем.
            return source, detail[:64]
    # Свободная метка без префикса — тоже кампания: так удобнее ссылаться
    # из внешних мест, где некогда придумывать формат.
    return SOURCE_CAMPAIGN, raw[:64]


def normalize_detail(detail: str) -> str:
    """Привести уточнение к предсказуемому виду для группировки в отчётах."""
    return " ".join((detail or "").split()).casefold()[:64]


async def record_source(session: AsyncSession, user: User, payload: str) -> str:
    """Зафиксировать источник первого касания. Возвращает сохранённый источник.

    Повторные вызовы ничего не меняют: первый канал остаётся за тем, кто
    действительно привёл человека. Исключение — уточнение «direct»: если
    человек сначала открыл бота без ссылки, а потом пришёл по реферальной,
    засчитываем рефералку, иначе пригласивший теряет заслуженную награду.
    """
    source, detail = parse_payload(payload)
    if user.source:
        if user.source not in ("", SOURCE_DIRECT):
            # Источник уже зафиксирован — первый канал сохраняет заслугу.
            return user.source
        if source == SOURCE_DIRECT:
            return user.source
    user.source = source
    user.source_detail = normalize_detail(detail)
    user.source_at = utcnow()
    await session.flush()
    return source


@dataclass(slots=True)
class SourceStats:
    """Итог по одному источнику привлечения.

    :param source: код источника (``ref``, ``src``, ``direct``…).
    :param detail: уточнение — конкретное размещение или код.
    :param joined: сколько человек пришло.
    :param trials: сколько из них взяло пробный доступ.
    :param payers: сколько оплатило хотя бы раз.
    :param revenue_rub: сколько денег они принесли (по оплаченным заказам).
    :param spend_rub: сколько мы на этот источник потратили.
    """

    source: str
    detail: str
    joined: int
    trials: int
    payers: int
    revenue_rub: float
    spend_rub: float = 0.0

    @property
    def title(self) -> str:
        base = SOURCE_TITLES.get(self.source, self.source)
        return f"{base} · {self.detail}" if self.detail else base

    @property
    def conversion(self) -> float:
        """Какая доля пришедших дошла до оплаты."""
        return self.payers / self.joined if self.joined else 0.0

    @property
    def cac_rub(self) -> float:
        """Стоимость одного платящего клиента из этого источника."""
        if not self.payers:
            return float("inf")
        return self.spend_rub / self.payers


async def source_stats(session: AsyncSession, spend: dict[str, float] | None = None) -> list[SourceStats]:
    """Собрать отчёт «источник → люди → оплаты → деньги».

    :param spend: расходы по источникам в рублях. Ключ — либо код источника
        (``src``), либо уточнение (``yt_ivanov``); уточнение приоритетнее,
        потому что расходы обычно привязаны к конкретному размещению.
    """
    spend = spend or {}

    def key(source: str | None, detail: str | None) -> str:
        return f"{source or SOURCE_DIRECT}|{detail or ''}"

    # Один проход по каждой метрике: «сколько пришло», «сколько взяло пробный»,
    # «сколько оплатило», «сколько денег принесли». Ключ — источник + уточнение.
    joined_map: dict[str, int] = {}
    for source, detail, count in (
        await session.execute(
            select(User.source, User.source_detail, func.count(User.id))
            .group_by(User.source, User.source_detail)
        )
    ).all():
        joined_map[key(source, detail)] = int(count or 0)

    trials_map: dict[str, int] = {}
    for source, detail, count in (
        await session.execute(
            select(User.source, User.source_detail, func.count(Subscription.id))
            .join(Subscription, Subscription.user_id == User.id)
            .group_by(User.source, User.source_detail)
        )
    ).all():
        trials_map[key(source, detail)] = int(count or 0)

    payers_map: dict[str, int] = {}
    revenue_map: dict[str, float] = {}
    for source, detail, payers, revenue in (
        await session.execute(
            select(
                User.source,
                User.source_detail,
                func.count(func.distinct(Order.user_id)),
                func.coalesce(func.sum(Order.amount_rub), 0),
            )
            .join(Order, Order.user_id == User.id)
            .where(Order.status == "paid")
            .group_by(User.source, User.source_detail)
        )
    ).all():
        payers_map[key(source, detail)] = int(payers or 0)
        revenue_map[key(source, detail)] = float(revenue or 0)

    result: list[SourceStats] = []
    for row_key, joined in joined_map.items():
        source, _, detail = row_key.partition("|")
        result.append(
            SourceStats(
                source=source,
                detail=detail,
                joined=joined,
                trials=trials_map.get(row_key, 0),
                payers=payers_map.get(row_key, 0),
                revenue_rub=revenue_map.get(row_key, 0.0),
                spend_rub=float(spend.get(detail) or spend.get(source) or 0),
            )
        )
    result.sort(key=lambda row: (-row.payers, -row.joined))
    return result


async def source_summary(session: AsyncSession) -> dict[str, int]:
    """Короткая сводка для дашборда: сколько людей без источника."""
    total = await session.scalar(select(func.count(User.id))) or 0
    unknown = await session.scalar(
        select(func.count(User.id)).where((User.source == "") | (User.source.is_(None)))
    ) or 0
    referral = await session.scalar(
        select(func.count(User.id)).where(User.source == SOURCE_REFERRAL)
    ) or 0
    return {"total": int(total), "unknown": int(unknown), "referral": int(referral)}
