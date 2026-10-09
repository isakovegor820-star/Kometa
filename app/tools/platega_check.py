"""Проверка подключения Platega: доступы, баланс, методы, вебхук, единицы суммы.

Зачем: у Platega нет песочницы, доступы выдают в кабинете, а API за 2026 год
успело поменяться (методы оплаты, генерация ``id`` транзакции). Ошибка в
единицах суммы стоит либо 100-кратной переплаты клиента, либо бесплатного
доступа — поэтому подключение должно заканчиваться не «вроде работает», а
проверкой, которая либо подтверждает настройки, либо называет, что исправить.

Запуск из корня проекта::

        .venv/bin/python -m app.tools.platega_check                 # только чтение
        .venv/bin/python -m app.tools.platega_check --probe         # + тестовая транзакция
        .venv/bin/python -m app.tools.platega_check --probe --method 2 --amount 100

Что делает ``--probe``:

1. создаёт **настоящую** транзакцию на маленькую сумму (никто её не оплачивает,
      ссылка сама истечёт через 15 минут — деньги не двигаются);
2. читает её статус через ``GET /transaction/{id}``;
3. по ``amountUsdt`` и курсу определяет, в каких единицах API ждёт сумму —
      рублях или копейках, — и сравнивает с ``PLATEGA_AMOUNT_UNIT``;
4. записывает подтверждённые единицы в ``data/platega_probe.json``.

Код возврата 1, если подключение не готово: нет доступов, API не принял
заголовки, в ``PLATEGA_METHODS`` мусор, вебхук смотрит в localhost или единицы
суммы не подтверждены. Удобно втыкать в дежурную проверку перед включением
продаж.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.payments.platega import (
        DEFAULT_CODE,
        DEFAULT_TITLE,
        LEGACY_METHOD_ALIASES,
        PAYMENT_METHOD_TITLES,
        PROD_URL,
        VALID_METHODS,
        PaymentError,
        PlategaProvider,
        normalize_payment_method,
)

#: Путь вебхука: совпадает с роутером ``app/web/payments.py``.
WEBHOOK_PATH = "/payments/platega/webhook"

#: Где запоминаем результат живой проверки единиц суммы.
PROBE_STATE_FILE = Path("data/platega_probe.json")

#: Хосты, по которым Platega до нас не достучится.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]")

#: Насколько сильно сумма из ответа может отличаться от ожидаемой, чтобы
#: считать догадку подтверждённой. Расхождение «рубли против копеек» — ровно
#: 100 раз, так что 25 % запаса хватает с головой и не ловит шум курса.
_TOLERANCE = 0.25


@dataclass(slots=True)
class Report:
        """Итог проверки: что увидели и что мешает включить приём платежей."""

        ok: bool = False
        lines: list[str] = field(default_factory=list)
        issues: list[str] = field(default_factory=list)
        warnings: list[str] = field(default_factory=list)

        def as_text(self) -> str:
                head = "✅ Platega готова к приёму платежей" if self.ok else "⚠️ Platega пока не готова"
                parts = [head, *self.lines]
                if self.warnings:
                        parts.append("")
                        parts.append("Предупреждения:")
                        parts.extend(f"  • {w}" for w in self.warnings)
                if self.issues:
                        parts.append("")
                        parts.append("Что мешает:")
                        parts.extend(f"  ❌ {i}" for i in self.issues)
                return "\n".join(parts)


def callback_url(public_base_url: str) -> str:
        """Адрес вебхука, который надо вписать в кабинет Platega."""
        base = (public_base_url or "").strip().rstrip("/")
        return f"{base}{WEBHOOK_PATH}" if base else ""


def callback_problem(url: str) -> str:
        """Почему Platega не сможет достучаться до этого адреса (пусто — сможет).

        Platega принимает только публичный HTTPS: localhost, loopback и приватные
        диапазоны (``10/8``, ``172.16/12``, ``192.168/16``, ``127/8``) запрещены —
        адрес с таким хостом просто не сохранится в кабинете.
        """
        if not url:
                return "PUBLIC_BASE_URL не заполнен — вебхук некуда присылать"
        host = url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
        host = host.split(":", 1)[0].strip("[]").lower()
        if host in _LOCAL_HOSTS:
                return (
                        "вебхук смотрит в localhost — Platega снаружи до него не дойдёт; "
                        "нужен публичный адрес (сервер, домен или туннель)"
                )
        try:
                address = ipaddress.ip_address(host)
        except ValueError:
                return ""
        if address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
                return (
                        f"адрес {host} приватный — Platega принимает только публичные IP и домены; "
                        "нужен сервер с внешним адресом или туннель"
                )
        return ""


def _fmt_money(value: Any) -> str:
        try:
                return f"{float(value):,.2f}".replace(",", " ")
        except (TypeError, ValueError):
                return str(value)


def _number(value: Any) -> float | None:
        """Достать число из ``100``, ``"100 RUB"`` или ``{"amount": 100}``."""
        if isinstance(value, bool) or value is None:
                return None
        if isinstance(value, (int, float)):
                return float(value)
        if isinstance(value, dict):
                return _number(value.get("amount"))
        if isinstance(value, str):
                # «1 000,50 RUB» и «1 000.50 RUB» → 1000.5: пробелы-разделители
                # разрядов убираем до поиска числа, иначе «1 000,50» распадается на 1 и 000,50.
                cleaned = value.replace("\u00a0", "").replace(" ", "")
                for chunk in re.findall(r"-?\d+(?:[.,]\d+)?", cleaned):
                        try:
                                return float(chunk.replace(",", "."))
                        except ValueError:
                                continue
        return None


def verdict_by_rate(amount_sent: float, amount_usdt: Any, rate: Any) -> tuple[str, str] | None:
        """Определить единицы суммы по списанному USDT и курсу.

        Возвращает ``(единицы, объяснение)`` или ``None``, если данных не хватило.
        Если API понял нашу сумму как рубли, то с баланса USDT спишется
        ``amount_sent / rate``; если как копейки — в 100 раз меньше.
        """
        usdt = _number(amount_usdt)
        kurs = _number(rate)
        if not usdt or not kurs or amount_sent <= 0:
                return None
        rub_actual = usdt * kurs
        as_rubles = abs(rub_actual - amount_sent) / amount_sent <= _TOLERANCE
        as_kopecks = abs(rub_actual - amount_sent / 100) / (amount_sent / 100) <= _TOLERANCE
        if as_rubles and not as_kopecks:
                return "rubles", f"отправили {amount_sent:g} → списывается {rub_actual:.2f} ₽: API понял это как рубли"
        if as_kopecks and not as_rubles:
                return "kopecks", (
                        f"отправили {amount_sent:g} → списывается {rub_actual:.2f} ₽: API понял это как копейки"
                )
        return None


def verdict_by_echo(amount_sent: float, details: Any) -> tuple[str, str] | None:
        """Запасной признак: что API вернул в ``paymentDetails``.

        Строка ``"100 RUB"`` означает, что число ушло как рубли; ``"1 RUB"`` при
        отправленных 100 — что как копейки. Признак слабее USDT-курса, поэтому
        используется только если тот не сработал.
        """
        echo = _number(details)
        if not echo or amount_sent <= 0:
                return None
        if abs(echo - amount_sent) / amount_sent <= _TOLERANCE:
                return "rubles", f"в ответе paymentDetails={details!r} — число не пересчитывали, значит это рубли"
        if abs(echo - amount_sent / 100) / (amount_sent / 100) <= _TOLERANCE:
                return "kopecks", f"в ответе paymentDetails={details!r} — число уменьшили в 100 раз, значит копейки"
        return None


def read_probe_state() -> dict[str, Any]:
        """Прошлый результат ``--probe`` (или пустой словарь)."""
        try:
                data = json.loads(PROBE_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
                return {}
        return data if isinstance(data, dict) else {}


def write_probe_state(payload: dict[str, Any]) -> None:
        PROBE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROBE_STATE_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


async def check_credentials(provider: PlategaProvider, report: Report) -> bool:
        """``GET /balance/all`` — принимает ли API наши заголовки."""
        try:
                balances = await provider.fetch_balances()
        except PaymentError as exc:
                report.issues.append(f"API не принял доступы: {exc}")
                return False

        if not balances:
                report.lines.append("  • доступы приняты, но баланс пуст (это нормально до первых продаж)")
                return True

        report.lines.append("  • доступы приняты. Балансы:")
        for item in balances:
                frozen = item.get("frozenBalance")
                tail = f", заморожено {_fmt_money(frozen)}" if frozen else ""
                report.lines.append(f"      {item.get('currency', '?')}: {_fmt_money(item.get('amount'))}{tail}")
        return True


async def probe_one_method(
        provider: PlategaProvider, method: int, raw_amount: float
) -> tuple[bool, str]:
        """Проверить, подключён ли метод к кассе, и сколько заплатит клиент.

        Метод может быть в списке API, но не подключён менеджером: тогда запрос
        падает с ``400 Wrong input parameters``, а у клиента остаётся кнопка,
        которая не работает никогда. Отдельно смотрим комиссию: если на форме
        сумма больше запрошенной, комиссию переложили на клиента — и он увидит
        не ту цену, которую обещал бот.
        """
        probe = PlategaProvider(
                merchant_id=provider.merchant_id,
                secret=provider.secret,
                payment_method=method,
                base_url=provider.base_url,
                amount_unit="rubles",
                send_metadata=provider.send_metadata,
                return_url=provider.return_url,
                failed_url=provider.failed_url,
                client=provider.client,
        )
        title = PAYMENT_METHOD_TITLES.get(method, (DEFAULT_CODE, DEFAULT_TITLE))[1]
        try:
                invoice = await probe.create_invoice(
                        0,
                        int(raw_amount) or 1,
                        "Kometa: проверка метода оплаты",
                        exact_kopecks=int(round(raw_amount * 100)),
                )
        except PaymentError as exc:
                return False, f"метод {method} ({title}) не подключён: {exc}"

        note = f"метод {method} ({title}): включён"
        try:
                check = await probe.check_payment(invoice.external_id)
        except PaymentError:
                return True, note
        raw = check.raw if isinstance(check.raw, dict) else {}
        shown = _number(raw.get("paymentDetails"))
        commission = _number(raw.get("comission"))
        if shown:
                note += f", клиент увидит {shown:g} ₽"
                if commission:
                        note += f" (комиссия {commission:g} ₽)"
                if shown > raw_amount + 0.01:
                        note += " — комиссия сверху, платит клиент"
                elif shown <= raw_amount + 0.01:
                        note += " — ровно столько, сколько просили"
        return True, note


async def probe_amount_unit(
        provider: PlategaProvider,
        report: Report,
        method: int,
        raw_amount: float,
        rate_fallback: float,
) -> str | None:
        """Создать тестовую транзакцию и определить, в чём API ждёт сумму.

        В тело уходит ровно ``raw_amount`` — то самое число, которое в бою
        собирается из ``PLATEGA_AMOUNT_UNIT``. Настройку при этом не подставляем:
        проверить надо API, а не себя. Сумма копеечная (ссылка на 1–100 ₽), никто
        её не оплачивает, и через 15 минут она истекает сама.
        """
        unit_before, code_before, title_before = provider.amount_unit, provider.code, provider.title
        probe = PlategaProvider(
                merchant_id=provider.merchant_id,
                secret=provider.secret,
                payment_method=method,
                base_url=provider.base_url,
                # rubles + exact_kopecks=raw*100 → в запрос уходит ровно raw_amount.
                amount_unit="rubles",
                send_metadata=provider.send_metadata,
                return_url=provider.return_url,
                failed_url=provider.failed_url,
                client=provider.client,
        )
        report.lines.append(
                f"  • пробный платёж: метод {method} ({probe.title}), в запрос уходит amount={raw_amount:g}"
        )

        try:
                invoice = await probe.create_invoice(
                        0,
                        int(raw_amount) or 1,
                        "Kometa: проверка подключения Platega",
                        exact_kopecks=int(round(raw_amount * 100)),
                        payer_user_id="probe" if provider.send_metadata else None,
                )
        except PaymentError as exc:
                report.issues.append(f"пробный платёж не создался: {exc}")
                return None

        report.lines.append(f"      ссылка на оплату: {invoice.pay_url}")
        report.lines.append(f"      id транзакции: {invoice.external_id}")

        try:
                check = await probe.check_payment(invoice.external_id)
        except PaymentError as exc:
                report.issues.append(f"транзакция создана ({invoice.external_id}), но статус не читается: {exc}")
                return None

        raw = check.raw if isinstance(check.raw, dict) else {}
        details = raw.get("paymentDetails")
        rate = raw.get("usdtRate") or rate_fallback
        amount_usdt = raw.get("amountUsdt")

        report.lines.append(
                f"      статус: {raw.get('status', '?')}, paymentDetails={details!r}, "
                f"amountUsdt={amount_usdt!r}, курс={rate!r}"
        )

        found = verdict_by_rate(raw_amount, amount_usdt, rate) or verdict_by_echo(raw_amount, details)
        if found is None:
                report.warnings.append(
                        f"единицы суммы не подтвердились автоматически по транзакции {invoice.external_id} — "
                        "откройте ссылку выше и посмотрите сумму на платёжной форме"
                )
                return None

        unit, why = found
        report.lines.append(f"      вывод: {why} → PLATEGA_AMOUNT_UNIT={unit}")

        write_probe_state(
                {
                        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "merchant_id_tail": provider.merchant_id[-6:],
                        "method": method,
                        "amount_sent": raw_amount,
                        "amount_usdt": amount_usdt,
                        "rate": rate,
                        "amount_unit": unit,
                        "evidence": why,
                        "transaction_id": invoice.external_id,
                }
        )
        provider.amount_unit, provider.code, provider.title = unit_before, code_before, title_before
        return unit


async def run(
        probe: bool,
        method: int | None = None,
        amount: float | None = None,
        probe_methods: bool = False,
) -> Report:
        """Собрать отчёт о готовности приёма платежей."""
        from app.config import get_settings

        settings = get_settings()
        report = Report()
        merchant_id = (settings.platega_merchant_id or "").strip()
        secret = (settings.platega_secret or "").strip()

        report.lines.append(f"API: {PROD_URL}")
        missing = []
        if not merchant_id:
                missing.append("PLATEGA_MERCHANT_ID")
        if not secret:
                missing.append("PLATEGA_SECRET")
        if missing:
                # Говорим, чего именно не хватает: «не заполнены оба» и «нет только
                # ключа» — разные задачи, и вторая решается одним копированием.
                report.issues.append(
                        f"в .env пусто: {', '.join(missing)} — значения выдают в кабинете "
                        "platega.io → Настройки проекта (ID мерчанта и API ключ)"
                )
                if merchant_id:
                        report.lines.append(
                                f"MerchantId: …{merchant_id[-6:]} — на месте, не хватает только API-ключа"
                        )
                return report

        report.lines.append(f"MerchantId: …{merchant_id[-6:]} (длина {len(merchant_id)})")
        try:
                parsed_id = uuid.UUID(merchant_id)
        except ValueError:
                report.warnings.append(
                        f"MerchantId «{merchant_id}» не похож на UUID — в кабинете рядом лежит ID "
                        "пользователя, он для API не подходит; нужен ID мерчанта из «Настройки проекта»"
                )

        # --- методы оплаты ---
        configured_raw = (settings.platega_methods or "").strip()
        methods = settings.platega_method_list
        raw_numbers = [
                int(x) for x in configured_raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()
        ]
        dropped = sorted({normalize_payment_method(x) for x in raw_numbers if x not in LEGACY_METHOD_ALIASES} - set(PAYMENT_METHOD_TITLES))
        legacy = sorted({x for x in raw_numbers if x in LEGACY_METHOD_ALIASES})
        if legacy:
                report.lines.append(
                        f"  • устаревшие номера методов {legacy} заменены на актуальные "
                        f"({', '.join(f'{k} → {v}' for k, v in LEGACY_METHOD_ALIASES.items())})"
                )
        if dropped:
                report.warnings.append(
                        f"PLATEGA_METHODS={configured_raw!r}: номера {dropped} в API больше нет и отброшены. "
                        f"Актуальные: {list(VALID_METHODS)} (2 — СБП, 11 — карты, 13 — крипта)."
                )
        if not methods:
                report.issues.append(
                        f"в PLATEGA_METHODS не осталось рабочих методов (сейчас {configured_raw!r}); "
                        f"актуальные: {list(VALID_METHODS)}"
                )
        titles = ", ".join(f"{m} — {PAYMENT_METHOD_TITLES[m][1]}" for m in methods)
        report.lines.append(f"Методы к показу: {titles or '—'}")

        # --- вебхук ---
        url = callback_url(settings.public_base_url)
        problem = callback_problem(url)
        if settings.platega_use_webhook:
                report.lines.append(f"Callback URL для кабинета: {url or '—'}")
                if problem:
                        report.issues.append(problem)
                elif url.startswith("http://"):
                        report.warnings.append("callback по http:// — включите TLS, иначе Platega может отказать")
        else:
                # Режим «без вебхука» — это выбор, а не поломка: оплату подтверждает
                # опрос и кнопка «Проверить оплату», публичный адрес тогда не нужен.
                report.lines.append(
                        "Вебхук отключён (PLATEGA_USE_WEBHOOK=false): оплата подтверждается "
                        "опросом каждые 30 секунд и кнопкой «Проверить оплату»"
                )
                if url and problem:
                        report.warnings.append(
                                f"Callback URL всё ещё {url} — в кабинете его лучше не указывать, "
                                "пока адрес не станет публичным"
                        )

        # --- живая проверка ---
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        provider = PlategaProvider(
                merchant_id=merchant_id,
                secret=secret,
                payment_method=methods[0] if methods else 2,
                amount_unit=settings.platega_amount_unit,
                send_metadata=settings.platega_send_metadata,
                return_url=settings.platega_return_url,
                failed_url=settings.platega_failed_url,
                client=client,
        )
        try:
                ok = await check_credentials(provider, report)
                if not ok:
                        return report

                probe_method = method or (methods[0] if methods else 2)
                if probe:
                        unit = await probe_amount_unit(
                                provider,
                                report,
                                probe_method,
                                float(amount if amount is not None else 100),
                                float(settings.usd_rub_rate or 0),
                        )
                        if unit and unit != settings.platega_amount_unit:
                                report.issues.append(
                                        f"PLATEGA_AMOUNT_UNIT={settings.platega_amount_unit}, а API ждёт {unit} — "
                                        f"исправьте .env, иначе клиент заплатит не ту сумму"
                                )
                else:
                        state = read_probe_state()
                        saved = str(state.get("amount_unit") or "")
                        if not saved:
                                report.issues.append(
                                        "единицы суммы не подтверждены живой проверкой — "
                                        "запустите `python -m app.tools.platega_check --probe`"
                                )
                        elif saved != settings.platega_amount_unit:
                                report.issues.append(
                                        f"PLATEGA_AMOUNT_UNIT={settings.platega_amount_unit}, а проверка "
                                        f"{state.get('checked_at', '?')} подтвердила {saved} — приведите .env к {saved}"
                                )
                        else:
                                report.lines.append(
                                        f"Единицы суммы: {saved} — подтверждены {state.get('checked_at', '?')} "
                                        f"({state.get('evidence', '')})"
                                )

                if probe_methods and methods:
                        report.lines.append("Методы оплаты у кассы:")
                        for candidate in methods:
                                enabled, note = await probe_one_method(
                                        provider, candidate, float(amount if amount is not None else 100)
                                )
                                report.lines.append(f"      {'✅' if enabled else '❌'} {note}")
                                if not enabled:
                                        # Кнопка, которая падает всегда, хуже отсутствующей: клиент
                                        # видит «не удалось создать счёт» и уходит.
                                        report.issues.append(
                                                f"{note} — уберите метод из PLATEGA_METHODS "
                                                "или попросите менеджера Platega его подключить"
                                        )
        finally:
                await provider.close()
                await client.aclose()

        report.ok = not report.issues
        return report


def main() -> int:  # pragma: no cover - ручной запуск
        parser = argparse.ArgumentParser(description="Проверка подключения Platega")
        parser.add_argument(
                "--probe",
                action="store_true",
                help="создать тестовую транзакцию и определить единицы суммы (деньги не двигаются)",
        )
        parser.add_argument(
                "--probe-methods",
                action="store_true",
                help="проверить, какие из PLATEGA_METHODS реально подключены, и сколько заплатит клиент",
        )
        parser.add_argument("--method", type=int, default=None, help="номер метода для пробы (по умолчанию первый из настроек)")
        parser.add_argument(
                "--amount",
                type=float,
                default=None,
                help="число, которое уйдёт в поле amount (по умолчанию 100: ссылка будет на 100 ₽ или на 1 ₽)",
        )
        args = parser.parse_args()

        report = asyncio.run(
                run(probe=args.probe, method=args.method, amount=args.amount, probe_methods=args.probe_methods)
        )
        print(report.as_text())

        if report.ok:
                from app.config import get_settings

                print("\nДальше:")
                if get_settings().platega_use_webhook:
                        print("  1. впишите Callback URL в кабинете platega.io → Настройки → Callback URLs")
                else:
                        print("  1. Callback URL в кабинете не указывайте — работаем опросом")
                        print("     (проверить, что адрес пуст: platega.io → Настройки проекта)")
                print("  2. включите продажи: SALES_ENABLED=true")
                print("  3. сделайте первый платёж на минимальный тариф и проверьте, что доступ выдался сам")
        return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - ручной запуск
        raise SystemExit(main())
