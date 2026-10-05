"""Источники входящих платежей (выписки банка).

Контракт один: источник отдаёт список поступлений с момента ``since``.
Конкретные реализации — в модуле ``statements_sources.py``:

* ``CsvStatementSource`` — файл выписки (CSV/TXT), который банк или скрипт
  складывает в папку (удобно, когда у банка нет API);
* ``ImapStatementSource`` — почтовые уведомления банка (IMAP): банк присылает
  письмо «Поступление 199.13 ₽», мы разбираем его.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class IncomingPayment:
    """Одно поступление денег."""

    amount_kopecks: int
    received_at: datetime
    comment: str = ""
    counterparty: str = ""
    source: str = ""
    #: Уникальный идентификатор у источника (id письма, номер операции) —
    #: нужен, чтобы не обработать одно поступление дважды.
    external_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Ключ дедупликации: без него повторный опрос создаст дубли."""
        if self.external_id:
            return f"{self.source}:{self.external_id}"
        return f"{self.source}:{self.received_at.isoformat()}:{self.amount_kopecks}:{self.comment}"


class StatementSource(ABC):
    """Абстрактный источник выписки."""

    name: str = "base"

    @abstractmethod
    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        """Вернуть поступления, случившиеся после ``since``."""

    async def close(self) -> None:  # pragma: no cover - переопределяется при необходимости
        """Освободить ресурсы."""


class StatementError(RuntimeError):
    """Ошибка чтения выписки (нет доступа, неверный формат)."""
