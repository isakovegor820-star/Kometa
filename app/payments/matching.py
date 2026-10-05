"""Сопоставление входящих платежей с заказами.

Идея автоподтверждения переводов (СБП/карта):

1. Каждому заказу на ручную оплату выдаётся **уникальная сумма** — базовые
   рубли плюс уникальные копейки (199 ₽ → 199.13 ₽). Двух одинаковых сумм
   среди активных заказов не бывает, поэтому поступление матчится однозначно.
2. Дополнительно просим указать в комментарии код заказа — это резервный
   способ, если банк не передаёт копейки или суммы всё-таки совпали.
3. Если платёж не удалось сопоставить — он не теряется: админ получает
   уведомление «пришло 199 ₽, заказ не найден».

Модуль намеренно без обращения к БД: только чистые функции, которые легко тестировать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Максимальная надбавка в копейках (0.01 … 0.99)
MAX_KOPECK_SIGNATURE = 99

#: Код заказа в комментарии: «Kometa 1234», «kometa-1234», «Комета 1234», «заказ 1234»
ORDER_CODE_PATTERNS = (
    re.compile(r"kometa[\s\-_#]*(\d{1,9})", re.IGNORECASE),
    re.compile(r"комета[\s\-_#]*(\d{1,9})", re.IGNORECASE),
    re.compile(r"заказ[\s\-_#]*(\d{1,9})", re.IGNORECASE),
    re.compile(r"order[\s\-_#]*(\d{1,9})", re.IGNORECASE),
)

_AMOUNT_CLEAN_RE = re.compile(r"[^\d,.\-]")


@dataclass(frozen=True, slots=True)
class Money:
    """Сумма в копейках — без float, чтобы не ловить ошибки округления."""

    kopecks: int

    @classmethod
    def from_rub(cls, rub: int, kopecks: int = 0) -> "Money":
        return cls(rub * 100 + kopecks)

    @property
    def rub(self) -> int:
        return self.kopecks // 100

    @property
    def signature(self) -> int:
        """Уникальная надбавка (0…99)."""
        return self.kopecks % 100

    def format(self, *, with_kopecks: bool = True) -> str:
        """«199.13» или «199» — как показывать пользователю."""
        if not with_kopecks or self.signature == 0:
            return str(self.rub)
        return f"{self.rub}.{self.signature:02d}"


def allocate_signature(taken: set[int]) -> int:
    """Подобрать свободные копейки для новой суммы.

    :param taken: уже занятые надбавки среди активных заказов.
    :returns: свободная надбавка 1…99 либо 0, если свободных нет
        (тогда матчинг пойдёт по комментарию и сумме без надбавки).
    """
    for signature in range(1, MAX_KOPECK_SIGNATURE + 1):
        if signature not in taken:
            return signature
    return 0


def parse_order_code(text: str | None) -> int | None:
    """Достать номер заказа из комментария платежа."""
    if not text:
        return None
    for pattern in ORDER_CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def parse_amount_to_kopecks(text: str | None) -> int | None:
    """Разобрать сумму из строки выписки: «1 990,50 ₽», «199.13», «199,13 RUB».

    Поддерживает оба разделителя дробной части и пробелы/неразрывные пробелы
    как разделители тысяч.
    """
    if not text:
        return None
    cleaned = _AMOUNT_CLEAN_RE.sub("", text.replace("\u00a0", "").replace(" ", ""))
    if not cleaned:
        return None

    # Если есть и точка, и запятая — запятая почти всегда дробная (1.990,50)
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")

    if cleaned.count(".") > 1:
        return None
    try:
        rub_text, _, fraction = cleaned.partition(".")
        fraction = (fraction + "00")[:2]
        return int(rub_text) * 100 + int(fraction)
    except ValueError:
        return None


def amounts_match(paid_kopecks: int, order_kopecks: int, *, tolerance_kopecks: int = 0) -> bool:
    """Совпадает ли поступление с суммой заказа.

    :param tolerance_kopecks: допуск — некоторые банки округляют копейки,
        поэтому по умолчанию 0 (строго), но администратор может разрешить допуск.
    """
    return abs(paid_kopecks - order_kopecks) <= tolerance_kopecks
