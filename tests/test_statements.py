"""Тесты источников входящих переводов: CSV-выписки и письма банка.

Сеть в тестах не используется вообще:

* CSV-файлы пишутся в ``tmp_path``;
* ``imaplib.IMAP4_SSL`` подменяется фейковым сервером через ``monkeypatch``;
* разбор письма — чистая функция :func:`parse_email_payment`.
"""

from __future__ import annotations

import imaplib
import json
import os
from datetime import datetime, timezone
from email.header import Header
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.payments.matching import parse_amount_to_kopecks
from app.payments.statements import StatementError
from app.payments.statements_sources import (
    CsvStatementSource,
    ImapStatementSource,
    build_sources,
    parse_email_payment,
)

#: «Сейчас» для тестов: даты в фикстурах заведомо новее этого значения.
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 3, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------
def write_csv(tmp_path: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    """Положить выписку в tmp_path."""
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


def make_email(
    body: str,
    *,
    subject: str = "Поступление по счёту",
    sender: str = "Т-Банк <no-reply@tbank.ru>",
    date: str = "Mon, 05 Oct 2026 14:30:00 +0300",
    message_id: str | None = "<tbank-2026-1@tbank.ru>",
    content_type: str = "text/plain",
    charset: str = "utf-8",
) -> bytes:
    """Собрать письмо в формате RFC 822 (как его отдаёт IMAP)."""
    headers = [
        f"From: {Header(sender, 'utf-8').encode()}",
        "To: client@example.com",
        f"Subject: {Header(subject, 'utf-8').encode()}",
        f"Date: {date}",
        "MIME-Version: 1.0",
        f"Content-Type: {content_type}; charset={charset}",
        "Content-Transfer-Encoding: 8bit",
    ]
    if message_id:
        headers.append(f"Message-ID: {message_id}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


#: Письмо в стиле Т-Банка: сумма с копейками, плательщик, назначение, баланс.
TBANK_BODY = (
    "Т-Банк\n"
    "Поступление 199.13 ₽\n"
    "От кого: ИВАНОВ ИВАН ИВАНОВИЧ\n"
    "Назначение платежа: Kometa 1234\n"
    "Баланс: 5 000,00 ₽\n"
)

#: Письмо в стиле Сбербанка: «Сумма», «Отправитель», «Сообщение».
SBER_BODY = (
    "Уважаемый клиент!\n"
    "Зачисление на карту\n"
    "Сумма: 1 990,50 ₽\n"
    "Отправитель: ПЕТРОВ ПЁТР ПЕТРОВИЧ\n"
    "Сообщение: Kometa 5678\n"
)

#: Рекламное письмо: ключевых слов о поступлении нет.
AD_BODY = "Скидки недели! Кредит наличными от 100 000 ₽. Оформите карту за 5 минут.\n"


# ---------------------------------------------------------------------------
# CSV: разбор строк
# ---------------------------------------------------------------------------
async def test_csv_semicolon_russian_headers(tmp_path):
    write_csv(
        tmp_path,
        "sber.csv",
        "Дата операции;Сумма;Назначение платежа;Плательщик\n"
        "05.10.2026 14:30;1 990,50 ₽;Kometa 1234;ИВАНОВ И.И.\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert len(payments) == 1
    payment = payments[0]
    assert payment.amount_kopecks == 199050
    assert payment.comment == "Kometa 1234"
    assert payment.counterparty == "ИВАНОВ И.И."
    assert payment.source == "csv"
    assert payment.external_id == "sber.csv:2"
    assert payment.received_at == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)
    assert payment.raw["file"] == "sber.csv"


async def test_csv_comma_delimiter_english_headers(tmp_path):
    write_csv(
        tmp_path,
        "tbank.csv",
        "Date,Amount,Purpose,Payer\n"
        "2026-10-05 12:00:00,199.13,Kometa 77,IVANOV IVAN\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert [(p.amount_kopecks, p.comment, p.counterparty) for p in payments] == [
        (19913, "Kometa 77", "IVANOV IVAN")
    ]
    assert payments[0].received_at == datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


async def test_csv_quoted_amount_with_comma_delimiter(tmp_path):
    write_csv(
        tmp_path,
        "quoted.csv",
        'Дата,Сумма,Назначение\n05.10.2026,"1 990,50 ₽",Kometa 10\n',
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert [p.amount_kopecks for p in payments] == [199050]


async def test_csv_tab_delimiter(tmp_path):
    write_csv(
        tmp_path,
        "tab.csv",
        "Дата\tСумма\tНазначение\n05.10.2026 09:15\t500,00\tKometa 5\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert [(p.amount_kopecks, p.comment) for p in payments] == [(50000, "Kometa 5")]


async def test_csv_without_headers_positional(tmp_path):
    """Заголовков нет: сумма — первая денежная ячейка, дата — первая с датой."""
    write_csv(tmp_path, "raw.txt", "05.10.2026 14:30;1 990,50;ИВАНОВ И.И.;Kometa 99\n")

    payments = await CsvStatementSource(str(tmp_path / "*.txt")).fetch(LONG_AGO)

    assert len(payments) == 1
    assert payments[0].amount_kopecks == 199050
    assert payments[0].comment == "Kometa 99"
    assert payments[0].counterparty == "ИВАНОВ И.И."
    assert payments[0].received_at == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)


async def test_csv_separate_time_column(tmp_path):
    write_csv(
        tmp_path,
        "raw.txt",
        "05.10.2026;14:30;1 990,50 ₽;Kometa 1\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.txt")).fetch(LONG_AGO)

    assert payments[0].received_at == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("05.10.2026 10:15", datetime(2026, 10, 5, 10, 15, tzinfo=timezone.utc)),
        ("05.10.2026", datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)),
        ("2026-10-05 10:15:00", datetime(2026, 10, 5, 10, 15, tzinfo=timezone.utc)),
        # ISO с таймзоной пересчитывается в UTC
        ("2026-10-05T10:15:00+03:00", datetime(2026, 10, 5, 7, 15, tzinfo=timezone.utc)),
    ],
)
async def test_csv_date_formats(tmp_path, raw_date, expected):
    write_csv(
        tmp_path,
        "dates.csv",
        f"Дата;Сумма;Назначение\n{raw_date};199,13;Kometa 1\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert payments[0].received_at == expected


async def test_csv_column_map_override(tmp_path):
    """Нестандартные заголовки описываются через column_map."""
    write_csv(tmp_path, "custom.csv", "Когда;Сколько;Плательщик\n05.10.2026;199,13;Иванов\n")

    source = CsvStatementSource(
        str(tmp_path / "*.csv"),
        column_map={"дата": "Когда", "amount": "Сколько"},
    )
    payments = await source.fetch(LONG_AGO)

    assert payments[0].amount_kopecks == 19913
    assert payments[0].counterparty == "Иванов"


async def test_csv_column_map_unknown_key_is_rejected():
    with pytest.raises(StatementError):
        CsvStatementSource("*.csv", column_map={"сумма2": "X"})


async def test_csv_skips_debits_and_foreign_currency(tmp_path):
    write_csv(
        tmp_path,
        "mixed.csv",
        "Дата;Тип операции;Сумма;Валюта;Назначение\n"
        "05.10.2026 10:00;Списание;500,00;RUB;Оплата связи\n"
        "05.10.2026 11:00;Поступление;-199,13;RUB;Возврат\n"
        "05.10.2026 12:00;Поступление;199,13;USD;Kometa 2\n"
        "05.10.2026 13:00;Поступление;100,00;RUB;Kometa 3\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert [(p.amount_kopecks, p.comment) for p in payments] == [(10000, "Kometa 3")]


# ---------------------------------------------------------------------------
# CSV: состояние и дедупликация
# ---------------------------------------------------------------------------
async def test_csv_credit_and_debit_columns(tmp_path):
    """Колонка «Сумма списания» не должна превращаться в поступление."""
    write_csv(
        tmp_path,
        "both.csv",
        "Дата;Сумма списания;Сумма зачисления;Назначение\n"
        "05.10.2026 10:00;500,00;;Оплата связи\n"
        "05.10.2026 11:00;;199,13;Kometa 1\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert [(p.amount_kopecks, p.comment) for p in payments] == [(19913, "Kometa 1")]


async def test_csv_state_file_skips_processed_rows(tmp_path):
    path = write_csv(
        tmp_path,
        "stmt.csv",
        "Дата;Сумма;Назначение\n05.10.2026 10:00;199,13;Kometa 1\n",
    )
    state = tmp_path / "state.json"
    source = CsvStatementSource(str(tmp_path / "*.csv"), state_file=state)

    first = await source.fetch(LONG_AGO)
    assert [p.amount_kopecks for p in first] == [19913]
    assert state.exists(), "состояние должно сохраняться на диск"
    assert json.loads(state.read_text(encoding="utf-8"))["files"]

    # Повторный опрос не возвращает то же поступление...
    assert await source.fetch(LONG_AGO) == []
    # ...и состояние переживает перезапуск бота (новый объект источника).
    assert await CsvStatementSource(str(tmp_path / "*.csv"), state_file=state).fetch(LONG_AGO) == []

    # Банк дописал строку — читается только она.
    with path.open("a", encoding="utf-8") as handle:
        handle.write("05.10.2026 11:00;500,00;Kometa 2\n")
    third = await source.fetch(LONG_AGO)
    assert [(p.amount_kopecks, p.comment) for p in third] == [(50000, "Kometa 2")]
    assert third[0].external_id == "stmt.csv:3"


async def test_csv_state_file_resets_after_rewrite(tmp_path):
    path = write_csv(
        tmp_path,
        "stmt.csv",
        "Дата;Сумма;Назначение\n"
        "05.10.2026 10:00;199,13;Kometa 1\n"
        "05.10.2026 11:00;300,00;Kometa 2\n",
    )
    state = tmp_path / "state.json"
    source = CsvStatementSource(str(tmp_path / "*.csv"), state_file=state)
    assert len(await source.fetch(LONG_AGO)) == 2

    # Банк перезаписал выписку с нуля (файл стал короче) — читаем заново.
    path.write_text(
        "Дата;Сумма;Назначение\n06.10.2026 09:00;777,00;Kometa 3\n", encoding="utf-8"
    )

    again = await source.fetch(LONG_AGO)
    assert [(p.amount_kopecks, p.comment) for p in again] == [(77700, "Kometa 3")]


async def test_csv_dedups_same_rows_from_two_files(tmp_path):
    """Без state_file дубли внутри одного вызова всё равно отсекаются."""
    text = "Дата;Сумма;Назначение\n05.10.2026 10:00;199,13;Kometa 1\n"
    write_csv(tmp_path, "a.csv", text)
    write_csv(tmp_path, "b.csv", text)

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert len(payments) == 1
    assert payments[0].amount_kopecks == 19913


async def test_csv_keeps_identical_rows_inside_one_file(tmp_path):
    """Две одинаковые строки в одном файле — это два реальных перевода."""
    write_csv(
        tmp_path,
        "stmt.csv",
        "Дата;Сумма;Назначение\n"
        "05.10.2026 10:00;199,13;Kometa 1\n"
        "05.10.2026 10:00;199,13;Kometa 1\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert len(payments) == 2
    assert {p.external_id for p in payments} == {"stmt.csv:2", "stmt.csv:3"}


# ---------------------------------------------------------------------------
# CSV: фильтр since и ошибки
# ---------------------------------------------------------------------------
async def test_csv_since_filter(tmp_path):
    write_csv(
        tmp_path,
        "stmt.csv",
        "Дата;Сумма;Назначение\n"
        "01.10.2026 10:00;100,00;Старое\n"
        "05.10.2026 10:00;199,13;Новое\n",
    )

    payments = await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LATER)

    assert [p.comment for p in payments] == ["Новое"]


async def test_csv_rows_without_date_use_file_mtime(tmp_path):
    path = write_csv(tmp_path, "nodate.csv", "Сумма;Назначение\n199,13;Kometa 1\n")
    stamp = datetime(2020, 5, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    source = CsvStatementSource(str(tmp_path / "*.csv"))

    assert await source.fetch(datetime(2021, 1, 1, tzinfo=timezone.utc)) == []

    payments = await source.fetch(LONG_AGO)
    assert [p.amount_kopecks for p in payments] == [19913]
    assert payments[0].received_at == datetime(2020, 5, 5, 10, 0, tzinfo=timezone.utc)


async def test_csv_broken_file_raises_statement_error(tmp_path):
    (tmp_path / "bad.csv").write_bytes(b"\xff\xfe\x00\x01\x80\x81")

    with pytest.raises(StatementError) as exc_info:
        await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert "bad.csv" in str(exc_info.value)
    assert "кодировке" in str(exc_info.value)


async def test_csv_broken_file_does_not_break_the_good_one(tmp_path):
    write_csv(
        tmp_path,
        "good.csv",
        "Дата;Сумма;Назначение\n05.10.2026 10:00;199,13;Kometa 1\n",
    )
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"\xff\xfe\x00\x01")
    state = tmp_path / "state.json"
    source = CsvStatementSource(str(tmp_path / "*.csv"), state_file=state)

    with pytest.raises(StatementError) as exc_info:
        await source.fetch(LONG_AGO)

    # Хороший файл прочитан, и поступление не потеряно — оно в ошибке.
    assert [p.amount_kopecks for p in exc_info.value.payments] == [19913]
    assert exc_info.value.errors

    # Состояние не зафиксировано: после починки файла платёж придёт снова.
    bad.unlink()
    again = await source.fetch(LONG_AGO)
    assert [p.amount_kopecks for p in again] == [19913]


async def test_csv_cp1251_encoding(tmp_path):
    """Выгрузки российских банков часто в Windows-1251."""
    text = "Дата;Сумма;Назначение;Плательщик\n05.10.2026;199,13;Kometa 1;ИВАНОВ\n"
    (tmp_path / "cp1251.csv").write_bytes(text.encode("cp1251"))

    # По умолчанию utf-8-sig: понятная ошибка с подсказкой, а не тихая потеря строк.
    with pytest.raises(StatementError) as exc_info:
        await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)
    assert "cp1251" in str(exc_info.value)

    payments = await CsvStatementSource(str(tmp_path / "*.csv"), encoding="cp1251").fetch(LONG_AGO)
    assert [(p.amount_kopecks, p.counterparty) for p in payments] == [(19913, "ИВАНОВ")]


async def test_csv_empty_glob_returns_empty_list(tmp_path):
    assert await CsvStatementSource(str(tmp_path / "nope-*.csv")).fetch(LONG_AGO) == []


async def test_csv_corrupt_state_file_raises(tmp_path):
    write_csv(
        tmp_path,
        "stmt.csv",
        "Дата;Сумма;Назначение\n05.10.2026 10:00;199,13;Kometa 1\n",
    )
    state = tmp_path / "state.json"
    state.write_text("{это не json", encoding="utf-8")

    with pytest.raises(StatementError):
        await CsvStatementSource(str(tmp_path / "*.csv"), state_file=state).fetch(LONG_AGO)


async def test_csv_unterminated_quote_raises_statement_error(tmp_path):
    """Битый CSV (незакрытая кавычка) — это StatementError, а не падение бота."""
    write_csv(
        tmp_path,
        "broken.csv",
        'Дата;Сумма;Назначение\n05.10.2026 10:00;"199,13;Kometa 1\n',
    )

    with pytest.raises(StatementError) as exc_info:
        await CsvStatementSource(str(tmp_path / "*.csv")).fetch(LONG_AGO)

    assert "broken.csv" in str(exc_info.value)


async def test_csv_state_file_is_not_read_as_statement(tmp_path):
    """Файл состояния не разбирается как выписка, даже если попал под glob."""
    write_csv(
        tmp_path,
        "stmt.csv",
        "Дата;Сумма;Назначение\n05.10.2026 10:00;199,13;Kometa 1\n",
    )
    state = tmp_path / "state.json"
    source = CsvStatementSource(str(tmp_path / "*"), state_file=state)

    first = await source.fetch(LONG_AGO)
    assert [p.external_id for p in first] == ["stmt.csv:2"]
    assert state.exists()

    # Второй вызов: state.json уже лежит в папке и подходит под glob.
    assert await source.fetch(LONG_AGO) == []


# ---------------------------------------------------------------------------
# Разбор письма банка (чистая функция)
# ---------------------------------------------------------------------------
def test_parse_tbank_email():
    payment = parse_email_payment(make_email(TBANK_BODY))

    assert payment is not None
    assert payment.amount_kopecks == 19913  # баланс 5 000,00 ₽ не перепутан с поступлением
    assert payment.comment == "Kometa 1234"
    assert payment.counterparty == "ИВАНОВ ИВАН ИВАНОВИЧ"
    assert payment.source == "imap"
    assert payment.received_at == datetime(2026, 10, 5, 11, 30, tzinfo=timezone.utc)


def test_parse_sberbank_email():
    payment = parse_email_payment(make_email(SBER_BODY, subject="Зачисление на карту"))

    assert payment is not None
    assert payment.amount_kopecks == 199050
    assert payment.comment == "Kometa 5678"
    assert payment.counterparty == "ПЕТРОВ ПЁТР ПЕТРОВИЧ"


def test_parse_email_message_id_becomes_external_id():
    payment = parse_email_payment(make_email(TBANK_BODY, message_id="<abc-123@tbank.ru>"))

    assert payment is not None
    assert payment.external_id == "abc-123@tbank.ru"


def test_parse_email_without_message_id_uses_body_hash():
    raw = make_email(TBANK_BODY, message_id=None)

    payment = parse_email_payment(raw)

    assert payment is not None
    assert len(payment.external_id) == 64
    assert all(char in "0123456789abcdef" for char in payment.external_id)
    # Одно и то же письмо → один и тот же ключ дедупликации.
    assert parse_email_payment(raw).external_id == payment.external_id


def test_parse_email_without_amount_returns_none():
    raw = make_email("Поступление ожидается завтра. Подробности в приложении.\n")

    assert parse_email_payment(raw) is None


def test_parse_advertisement_returns_none():
    raw = make_email(AD_BODY, subject="Скидки недели")

    assert parse_email_payment(raw) is None


def test_parse_html_email():
    html = (
        "<html><body>"
        "<p>Поступление <b>199,13&nbsp;₽</b></p>"
        "<div>От кого: ООО &laquo;Ромашка&raquo;</div>"
        "<div>Назначение платежа: Kometa 4321</div>"
        "</body></html>"
    )
    raw = make_email(html, content_type="text/html")

    payment = parse_email_payment(raw)

    assert payment is not None
    assert payment.amount_kopecks == 19913
    assert payment.comment == "Kometa 4321"
    assert payment.counterparty == "ООО «Ромашка»"


def test_parse_email_with_nbsp_amount():
    raw = make_email("Зачисление: 1\u00a0990,50\u00a0₽\nСообщение: Kometa 9\n")

    payment = parse_email_payment(raw)

    assert payment is not None
    assert payment.amount_kopecks == 199050


def test_parse_email_amount_without_currency_marker():
    """Запасной вариант: «Сумма 199.13» без символа рубля."""
    raw = make_email("Поступление средств\nСумма 199.13\nНазначение: Kometa 5\n")

    payment = parse_email_payment(raw)

    assert payment is not None
    assert payment.amount_kopecks == 19913
    assert payment.comment == "Kometa 5"


def test_parse_email_custom_patterns_replace_defaults():
    raw = make_email("Kredit gutgeschrieben: 199,13 RUB\n", subject="Kredit")

    assert parse_email_payment(raw) is None  # по умолчанию это не наше письмо

    payment = parse_email_payment(raw, patterns=["kredit"])
    assert payment is not None
    assert payment.amount_kopecks == 19913


def test_parse_email_custom_source_name():
    payment = parse_email_payment(make_email(TBANK_BODY), source_name="tbank-mail")

    assert payment is not None
    assert payment.source == "tbank-mail"
    assert payment.raw["matched_pattern"]


def test_parse_email_transfer_phrasing():
    """«Перевод от … на сумму … RUB» — фраза, частая у банков."""
    raw = make_email(
        "Перевод от ИВАНОВ И.И. на сумму 199,13 RUB\nНазначение: Kometa 42\n",
        subject="Пополнение",
    )

    payment = parse_email_payment(raw)

    assert payment is not None
    assert payment.amount_kopecks == 19913
    assert payment.comment == "Kometa 42"
    assert payment.counterparty == "ИВАНОВ И.И."





def test_parse_email_broken_date_falls_back_to_now():
    raw = make_email(TBANK_BODY, date="не дата")

    payment = parse_email_payment(raw)

    assert payment is not None
    assert abs((payment.received_at - datetime.now(timezone.utc)).total_seconds()) < 60


def test_parse_garbage_email_returns_none():
    assert parse_email_payment(b"") is None
    assert parse_email_payment(b"\xff\xfe\x00\x01\x02") is None


def test_parse_quoted_printable_email():
    """Банки часто шлют письма в quoted-printable — тело должно декодироваться."""
    message = EmailMessage()
    message["From"] = "Сбербанк <no-reply@sberbank.ru>"
    message["To"] = "client@example.com"
    message["Subject"] = "Зачисление на карту"
    message["Date"] = "Mon, 05 Oct 2026 14:30:00 +0300"
    message["Message-ID"] = "<qp-1@sberbank.ru>"
    message.set_content(SBER_BODY, charset="utf-8", cte="quoted-printable")

    payment = parse_email_payment(message.as_bytes())

    assert payment is not None
    assert payment.amount_kopecks == 199050
    assert payment.comment == "Kometa 5678"
    assert payment.counterparty == "ПЕТРОВ ПЁТР ПЕТРОВИЧ"


def test_parse_multipart_prefers_plain_text():
    """multipart/alternative: берём text/plain, даже если HTML идёт вторым."""
    message = EmailMessage()
    message["From"] = "Т-Банк <no-reply@tbank.ru>"
    message["To"] = "client@example.com"
    message["Subject"] = "Поступление"
    message["Date"] = "Mon, 05 Oct 2026 14:30:00 +0300"
    message["Message-ID"] = "<multi-1@tbank.ru>"
    message.set_content(TBANK_BODY, charset="utf-8")
    message.add_alternative(
        "<html><body><p>Поступление 199.13 ₽</p>"
        "<p>Назначение платежа: Из HTML</p></body></html>",
        subtype="html",
        charset="utf-8",
    )

    payment = parse_email_payment(message.as_bytes())

    assert payment is not None
    assert payment.amount_kopecks == 19913
    assert payment.comment == "Kometa 1234"  # из text/plain, а не «Из HTML»


# ---------------------------------------------------------------------------
# IMAP-источник: фейковый сервер вместо сети
# ---------------------------------------------------------------------------
class FakeImapConnection:
    """Мини-заглушка ``imaplib.IMAP4_SSL``.

    Атрибуты класса настраиваются фабрикой ``fake_imap`` для каждого теста.
    """

    messages: dict[bytes, bytes] = {}
    created: list["FakeImapConnection"] = []
    search_criteria: list[tuple] = []
    login_error: str | None = None

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.messages = dict(type(self).messages)
        self.logged_in: tuple[str, str] | None = None
        self.folder: str | None = None
        self.fetched: list[bytes] = []
        self.fetch_specs: list[str] = []
        self.stored: list[tuple] = []
        self.logged_out = False
        type(self).created.append(self)

    def login(self, user: str, password: str):
        if type(self).login_error:
            raise imaplib.IMAP4.error(type(self).login_error)
        self.logged_in = (user, password)
        return "OK", [b"LOGIN completed"]

    def select(self, folder: str, readonly: bool = False):
        self.folder = folder
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        type(self).search_criteria.append((charset, criteria))
        return "OK", [b" ".join(self.messages)]

    def fetch(self, number: bytes, spec: str):
        self.fetched.append(number)
        self.fetch_specs.append(spec)
        if number not in self.messages:
            raise imaplib.IMAP4.error(f"нет письма {number!r}")
        raw = self.messages[number]
        return "OK", [(b'(BODY[] {%d}' % len(raw), raw), b")"]

    def store(self, number: bytes, flags: str, value: str):
        self.stored.append((number, flags, value))
        return "OK", [b""]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture
def fake_imap(monkeypatch):
    """Подменить ``imaplib.IMAP4_SSL`` фейковым сервером."""

    def install(messages: dict[bytes, bytes], *, login_error: str | None = None):
        klass = type("FakeIMAP4SSL", (FakeImapConnection,), {})
        klass.messages = dict(messages)
        klass.created = []
        klass.search_criteria = []
        klass.login_error = login_error
        monkeypatch.setattr(imaplib, "IMAP4_SSL", klass)
        return klass

    return install


async def test_imap_fetch_parses_and_marks_seen(fake_imap):
    klass = fake_imap({b"1": make_email(TBANK_BODY), b"2": make_email(AD_BODY)})
    source = ImapStatementSource("imap.example.ru", "user@example.ru", "app-password")

    payments = await source.fetch(LONG_AGO)

    assert [p.amount_kopecks for p in payments] == [19913]
    assert payments[0].source == "imap"

    connection = klass.created[0]
    assert (connection.host, connection.port) == ("imap.example.ru", 993)
    assert connection.logged_in == ("user@example.ru", "app-password")
    assert connection.folder == "INBOX"
    # Прочитанным помечается только разобранное письмо, реклама — нет.
    assert connection.stored == [(b"1", "+FLAGS", "\\Seen")]
    # RFC822 пометил бы письмо прочитанным в обход mark_seen.
    assert connection.fetched == [b"1", b"2"]
    assert set(connection.fetch_specs) == {"(BODY.PEEK[])"}
    assert connection.logged_out is True
    assert klass.search_criteria == [(None, ("UNSEEN",))]


async def test_imap_mark_seen_disabled(fake_imap):
    klass = fake_imap({b"1": make_email(TBANK_BODY)})
    source = ImapStatementSource(
        "imap.example.ru", "user@example.ru", "app-password", mark_seen=False
    )

    payments = await source.fetch(LONG_AGO)

    assert [p.amount_kopecks for p in payments] == [19913]
    assert klass.created[0].stored == []


async def test_imap_filters_by_since(fake_imap):
    old = make_email(TBANK_BODY, date="Mon, 01 Jan 2024 10:00:00 +0300", message_id="<old@x>")
    fresh = make_email(TBANK_BODY, date="Mon, 05 Oct 2026 14:30:00 +0300", message_id="<new@x>")
    klass = fake_imap({b"1": old, b"2": fresh})
    source = ImapStatementSource("imap.example.ru", "user@example.ru", "app-password")

    payments = await source.fetch(LATER)

    assert [p.external_id for p in payments] == ["new@x"]
    # Старое письмо остаётся непрочитанным: его ещё может понадобиться разобрать.
    assert klass.created[0].stored == [(b"2", "+FLAGS", "\\Seen")]


async def test_imap_takes_last_max_messages(fake_imap):
    klass = fake_imap(
        {
            b"1": make_email(TBANK_BODY, message_id="<m1@x>"),
            b"2": make_email(AD_BODY, message_id="<m2@x>"),
            b"3": make_email(TBANK_BODY, message_id="<m3@x>"),
        }
    )
    source = ImapStatementSource(
        "imap.example.ru", "user@example.ru", "app-password", max_messages=2
    )

    payments = await source.fetch(LONG_AGO)

    assert klass.created[0].fetched == [b"2", b"3"]
    assert [p.external_id for p in payments] == ["m3@x"]


async def test_imap_login_error_raises_statement_error(fake_imap):
    fake_imap({}, login_error="AUTHENTICATIONFAILED")
    source = ImapStatementSource("imap.example.ru", "user@example.ru", "bad-password")

    with pytest.raises(StatementError) as exc_info:
        await source.fetch(LONG_AGO)

    assert "логин" in str(exc_info.value)
    assert "bad-password" not in str(exc_info.value)  # секрет не утекает в текст ошибки


async def test_imap_connection_error_raises_statement_error(monkeypatch):
    class BrokenConnection:
        def __init__(self, host, port, timeout=None):
            raise OSError("connection refused")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", BrokenConnection)
    source = ImapStatementSource("imap.example.ru", "user@example.ru", "app-password")

    with pytest.raises(StatementError) as exc_info:
        await source.fetch(LONG_AGO)

    assert "подключиться" in str(exc_info.value)


def test_imap_repr_hides_password():
    source = ImapStatementSource("imap.example.ru", "user@example.ru", "top-secret")

    assert "top-secret" not in repr(source)


def test_imap_requires_credentials():
    with pytest.raises(StatementError):
        ImapStatementSource("imap.example.ru", "user@example.ru", "")


async def test_imap_custom_folder_and_patterns(fake_imap):
    klass = fake_imap({b"1": make_email("Kredit gutgeschrieben: 199,13 RUB\n", subject="Kredit")})
    source = ImapStatementSource(
        "imap.example.ru",
        "user@example.ru",
        "app-password",
        folder="Bank",
        patterns=["kredit"],
    )

    payments = await source.fetch(LONG_AGO)

    assert [p.amount_kopecks for p in payments] == [19913]
    assert klass.created[0].folder == "Bank"


# ---------------------------------------------------------------------------
# Фабрика источников
# ---------------------------------------------------------------------------
def test_build_sources_empty():
    assert build_sources() == []
    assert build_sources(csv_paths="   ", imap={}) == []


def test_build_sources_csv_only(tmp_path):
    sources = build_sources(csv_paths=str(tmp_path / "*.csv"))

    assert len(sources) == 1
    assert isinstance(sources[0], CsvStatementSource)


def test_build_sources_imap_only():
    sources = build_sources(
        imap={"host": "imap.example.ru", "user": "u@example.ru", "password": "p"}
    )

    assert len(sources) == 1
    assert isinstance(sources[0], ImapStatementSource)
    assert sources[0].port == 993


def test_build_sources_both(tmp_path):
    sources = build_sources(
        csv_paths=str(tmp_path / "*.csv"),
        imap={
            "host": "imap.example.ru",
            "user": "u@example.ru",
            "password": "p",
            "folder": "Bank",
            "mark_seen": False,
            "неизвестный_параметр": 1,
        },
    )

    assert [type(source) for source in sources] == [CsvStatementSource, ImapStatementSource]
    assert sources[1].folder == "Bank"
    assert sources[1].mark_seen is False


def test_build_sources_incomplete_imap_is_loud():
    with pytest.raises(StatementError):
        build_sources(imap={"host": "imap.example.ru", "user": "u@example.ru"})


# ---------------------------------------------------------------------------
# Границы parse_amount_to_kopecks (на него опираются оба источника)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0,01", 1),
        ("1.990,50", 199050),
        ("199", 19900),
        ("199,13 ₽", 19913),
        ("1 990,50 ₽", 199050),
        ("1\u00a0990,50\u00a0₽", 199050),
        ("12.34.56", None),
        ("мусор", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_amount_to_kopecks_boundaries(raw, expected):
    assert parse_amount_to_kopecks(raw) == expected
