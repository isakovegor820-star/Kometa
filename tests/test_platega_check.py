"""Тесты самопроверки подключения Platega (``app/tools/platega_check.py``).

Инструмент решает одну задачу: не дать включить продажи, пока не подтверждены
единицы суммы и публичный адрес вебхука. Поэтому проверяем ровно это —
определение единиц по ответу API и требования к Callback URL.
"""

from __future__ import annotations

import httpx
import pytest

from app.payments.platega import PlategaProvider
from app.tools.platega_check import (
    Report,
    callback_problem,
    callback_url,
    probe_one_method,
    verdict_by_echo,
    verdict_by_rate,
)


def make_provider(handler) -> PlategaProvider:  # noqa: ANN001
    """Провайдер поверх MockTransport: наружу (в сеть) не ходим."""
    return PlategaProvider(
        "34c1b38c-6068-4196-bdd4-8fea587e8d75",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://vpn.example.ru", "https://vpn.example.ru/payments/platega/webhook"),
        ("https://vpn.example.ru/", "https://vpn.example.ru/payments/platega/webhook"),
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090/payments/platega/webhook"),
        ("", ""),
    ],
)
def test_callback_url(base, expected):
    assert callback_url(base) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8090/payments/platega/webhook",
        "https://localhost/payments/platega/webhook",
        "https://0.0.0.0:8090/payments/platega/webhook",
        "https://192.168.1.10/payments/platega/webhook",
    ],
)
def test_local_addresses_are_rejected(url):
    """Platega не принимает localhost и приватные адреса — это блокер, а не мелочь."""
    assert callback_problem(url)


def test_public_address_is_accepted():
    assert callback_problem("https://vpn.example.ru/payments/platega/webhook") == ""
    # Публичный IP тоже годится.
    assert callback_problem("https://185.221.160.7/payments/platega/webhook") == ""


def test_empty_public_base_is_reported():
    assert "PUBLIC_BASE_URL" in callback_problem("")


# ------------------------------------------------------------ единицы суммы
def test_rate_verdict_detects_rubles():
    """Отправили 100, списали ~1.07 USDT по курсу 93.45 — значит это рубли."""
    found = verdict_by_rate(100, 1.07, 93.45)

    assert found is not None
    assert found[0] == "rubles"
    assert "рубли" in found[1]


def test_rate_verdict_detects_kopecks():
    """Отправили 100, списали ~0.0107 USDT — API понял число как копейки."""
    found = verdict_by_rate(100, 0.0107, 93.45)

    assert found is not None
    assert found[0] == "kopecks"
    assert "копейки" in found[1]


def test_rate_verdict_needs_data():
    assert verdict_by_rate(100, None, 93.45) is None
    assert verdict_by_rate(100, 1.07, None) is None
    assert verdict_by_rate(0, 1.07, 93.45) is None


def test_rate_verdict_ignores_ambiguous_amount():
    """Сумма, не похожая ни на рубли, ни на копейки, не даёт вывода."""
    assert verdict_by_rate(100, 5.0, 93.45) is None


@pytest.mark.parametrize(
    ("details", "unit"),
    [
        ({"amount": 100, "currency": "RUB"}, "rubles"),
        ("100 RUB", "rubles"),
        ({"amount": 1, "currency": "RUB"}, "kopecks"),
        ("1.00 RUB", "kopecks"),
    ],
)
def test_echo_verdict(details, unit):
    """Запасной признак — что API вернул в paymentDetails."""
    found = verdict_by_echo(100, details)

    assert found is not None and found[0] == unit


def test_echo_verdict_survives_garbage():
    assert verdict_by_echo(100, "не число") is None
    assert verdict_by_echo(100, None) is None


def test_report_text_lists_issues_and_warnings():
    report = Report(lines=["API: ok"], warnings=["мелочь"], issues=["блокер"])

    text = report.as_text()

    assert "не готова" in text
    assert "блокер" in text and "мелочь" in text

    report.ok = True
    assert "готова" in report.as_text()


async def test_run_names_the_missing_field(monkeypatch):
    """Диагноз должен называть, чего именно не хватает.

    «Заполните оба поля» и «нет только ключа» — разные задачи, и вторая
    решается одним копированием из кабинета.
    """
    from app.config import get_settings
    from app.tools import platega_check

    settings = get_settings()
    monkeypatch.setattr(settings, "platega_merchant_id", "34c1b38c-6068-4196-bdd4-8fea587e8d75")
    monkeypatch.setattr(settings, "platega_secret", "")

    report = await platega_check.run(probe=False)

    assert not report.ok
    assert any("PLATEGA_SECRET" in issue for issue in report.issues)
    assert any("на месте" in line for line in report.lines)


