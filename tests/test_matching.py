"""Тесты сопоставления платежей: суммы, копейки, коды заказов."""

from __future__ import annotations

import pytest

from app.payments.matching import (
    MAX_KOPECK_SIGNATURE,
    Money,
    allocate_signature,
    amounts_match,
    parse_amount_to_kopecks,
    parse_order_code,
)


def test_money_formats_with_and_without_kopecks():
    assert Money.from_rub(199).format() == "199"
    assert Money.from_rub(199, 13).format() == "199.13"
    assert Money.from_rub(199, 5).format() == "199.05"
    assert Money.from_rub(199, 5).rub == 199
    assert Money.from_rub(199, 5).signature == 5


def test_allocate_signature_skips_taken():
    assert allocate_signature(set()) == 1
    assert allocate_signature({1, 2, 3}) == 4
    assert allocate_signature(set(range(1, 100))) == 0  # свободных нет — матчим по комментарию


def test_allocate_signature_never_returns_zero_before_exhaustion():
    taken = {1, 2, 50, 99}
    signature = allocate_signature(taken)
    assert signature not in taken
    assert 1 <= signature <= MAX_KOPECK_SIGNATURE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("199.13", 19913),
        ("199,13", 19913),
        ("199", 19900),
        ("1 990,50 ₽", 199050),
        ("1.990,50 RUB", 199050),
        ("1\u00a0990,50 руб", 199050),
        ("0,01", 1),
        ("", None),
        (None, None),
        ("мусор", None),
        ("1.2.3", None),
    ],
)
def test_parse_amount_to_kopecks(text, expected):
    assert parse_amount_to_kopecks(text) == expected


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("Kometa 42", 42),
        ("kometa-42", 42),
        ("КОМЕТА 42", 42),
        ("Заказ 1234", 1234),
        ("order #77", 77),
        ("перевод другу", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_order_code(comment, expected):
    assert parse_order_code(comment) == expected


def test_amounts_match_with_tolerance():
    assert amounts_match(19913, 19913)
    assert not amounts_match(19900, 19913)
    assert amounts_match(19900, 19913, tolerance_kopecks=13)
