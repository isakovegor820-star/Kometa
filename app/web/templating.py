"""Настройка Jinja для веб-панели: один движок и один набор хелперов.

Шаблоны не должны заниматься арифметикой и склонениями: подписи статусов,
деньги «1 590 ₽», «5 мин назад» и русские окончания приходят из
:mod:`app.web.ui` как глобальные функции. Так одна и та же фраза выглядит
одинаково и в списке, и в карточке, и в CSV.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.web import ui

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _build() -> Jinja2Templates:
    engine = Jinja2Templates(directory=str(TEMPLATES_DIR))
    engine.env.globals.update(
        {
            "money": ui.money,
            "num": ui.num,
            "percent": ui.percent,
            "dt": ui.dt,
            "dt_short": ui.dt_short,
            "dt_day": ui.dt_day,
            "ago": ui.ago,
            "initials": ui.initials,
            "days_word": ui.days_word,
            "plural": ui.plural,
            "mask_secret": ui.mask_secret,
            "status_label": ui.status_pair,
            "event_label": ui.event_label,
            "provider_label": lambda code: ui.PROVIDER_LABELS.get(code or "", code or "—"),
            "kind_label": lambda code: ui.ORDER_KIND.get(code or "", code or "—"),
            "role_label": ui.role_label,
            "severity_label": lambda sev: ui.SEVERITY_LABELS.get(sev or "", sev or ""),
            "split_tags": ui.split_tags,
            "page_range": ui.page_range,
            "query_string": ui.query_string,
            "ORDER_STATUS": ui.ORDER_STATUS,
            "SUBSCRIPTION_STATUS": ui.SUBSCRIPTION_STATUS,
            "PROVIDER_LABELS": ui.PROVIDER_LABELS,
            "ADMIN_ACTION_LABELS": ui.ADMIN_ACTION_LABELS,
            "ROLES": ui.ROLES,
        }
    )
    # Фильтры доступны и как `| money`, и как `money(...)` — шаблоны пишутся
    # и так, и так, ломаться на этом не должны.
    engine.env.filters.update(
        {
            "money": ui.money,
            "num": ui.num,
            "dt": ui.dt,
            "dt_short": ui.dt_short,
            "ago": ui.ago,
            "initials": ui.initials,
            "mask": ui.mask_secret,
            "event_label": ui.event_label,
        }
    )
    return engine


templates = _build()