async def test_run_warns_about_non_uuid_merchant_id(monkeypatch):
    """ID пользователя вместо ID мерчанта — частая путаница, предупреждаем."""
    from app.config import get_settings
    from app.tools import platega_check

    settings = get_settings()
    monkeypatch.setattr(settings, "platega_merchant_id", "12345")
    monkeypatch.setattr(settings, "platega_secret", "secret")

    async def no_network(provider, report):  # noqa: ANN001, ARG001
        """Дальше инструмент пошёл бы в API — в тестах сеть не трогаем."""
        report.issues.append("офлайн-тест: API не опрашиваем")
        return False

    monkeypatch.setattr(platega_check, "check_credentials", no_network)

    report = await platega_check.run(probe=False)

    assert any("не похож на UUID" in warning for warning in report.warnings)


# ------------------------------------------------- методы: включён или нет
async def test_probe_one_method_reports_disabled_method():
    """Метод есть в API, но не подключён к кассе — кнопка в боте будет падать.

    Живой пример 08.10.2026: метод 11 (карты) отвечал
    ``400 Wrong input parameters``, пока менеджер его не подключил.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "Common:VAL_0001",
                "message": "Wrong input parameters",
                "data": [{"key": "paymentMethod", "message": "Card"}],
            },
        )

    provider = make_provider(handler)
    enabled, note = await probe_one_method(provider, 11, 100)

    assert enabled is False
    assert "не подключён" in note and "Карта МИР" in note
    await provider.close()


async def test_probe_one_method_reports_client_commission():
    """Комиссия сверху — клиент увидит не ту цену, что обещал бот."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "transactionId": "tx-1",
                    "redirect": "https://pay.platega.io/p/tx-1",
                    "status": "PENDING",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "tx-1",
                "status": "PENDING",
                "paymentDetails": {"amount": 108.0, "currency": "RUB"},
                "comission": 8.0,
                "paymentMethod": "SBPQR",
            },
        )

    provider = make_provider(handler)
    enabled, note = await probe_one_method(provider, 2, 100)

    assert enabled is True
    assert "клиент увидит 108 ₽" in note
    assert "платит клиент" in note
    await provider.close()


async def test_probe_one_method_reports_exact_amount():
    """Комиссия на мерчанте — клиент платит ровно запрошенное."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"transactionId": "tx-2", "redirect": "https://pay/p", "status": "PENDING"})
        return httpx.Response(
            200,
            json={"id": "tx-2", "status": "PENDING", "paymentDetails": {"amount": 100.0, "currency": "RUB"}},
        )

    provider = make_provider(handler)
    enabled, note = await probe_one_method(provider, 2, 100)

    assert enabled is True
    assert "ровно столько" in note
    await provider.close()


async def test_run_accepts_webhook_off_mode(monkeypatch):
    """Режим «без сервера»: localhost — это выбор, а не блокер.

    Бот на домашнем компьютере без домена подтверждает оплату опросом, и
    `platega_check` не должен из-за этого навсегда считать подключение неготовым.
    """
    from app.config import get_settings
    from app.tools import platega_check

    settings = get_settings()
    monkeypatch.setattr(settings, "platega_merchant_id", "34c1b38c-6068-4196-bdd4-8fea587e8d75")
    monkeypatch.setattr(settings, "platega_secret", "secret")
    monkeypatch.setattr(settings, "platega_use_webhook", False)
    monkeypatch.setattr(settings, "public_base_url", "http://127.0.0.1:8090")

    async def fake_credentials(provider, report):  # noqa: ANN001, ARG001
        report.lines.append("  • доступы приняты (заглушка)")
        return True

    monkeypatch.setattr(platega_check, "check_credentials", fake_credentials)

    report = await platega_check.run(probe=False)

    assert not any("localhost" in issue for issue in report.issues)
    assert any("опросом" in line for line in report.lines)
    assert report.ok, report.as_text()


async def test_run_still_requires_public_url_with_webhook(monkeypatch):
    """С включённым вебхуком localhost остаётся блокером."""
    from app.config import get_settings
    from app.tools import platega_check

    settings = get_settings()
    monkeypatch.setattr(settings, "platega_merchant_id", "34c1b38c-6068-4196-bdd4-8fea587e8d75")
    monkeypatch.setattr(settings, "platega_secret", "secret")
    monkeypatch.setattr(settings, "platega_use_webhook", True)
    monkeypatch.setattr(settings, "public_base_url", "http://127.0.0.1:8090")

    async def fake_credentials(provider, report):  # noqa: ANN001, ARG001
        return True

    monkeypatch.setattr(platega_check, "check_credentials", fake_credentials)

    report = await platega_check.run(probe=False)

    assert any("localhost" in issue for issue in report.issues)
    assert not report.ok
