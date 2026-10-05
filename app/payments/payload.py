"""Разбор payload'ов, которые мы передаём в платёжные системы.

Формат: `order:<id>`. Вынесено отдельно, чтобы бот не зависел от конкретного
провайдера (Stars/CryptoBot используют один и тот же payload).
"""

from __future__ import annotations

PAYLOAD_PREFIX = "order:"


def make_order_payload(order_id: int) -> str:
    return f"{PAYLOAD_PREFIX}{order_id}"


def parse_order_id_from_payload(payload: str | None) -> int | None:
    if not payload or not payload.startswith(PAYLOAD_PREFIX):
        return None
    raw = payload[len(PAYLOAD_PREFIX) :].strip()
    return int(raw) if raw.isdigit() else None
