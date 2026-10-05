"""Реализации источников выписок: CSV-файлы и почтовые уведомления банка.

Модуль — «глаза» системы автоподтверждения оплаты: он смотрит, сколько денег
реально пришло на счёт, и отдаёт это вызывающей стороне в виде
:class:`~app.payments.statements.IncomingPayment`.

Источники:

* :class:`CsvStatementSource` — банк (или скрипт-выгрузка) складывает выписку
  в папку, мы читаем только новые строки. Номера уже прочитанных строк
  запоминаются в ``state_file``, поэтому повторный опрос не даёт дублей.
* :class:`ImapStatementSource` — разбираем письма банка о поступлениях через
  IMAP. Сам разбор письма вынесен в чистую функцию :func:`parse_email_payment`
  — её можно тестировать без сети.

Общее правило для обоих источников: **если часть данных разобрать не удалось,
``fetch`` поднимает :class:`StatementError`, но в атрибуте ``payments``
исключения лежит всё, что прочитать успели, а состояние обработки при этом не
фиксируется.** Следующий успешный вызов вернёт эти поступления снова, поэтому
вызывающая сторона обязана дедуплицировать их по ``IncomingPayment.key``.
Такой порядок гарантирует доставку «хотя бы один раз»: потерять платёж хуже,
чем увидеть его повторно.

Секретов в коде нет: пароль IMAP приходит из настроек и никогда не попадает
ни в логи, ни в ``repr``.
"""

from __future__ import annotations

import asyncio
import csv
import glob
import hashlib
import imaplib
import io
import json
import logging
import os
import re
import ssl
from collections.abc import Sequence
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message, message_from_bytes
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from app.payments.matching import parse_amount_to_kopecks
from app.payments.statements import IncomingPayment, StatementError, StatementSource

__all__ = [
    "CsvStatementSource",
    "ImapStatementSource",
    "build_sources",
    "parse_email_payment",
    "DEFAULT_PAYMENT_KEYWORDS",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Общие помощники: время, суммы, валюты, склейка ошибок
# ---------------------------------------------------------------------------

_DEFAULT_TIMEZONE = timezone.utc

#: Форматы даты, которые встречаются в выгрузках российских банков.
#: Порядок важен: сначала «длинные» форматы, потом короткие.
_DATE_FORMATS: tuple[str, ...] = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d.%m.%y %H:%M",
    "%d.%m.%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
)

_TIME_FORMATS: tuple[str, ...] = ("%H:%M:%S", "%H:%M")

_TIME_IN_TEXT_RE = re.compile(r"\d{1,2}:\d{2}")

#: Рублёвые обозначения: всё остальное считаем валютой, которую пропускаем.
_RUB_ALIASES = frozenset({"RUB", "RUR", "РУБ", "₽", "Р.", "RUBLE", "ROUBLE"})

#: Валюта, явно написанная в тексте суммы.
_CURRENCY_MARKERS: tuple[tuple[str, str], ...] = (
    ("USDT", "USDT"),
    ("USD", "USD"),
    ("$", "USD"),
    ("EUR", "EUR"),
    ("€", "EUR"),
    ("KZT", "KZT"),
    ("₸", "KZT"),
    ("UAH", "UAH"),
    ("₴", "UAH"),
    ("GBP", "GBP"),
    ("£", "GBP"),
)


def _as_utc(value: datetime) -> datetime:
    """Привести дату к aware-UTC.

    Наивные даты из выписок и писем считаем UTC без пересчёта смещения: банки
    присылают локальное время, а «сдвинуть» его в прошлое опаснее, чем в
    будущее — при фильтре ``since`` лишний платёж отсеет дедупликация, а
    пропущенный платёж означает неоплаченную подписку.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=_DEFAULT_TIMEZONE)
    return value.astimezone(_DEFAULT_TIMEZONE)


def _parse_datetime(text: str | None) -> datetime | None:
    """Устойчиво разобрать дату из строки выписки.

    Поддерживает ISO-8601 (в том числе с таймзоной и ``Z``), ``%d.%m.%Y``,
    ``%d.%m.%Y %H:%M``, ``%Y-%m-%d %H:%M:%S``, а также варианты с ``/`` и
    ``-``. Возвращает aware-UTC или ``None``, если даты в строке нет.
    """
    if text is None:
        return None
    value = str(text).strip().replace("\u00a0", " ")
    if not value or not any(ch.isdigit() for ch in value):
        return None

    parsed: datetime | None = None
    # ISO-8601 покрывает «2026-10-05T10:00:00+03:00» и «2026-10-05 10:00:00».
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    return _as_utc(parsed)


def _parse_time(text: str | None) -> tuple[int, int, int] | None:
    """Разобрать время вида «14:30» / «14:30:05» (нужно для колонки «время»)."""
    if text is None:
        return None
    value = str(text).strip()
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return parsed.hour, parsed.minute, parsed.second
    return None


def _detect_currency(text: str | None) -> str | None:
    """Найти валюту, явно указанную в тексте (``USD``, ``$``, ``₽`` …)."""
    if not text:
        return None
    upper = str(text).upper()
    if "₽" in upper or "РУБ" in upper or re.search(r"\b(RUB|RUR)\b", upper):
        return "RUB"
    for marker, code in _CURRENCY_MARKERS:
        if marker in upper:
            return code
    return None


def _currency_is_acceptable(currency: str, default_currency: str) -> bool:
    """Подходит ли валюта поступления (по умолчанию принимаем только рубли)."""
    code = (currency or "").strip().upper()
    if not code:
        return True
    return code in _RUB_ALIASES or code == (default_currency or "RUB").strip().upper()


def _payment_fingerprint(payment: IncomingPayment) -> tuple:
    """Отпечаток поступления — нужен, чтобы не задваивать строки из разных файлов."""
    return (
        payment.amount_kopecks,
        _as_utc(payment.received_at).isoformat(),
        payment.comment.strip().casefold(),
        payment.counterparty.strip().casefold(),
    )


def _with_payload(
    error: StatementError,
    payments: list[IncomingPayment] | None = None,
    errors: list[str] | None = None,
) -> StatementError:
    """Приклеить к ошибке то, что удалось прочитать (см. докстринг модуля)."""

    error.payments = list(payments or [])  # type: ignore[attr-defined]
    error.errors = list(errors or [])  # type: ignore[attr-defined]
    return error


def _detect_delimiter(text: str) -> str:
    """Определить разделитель CSV через :class:`csv.Sniffer`, фолбэк — «;»."""
    sample = "\n".join(text.splitlines()[:20])[:8192]
    if not sample.strip():
        return ";"
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Одна колонка или нестандартный формат — «;» самый частый у банков РФ.
        return ";"


def _iter_csv_rows(text: str, delimiter: str) -> list[tuple[int, list[str]]]:
    """Прочитать CSV в список ``(номер физической строки, ячейки)``.

    Номер строки берётся у :class:`csv.reader` (``line_num``), поэтому он
    остаётся корректным даже для многострочных значений в кавычках.
    """
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        rows.append((reader.line_num, [cell.strip() for cell in cells]))
    return rows


# ---------------------------------------------------------------------------
# CSV-выписка
# ---------------------------------------------------------------------------

_HEADER_NOISE_RE = re.compile(r"[\s\u00a0_\-.,:;\"'`()\[\]№#]+")

#: Логические колонки и их заголовки. Порядок ключевых слов = приоритет:
#: «поступление» важнее generic-«суммы», иначе можно случайно взять колонку
#: списаний. Поиск идёт по подстроке в нормализованном заголовке.
_COLUMN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "amount": ("поступление", "зачисление", "credit", "сумма", "amount"),
    "date": ("дата операции", "дата", "date"),
    "comment": (
        "назначение платежа",
        "назначение",
        "комментарий",
        "описание",
        "purpose",
        "comment",
    ),
    "counterparty": ("плательщик", "отправитель", "от кого", "payer", "counterparty"),
    "currency": ("валюта", "currency"),
    "direction": ("направление", "тип операции", "вид операции", "тип", "operation", "direction"),
}

#: Русские и английские имена логических колонок для ``column_map``.
_COLUMN_ALIASES: dict[str, str] = {
    "amount": "amount",
    "сумма": "amount",
    "date": "date",
    "дата": "date",
    "comment": "comment",
    "комментарий": "comment",
    "назначение": "comment",
    "counterparty": "counterparty",
    "контрагент": "counterparty",
    "плательщик": "counterparty",
    "currency": "currency",
    "валюта": "currency",
    "direction": "direction",
    "направление": "direction",
}

_ALL_HEADER_KEYWORDS: tuple[str, ...] = tuple(
    keyword for keywords in _COLUMN_KEYWORDS.values() for keyword in keywords
)

#: Заголовки колонок, которые описывают списания, а не поступления.
_NEGATIVE_HEADER_RE = re.compile(r"(списани|дебет|debit|расход|withdraw|исходящ)", re.IGNORECASE)

#: Значения колонки «тип операции», означающие списание.
_OUTGOING_RE = re.compile(r"(списани|дебет|debit|расход|исходящ|withdraw|outgoing)", re.IGNORECASE)

_MONEY_LIKE_RE = re.compile(r"\d[.,]\d{2}\b")
_BARE_NUMBER_RE = re.compile(r"^-?\d{1,15}$")
_HAS_LETTERS_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _normalize_header(text: str | None) -> str:
    """Привести заголовок колонки к сравнимому виду."""
    return _HEADER_NOISE_RE.sub("", str(text or "")).casefold()


def _find_column(
    normalized_headers: Sequence[str],
    keywords: Sequence[str],
    *,
    skip: frozenset[int] = frozenset(),
) -> int | None:
    """Найти индекс колонки по ключевым словам (подстрокой)."""
    for keyword in keywords:
        needle = _normalize_header(keyword)
        if not needle:
            continue
        for index, header in enumerate(normalized_headers):
            if index in skip or not header:
                continue
            if needle in header:
                return index
    return None


def _money_like(text: str) -> bool:
    """Похоже ли значение на денежную сумму (есть копейки или символ валюты)."""
    return bool(_MONEY_LIKE_RE.search(text)) or _detect_currency(text) is not None


def _looks_like_header(cells: Sequence[str]) -> bool:
    """Отличить строку заголовков от строки данных.

    Данные почти всегда содержат дату или сумму, поэтому наличие разобранной
    даты/времени сразу снимает подозрения; шапка опознаётся по известным
    ключевым словам или по полному отсутствию чисел.
    """
    for cell in cells:
        if _parse_datetime(cell) is not None or _parse_time(cell) is not None:
            return False
    normalized = [_normalize_header(cell) for cell in cells]
    if any(_find_column([cell], _ALL_HEADER_KEYWORDS) is not None for cell in normalized):
        return True
    return not any(parse_amount_to_kopecks(cell) is not None for cell in cells)


def _amount_from_cell(text: str | None) -> int | None:
    """Сумма из ячейки выписки; пустая ячейка — это не ошибка, а «нет данных»."""
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    return parse_amount_to_kopecks(value)


class _CsvState:
    """Состояние обработки CSV: сколько строк каждого файла уже прочитано.

    Формат файла состояния::

        {"version": 1, "files": {"<путь>": {"rows": 12, "size": 3456}}}
    """

    VERSION = 1

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._files: dict[str, dict[str, int]] = {}
        self._dirty = False
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StatementError(
                f"не удалось прочитать файл состояния выписок {self.path}: {exc}"
            ) from exc
        if not raw.strip():
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StatementError(
                f"файл состояния выписок {self.path} повреждён (не JSON): {exc}. "
                "Удалите его, чтобы обработать выписки заново."
            ) from exc
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, dict):
            raise StatementError(
                f"файл состояния выписок {self.path} имеет неожиданный формат: "
                "ожидался объект с полем 'files'."
            )
        for key, value in files.items():
            if not isinstance(value, dict):
                continue
            try:
                rows = int(value.get("rows") or 0)
                size = int(value.get("size") or 0)
            except (TypeError, ValueError) as exc:
                raise StatementError(
                    f"файл состояния выписок {self.path} повреждён: у записи {key!r} "
                    f"нечисловые rows/size ({exc})."
                ) from exc
            self._files[str(key)] = {"rows": rows, "size": size}

    def get(self, key: str) -> dict[str, int]:
        return dict(self._files.get(key, {"rows": 0, "size": 0}))

    def stage(self, key: str, rows: int, size: int) -> None:
        """Запомнить прогресс в памяти; на диск попадёт только при ``save()``."""
        self._files[key] = {"rows": int(rows), "size": int(size)}
        self._dirty = True

    def save(self) -> None:
        """Записать состояние атомарно (через временный файл)."""
        if self.path is None or not self._dirty:
            return
        payload = {"version": self.VERSION, "files": self._files}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise StatementError(
                f"не удалось сохранить состояние выписок в {self.path}: {exc}"
            ) from exc


class CsvStatementSource(StatementSource):
    """Источник поступлений из CSV-выписок в папке.

    :param paths: glob-шаблон (``data/statements/*.csv``) или папка с ``*.csv``.
    :param encoding: кодировка файлов; для выгрузок Windows-1251 передайте
        ``encoding="cp1251"``. Ошибка декодирования — это :class:`StatementError`,
        а не молчаливая потеря строк.
    :param delimiter: разделитель; ``None`` — автоопределение через
        :class:`csv.Sniffer` с фолбэком на ``;``.
    :param column_map: переопределение колонок, например
        ``{"amount": "Сумма зачисления", "date": "Дата операции"}``.
        Ключи — логические имена (``amount``/``date``/``comment``/
        ``counterparty``/``currency``/``direction``) или их русские синонимы.
    :param state_file: JSON-файл с прогрессом. ``None`` — не хранить прогресс
        между вызовами (внутри одного вызова дубли всё равно отсекаются).
    :param default_currency: валюта, которую считаем «своей»; строки с явно
        другой валютой (USD/EUR/KZT…) пропускаются.
    """

    name = "csv"

    def __init__(
        self,
        paths: str,
        *,
        encoding: str = "utf-8-sig",
        delimiter: str | None = None,
        column_map: dict[str, str] | None = None,
        state_file: Path | None = None,
        default_currency: str = "RUB",
    ) -> None:
        if not paths or not str(paths).strip():
            raise StatementError("не задан шаблон файлов выписки (paths)")
        if delimiter is not None and len(delimiter) != 1:
            raise StatementError("разделитель CSV должен быть одним символом")
        self.paths = str(paths).strip()
        self.encoding = encoding
        self.delimiter = delimiter
        self.state_file = Path(state_file) if state_file is not None else None
        self.default_currency = (default_currency or "RUB").strip().upper()
        self.column_map: dict[str, str] = {}
        for logical, header in (column_map or {}).items():
            key = _COLUMN_ALIASES.get(_normalize_header(logical))
            if key is None:
                raise StatementError(
                    f"неизвестная логическая колонка {logical!r} в column_map; "
                    f"допустимо: {', '.join(sorted(_COLUMN_KEYWORDS))}"
                )
            self.column_map[key] = str(header)

    # -- публичный интерфейс -------------------------------------------------

    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        """Вернуть новые поступления из файлов, изменённых после ``since``.

        Чтение файлов уходит в отдельный поток, чтобы не блокировать цикл бота.

        :raises StatementError: если хотя бы один файл не удалось прочитать;
            успешно разобранные поступления лежат в ``error.payments``.
        """
        return await asyncio.to_thread(self._fetch_sync, since)

    # -- внутренняя кухня ----------------------------------------------------

    def _resolve_files(self) -> list[Path]:
        pattern = self.paths
        candidate = Path(pattern)
        if candidate.is_dir():
            pattern = str(candidate / "*.csv")
        found = {Path(item) for item in glob.glob(pattern, recursive=True)}
        return sorted((path for path in found if path.is_file()), key=str)

    def _file_key(self, path: Path) -> str:
        """Ключ файла в состоянии: абсолютный путь (стабилен между запусками)."""
        try:
            return str(path.resolve())
        except OSError:  # pragma: no cover - защита от экзотических ФС
            return str(path)

    def _fetch_sync(self, since: datetime) -> list[IncomingPayment]:
        since_utc = _as_utc(since)
        state = _CsvState(self.state_file)
        payments: list[IncomingPayment] = []
        errors: list[str] = []
        # Отпечаток -> файл, из которого поступление уже пришло в этом вызове.
        # Так один и тот же файл под двумя именами не даёт двух оплат.
        seen: dict[tuple, str] = {}

        for path in self._resolve_files():
            file_key = self._file_key(path)
            try:
                found, processed, size = self._read_file(path, since_utc, state.get(file_key))
            except StatementError as exc:
                logger.warning("Выписка %s не прочитана: %s", path, exc)
                errors.append(str(exc))
                continue
            for payment in found:
                fingerprint = _payment_fingerprint(payment)
                source_file = seen.get(fingerprint)
                if source_file is not None and source_file != file_key:
                    logger.info(
                        "Пропускаю дубль поступления %.2f ₽ из %s (уже было в %s)",
                        payment.amount_kopecks / 100,
                        path.name,
                        Path(source_file).name,
                    )
                    continue
                seen[fingerprint] = file_key
                payments.append(payment)
            state.stage(file_key, processed, size)

        if errors:
            # Состояние не сохраняем: лучше вернуть поступления повторно, чем
            # потерять их. Вызывающая сторона дедуплицирует по `key`.
            raise _with_payload(
                StatementError("не удалось прочитать часть выписок: " + "; ".join(errors)),
                payments,
                errors,
            )

        state.save()
        payments.sort(key=lambda item: _as_utc(item.received_at))
        return payments

    def _read_file(
        self,
        path: Path,
        since_utc: datetime,
        known: dict[str, int],
    ) -> tuple[list[IncomingPayment], int, int]:
        """Прочитать один файл.

        :returns: ``(поступления, сколько строк данных теперь считается
            обработанными, размер файла в байтах)``.
        """
        try:
            stat = path.stat()
        except OSError as exc:
            raise StatementError(f"файл выписки {path.name} недоступен: {exc}") from exc
        size = stat.st_size
        try:
            text = path.read_text(encoding=self.encoding)
        except UnicodeDecodeError as exc:
            raise StatementError(
                f"файл выписки {path.name} не читается в кодировке {self.encoding} "
                f"(для выгрузок Windows-1251 укажите encoding='cp1251'): {exc}"
            ) from exc
        except OSError as exc:
            raise StatementError(f"файл выписки {path.name} недоступен: {exc}") from exc

        if not text.strip():
            return [], 0, size

        delimiter = self.delimiter or _detect_delimiter(text)
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=_DEFAULT_TIMEZONE)
        skip_rows = int(known.get("rows") or 0)
        if int(known.get("size") or 0) > size:
            # Файл перезаписан с нуля (ротация выгрузки) — читаем заново.
            skip_rows = 0

        rows = _iter_csv_rows(text, delimiter)
        if not rows:
            return [], 0, size

        header, data_rows = self._split_header(rows)
        mapping = self._map_columns(header) if header is not None else {}

        payments: list[IncomingPayment] = []
        processed = 0
        data_index = 0
        last_row_unreadable = False
        for line_no, cells in data_rows:
            data_index += 1
            if data_index <= skip_rows:
                processed = data_index
                continue
            payment, unreadable = self._build_payment(
                path, line_no, cells, mapping, since_utc, mtime
            )
            last_row_unreadable = unreadable
            processed = data_index
            if payment is not None:
                payments.append(payment)

        # Банк может писать файл построчно: последняя строка без перевода
        # строки в конце файла может быть недописанной. Не считаем её
        # обработанной, чтобы не потерять платёж.
        if last_row_unreadable and not text.endswith(("\n", "\r")):
            processed = max(skip_rows, processed - 1)

        return payments, processed, size

    def _split_header(
        self, rows: Sequence[tuple[int, list[str]]]
    ) -> tuple[list[str] | None, list[tuple[int, list[str]]]]:
        first_cells = rows[0][1]
        if _looks_like_header(first_cells):
            return first_cells, list(rows[1:])
        return None, list(rows)

    def _map_columns(self, header: Sequence[str]) -> dict[str, int]:
        normalized = [_normalize_header(cell) for cell in header]
        negative = frozenset(
            index for index, cell in enumerate(normalized) if _NEGATIVE_HEADER_RE.search(cell)
        )
        mapping: dict[str, int] = {}
        for key, keywords in _COLUMN_KEYWORDS.items():
            skip = negative if key == "amount" else frozenset()
            index = _find_column(normalized, keywords, skip=skip)
            if index is not None:
                mapping[key] = index

        for key, wanted in self.column_map.items():
            needle = _normalize_header(wanted)
            index = next(
                (i for i, cell in enumerate(normalized) if needle and needle in cell),
                None,
            )
            if index is None:
                raise StatementError(
                    f"в выписке нет колонки «{wanted}» (column_map[{key}]); "
                    f"заголовки файла: {', '.join(header)}"
                )
            mapping[key] = index
        return mapping

    def _build_payment(
        self,
        path: Path,
        line_no: int,
        cells: Sequence[str],
        mapping: dict[str, int],
        since_utc: datetime,
        mtime: datetime,
    ) -> tuple[IncomingPayment | None, bool]:
        """Собрать поступление из строки.

        :returns: ``(поступление или None, строка выглядит недописанной)``.
        """
        used: set[int] = set()

        def cell(key: str) -> str | None:
            index = mapping.get(key)
            if index is None or index >= len(cells):
                return None
            used.add(index)
            return cells[index]

        # 1. Направление операции: списания нам не нужны.
        direction = cell("direction")
        if direction and _OUTGOING_RE.search(direction):
            return None, False

        # 2. Валюта: по умолчанию принимаем только рубли.
        currency = (cell("currency") or "").strip()
        if not _currency_is_acceptable(currency, self.default_currency):
            logger.debug("Строка %s:%s пропущена: валюта %s", path.name, line_no, currency)
            return None, False

        # 3. Дата: колонка, затем позиционный поиск, затем mtime файла.
        received_at = _parse_datetime(cell("date"))
        amount_index = mapping.get("amount")
        if amount_index is not None:
            used.add(amount_index)
        amount_raw = cells[amount_index] if amount_index is not None and amount_index < len(cells) else None

        amount_kopecks = _amount_from_cell(amount_raw)
        unreadable = amount_kopecks is None and bool((amount_raw or "").strip())

        positional_amount, positional_date, positional_time = self._scan_row(cells, used)
        if amount_kopecks is None and positional_amount is not None:
            amount_kopecks = _amount_from_cell(cells[positional_amount])
            unreadable = False
            used.add(positional_amount)

        if received_at is None and positional_date is not None:
            received_at = _parse_datetime(cells[positional_date])
            used.add(positional_date)
        if positional_time is not None:
            used.add(positional_time)
            if received_at is not None and not _TIME_IN_TEXT_RE.search(
                cells[positional_date] if positional_date is not None else ""
            ):
                clock = _parse_time(cells[positional_time])
                if clock is not None:
                    received_at = received_at.replace(
                        hour=clock[0], minute=clock[1], second=clock[2]
                    )
        if received_at is None:
            received_at = mtime

        # 4. Сумма: без неё строку обработать нечем.
        if amount_kopecks is None or amount_kopecks <= 0:
            return None, unreadable
        if amount_raw is not None:
            text_currency = _detect_currency(amount_raw)
            if text_currency and not _currency_is_acceptable(text_currency, self.default_currency):
                return None, False

        # 5. Комментарий и плательщик: из колонок, иначе — эвристика по тексту.
        comment = (cell("comment") or "").strip()
        counterparty = (cell("counterparty") or "").strip()
        if not comment or not counterparty:
            text_cells = [
                cells[index]
                for index in range(len(cells))
                if index not in used and _HAS_LETTERS_RE.search(cells[index])
            ]
            if not comment and text_cells:
                comment = text_cells[-1]
            if not counterparty and len(text_cells) > 1:
                counterparty = text_cells[0]

        if _as_utc(received_at) < since_utc:
            return None, False

        return (
            IncomingPayment(
                amount_kopecks=amount_kopecks,
                received_at=received_at,
                comment=comment,
                counterparty=counterparty,
                source=self.name,
                external_id=f"{path.name}:{line_no}",
                raw={
                    "file": path.name,
                    "line": line_no,
                    "currency": currency or self.default_currency,
                    "row": list(cells),
                },
            ),
            False,
        )

    def _scan_row(
        self, cells: Sequence[str], used: set[int]
    ) -> tuple[int | None, int | None, int | None]:
        """Позиционный разбор строки без заголовков.

        Сумма — первая «денежная» ячейка (с копейками или символом валюты), а
        если таких нет — первое одинокое число. Дата — первая ячейка с датой,
        время — первая ячейка вида «14:30».
        """
        amount: int | None = None
        weak_amount: int | None = None
        date: int | None = None
        clock: int | None = None
        for index, raw in enumerate(cells):
            if index in used:
                continue
            value = (raw or "").strip()
            if not value:
                continue
            if date is None and _parse_datetime(value) is not None:
                date = index
                continue
            if clock is None and _parse_time(value) is not None:
                clock = index
                continue
            if amount is None and _money_like(value) and _amount_from_cell(value) is not None:
                amount = index
                continue
            if weak_amount is None and _BARE_NUMBER_RE.match(value) and _amount_from_cell(value):
                weak_amount = index
        return (amount if amount is not None else weak_amount), date, clock


# ---------------------------------------------------------------------------
# Почтовые уведомления банка (IMAP)
# ---------------------------------------------------------------------------

#: Ключевые слова, по которым узнаём уведомление о поступлении.
DEFAULT_PAYMENT_KEYWORDS: tuple[str, ...] = (
    "поступление",
    "зачисление",
    "перевод от",
    "пополнение",
    "credited",
    "incoming",
)

#: Основная регулярка суммы: «199.13 ₽», «1 990,50 руб», «199,13 RUB».
_AMOUNT_PRIMARY_RE = re.compile(
    r"(\d[\d\s\u00a0]*[.,]\d{2})\s*(?:₽|руб|RUB|RUR|р\.)",
    re.IGNORECASE,
)

#: Запасной вариант: «Сумма … 199.13» без символа валюты.
_AMOUNT_FALLBACK_RE = re.compile(
    r"(?:сумма|amount|зачислено|к зачислению)[^\d\n]{0,25}(\d[\d\s\u00a0]*(?:[.,]\d{2})?)",
    re.IGNORECASE,
)

#: Контекст, который подтверждает, что найденная сумма — это поступление.
_AMOUNT_CONTEXT_RE = re.compile(
    r"(поступление|зачислен|перевод|пополнени|credited|incoming|сумма|amount)",
    re.IGNORECASE,
)

#: Контекст, который говорит, что сумма — не поступление (баланс, лимит…).
_AMOUNT_IGNORE_RE = re.compile(
    r"(баланс|остаток|доступн|balance|лимит|комисси|задолженност)",
    re.IGNORECASE,
)

_COMMENT_RE = re.compile(
    r"(?:назначение\s+платежа|назначение|комментарий|сообщение|описание|purpose|comment)"
    r"\s*[:\-–—]?\s*(.+)",
    re.IGNORECASE,
)

_COUNTERPARTY_RE = re.compile(
    r"(?:от\s+кого|плательщик|отправитель|payer|counterparty|sender"
    r"|(?:перевод|поступление|зачисление)\s+от)"
    r"\s*[:\-–—]?\s*([^\d\s][^\n]{0,120})",
    re.IGNORECASE,
)

#: Обрезка значения поля: два пробела подряд или начало следующего поля.
_FIELD_STOP_RE = re.compile(
    r"\s{2,}|(?=\s(?:сумма|назначение|сообщение|комментарий|описание|"
    r"от\s+кого|плательщик|отправитель|дата|balance|amount|итого)\b)",
    re.IGNORECASE,
)


class _HtmlTextExtractor(HTMLParser):
    """Превратить HTML письма в текст, сохранив переводы строк.

    Переводы строк принципиальны: без них «Назначение платежа: Kometa 1234»
    склеилось бы со следующим полем письма.
    """

    _BLOCK_TAGS = frozenset(
        {
            "br",
            "p",
            "div",
            "tr",
            "td",
            "th",
            "li",
            "ul",
            "ol",
            "table",
            "section",
            "article",
            "header",
            "footer",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }
    )
    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts).replace("\r", "\n")
        lines = [line.strip() for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


def _html_to_text(html: str) -> str:
    """Убрать теги HTML, оставив осмысленный текст."""
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - защита от экзотической разметки
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def _decode_header_value(value: object) -> str:
    """Декодировать заголовок письма (RFC 2047), не падая на битой кодировке."""
    if value is None:
        return ""
    try:
        chunks = decode_header(str(value))
    except Exception:  # pragma: no cover - экзотические заголовки
        return str(value).strip()
    parts: list[str] = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _extract_body_text(message: Message) -> str:
    """Достать тело письма: сначала ``text/plain``, иначе ``text/html``."""
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():  # вложения (выписки PDF и т.п.) не разбираем
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            text = str(part.get_payload() or "")
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
        (plain if content_type == "text/plain" else html).append(text)

    if plain:
        return "\n".join(plain)
    if html:
        return _html_to_text("\n".join(html))
    return ""


def _matching_pattern(text: str, patterns: Sequence[str] | None) -> str | None:
    """Найти шаблон, по которому письмо опознано как уведомление о поступлении."""
    for pattern in patterns or DEFAULT_PAYMENT_KEYWORDS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        except re.error:
            # Шаблон — не регулярка; считаем его обычной подстрокой.
            if pattern.casefold() in text.casefold():
                return pattern
    return None


def _clean_field(value: str | None, *, limit: int = 200) -> str:
    """Причесать значение поля письма (обрезать хвост и кавычки)."""
    if not value:
        return ""
    text = str(value).strip()
    parts = _FIELD_STOP_RE.split(text, maxsplit=1)
    if parts:
        text = parts[0]
    text = text.strip().strip("\"'«»").strip().strip(".,;:-–—").strip()
    return text[:limit].strip()


def _pick_amount(text: str) -> int | None:
    """Найти сумму поступления среди всех сумм в письме.

    Предпочитаем сумму, рядом с которой есть «поступление/зачисление/сумма»,
    и игнорируем баланс и остаток: банки любят приписать их в то же письмо.
    """
    for match in _AMOUNT_PRIMARY_RE.finditer(text):
        context = text[max(0, match.start() - 40) : match.start()]
        if _AMOUNT_IGNORE_RE.search(context):
            continue
        if _AMOUNT_CONTEXT_RE.search(context):
            amount = parse_amount_to_kopecks(match.group(1))
            if amount:
                return amount

    primary = _AMOUNT_PRIMARY_RE.search(text)
    if primary is not None:
        amount = parse_amount_to_kopecks(primary.group(1))
        if amount:
            return amount

    fallback = _AMOUNT_FALLBACK_RE.search(text)
    if fallback is not None:
        amount = parse_amount_to_kopecks(fallback.group(1))
        if amount:
            return amount
    return None


def parse_email_payment(
    raw_email: bytes,
    patterns: list[str] | None = None,
    source_name: str = "imap",
) -> IncomingPayment | None:
    """Разобрать письмо банка и вернуть поступление (или ``None``).

    Чистая функция без сети и состояния — её и тестируем отдельно.

    :param raw_email: письмо целиком (``bytes``) в формате RFC 822.
    :param patterns: свои шаблоны «это уведомление о поступлении». Список
        **заменяет** набор по умолчанию (:data:`DEFAULT_PAYMENT_KEYWORDS`).
        Каждый элемент — регулярка, а если она не компилируется — подстрока.
    :param source_name: значение поля ``source`` у результата.
    :returns: :class:`IncomingPayment` либо ``None``, если письмо не про
        поступление или сумму найти не удалось. Исключений не поднимает.
    """
    try:
        raw_bytes = raw_email.encode("utf-8") if isinstance(raw_email, str) else bytes(raw_email)
    except (TypeError, ValueError):
        return None

    try:
        message = message_from_bytes(raw_bytes)
    except Exception:  # pragma: no cover - битое письмо
        return None

    subject = _decode_header_value(message.get("Subject"))
    sender = _decode_header_value(message.get("From"))
    body = _extract_body_text(message)
    text = f"{subject}\n{body}" if subject else body

    matched = _matching_pattern(text, patterns)
    if matched is None:
        return None

    amount_kopecks = _pick_amount(text)
    if not amount_kopecks or amount_kopecks <= 0:
        return None

    received_at = _parse_email_date(message)
    message_id = str(message.get("Message-ID") or "").strip().strip("<>").strip()
    if not message_id:
        digest_source = body if body else subject
        message_id = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()

    comment_match = _COMMENT_RE.search(text)
    counterparty_match = _COUNTERPARTY_RE.search(text)

    return IncomingPayment(
        amount_kopecks=amount_kopecks,
        received_at=received_at,
        comment=_clean_field(comment_match.group(1) if comment_match else ""),
        counterparty=_clean_field(counterparty_match.group(1) if counterparty_match else ""),
        source=source_name,
        external_id=message_id,
        raw={
            "subject": subject,
            "from": sender,
            "message_id": message_id,
            "matched_pattern": matched,
            "date": received_at.isoformat(),
        },
    )


def _parse_email_date(message: Message) -> datetime:
    """Дата письма из заголовка ``Date``; фолбэк — текущее время UTC."""
    raw_date = message.get("Date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(str(raw_date))
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            return _as_utc(parsed)
    return datetime.now(tz=timezone.utc)


class ImapStatementSource(StatementSource):
    """Источник поступлений из почтовых уведомлений банка.

    :param host: IMAP-сервер, например ``imap.yandex.ru``.
    :param user: логин (обычно полный адрес почты).
    :param password: пароль приложения; в логи и ``repr`` не попадает.
    :param patterns: свои шаблоны уведомлений о поступлении (см.
        :func:`parse_email_payment`).
    :param max_messages: сколько последних писем разбирать за один опрос.
    :param mark_seen: помечать прочитанными те письма, из которых получилось
        поступление. Неразобранные письма остаются непрочитанными — их должен
        увидеть человек, иначе платёж потеряется молча.

    Письма читаются через ``BODY.PEEK[]``: обычный ``RFC822`` помечает письмо
    прочитанным на стороне сервера, что сломало бы ``mark_seen=False``.
    """

    name = "imap"

    #: Таймаут соединения с почтовым сервером, секунды.
    TIMEOUT = 30.0

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        port: int = 993,
        folder: str = "INBOX",
        use_ssl: bool = True,
        patterns: list[str] | None = None,
        max_messages: int = 50,
        mark_seen: bool = True,
    ) -> None:
        if not host or not str(host).strip():
            raise StatementError("для IMAP не задан host")
        if not user or not str(user).strip():
            raise StatementError("для IMAP не задан user")
        if not password:
            raise StatementError("для IMAP не задан password")
        self.host = str(host).strip()
        self.user = str(user).strip()
        self._password = str(password)
        self.port = int(port)
        self.folder = str(folder or "INBOX")
        self.use_ssl = bool(use_ssl)
        self.patterns = list(patterns) if patterns else None
        self.max_messages = int(max_messages)
        self.mark_seen = bool(mark_seen)

    def __repr__(self) -> str:  # pragma: no cover - косметика
        return (
            f"ImapStatementSource(host={self.host!r}, user={self.user!r}, "
            f"folder={self.folder!r}, use_ssl={self.use_ssl!r}, "
            f"mark_seen={self.mark_seen!r})"
        )

    # -- публичный интерфейс -------------------------------------------------

    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        """Разобрать непрочитанные письма и вернуть поступления после ``since``.

        ``imaplib`` блокирующий, поэтому вся работа уходит в отдельный поток.

        :raises StatementError: если не удалось подключиться, авторизоваться
            или прочитать часть писем; успешно разобранные поступления лежат
            в ``error.payments``.
        """
        return await asyncio.to_thread(self._fetch_sync, since)

    # -- внутренняя кухня ----------------------------------------------------

    def _connect(self) -> imaplib.IMAP4:
        try:
            if self.use_ssl:
                return imaplib.IMAP4_SSL(self.host, self.port, timeout=self.TIMEOUT)
            return imaplib.IMAP4(self.host, self.port, timeout=self.TIMEOUT)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise StatementError(
                f"не удалось подключиться к почтовому серверу {self.host}:{self.port}: {exc}"
            ) from exc

    def _login(self, connection: imaplib.IMAP4) -> None:
        try:
            status, data = connection.login(self.user, self._password)
        except imaplib.IMAP4.error as exc:
            raise StatementError(
                f"почтовый сервер {self.host} отклонил логин {self.user}: {exc}"
            ) from exc
        if str(status).upper() != "OK":
            raise StatementError(f"почтовый сервер {self.host} отклонил логин {self.user}: {data}")

    def _select(self, connection: imaplib.IMAP4) -> None:
        try:
            status, data = connection.select(self.folder)
        except imaplib.IMAP4.error as exc:
            raise StatementError(f"не удалось открыть папку {self.folder}: {exc}") from exc
        if str(status).upper() != "OK":
            raise StatementError(f"не удалось открыть папку {self.folder}: {data}")

    def _search_unseen(self, connection: imaplib.IMAP4) -> list[bytes]:
        try:
            status, data = connection.search(None, "UNSEEN")
        except imaplib.IMAP4.error as exc:
            raise StatementError(f"не удалось получить список писем: {exc}") from exc
        if str(status).upper() != "OK" or not data:
            return []
        raw = data[0] or b""
        return str(raw, "ascii", errors="ignore").split()

    def _fetch_raw(self, connection: imaplib.IMAP4, number: bytes) -> bytes:
        try:
            status, data = connection.fetch(number, "(BODY.PEEK[])")
        except imaplib.IMAP4.error as exc:
            raise StatementError(f"не удалось прочитать письмо {number!r}: {exc}") from exc
        if str(status).upper() != "OK":
            raise StatementError(f"не удалось прочитать письмо {number!r}: {data}")
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        raise StatementError(f"письмо {number!r} пришло без тела")

    def _mark_seen(self, connection: imaplib.IMAP4, number: bytes) -> None:
        try:
            connection.store(number, "+FLAGS", "\\Seen")
        except imaplib.IMAP4.error as exc:  # pragma: no cover - зависит от сервера
            logger.warning("Не удалось пометить письмо %r прочитанным: %s", number, exc)

    def _logout(self, connection: imaplib.IMAP4) -> None:
        try:
            connection.logout()
        except Exception as exc:  # pragma: no cover - сервер мог уже закрыть сессию
            logger.debug("IMAP logout завершился с ошибкой: %s", exc)

    def _fetch_sync(self, since: datetime) -> list[IncomingPayment]:
        since_utc = _as_utc(since)
        connection = self._connect()
        payments: list[IncomingPayment] = []
        errors: list[str] = []
        try:
            self._login(connection)
            self._select(connection)
            numbers = self._search_unseen(connection)
            if self.max_messages > 0:
                numbers = numbers[-self.max_messages :]

            for number in numbers:
                try:
                    raw = self._fetch_raw(connection, number)
                except StatementError as exc:
                    logger.warning("Письмо %r не прочитано: %s", number, exc)
                    errors.append(str(exc))
                    continue
                payment = parse_email_payment(raw, self.patterns, self.name)
                if payment is None:
                    # Не наше письмо или не разобрали: оставляем непрочитанным,
                    # чтобы человек увидел его и разобрался.
                    logger.debug("Письмо %r не опознано как поступление", number)
                    continue
                if _as_utc(payment.received_at) < since_utc:
                    logger.debug("Письмо %r старше since — пропускаю", number)
                    continue
                payments.append(payment)
                if self.mark_seen:
                    self._mark_seen(connection, number)
        finally:
            self._logout(connection)

        if errors:
            raise _with_payload(
                StatementError("не удалось прочитать часть писем: " + "; ".join(errors)),
                payments,
                errors,
            )

        payments.sort(key=lambda item: _as_utc(item.received_at))
        return payments


# ---------------------------------------------------------------------------
# Фабрика источников
# ---------------------------------------------------------------------------

#: Параметры, которые понимает :class:`ImapStatementSource`.
_IMAP_KEYS = frozenset(
    {
        "host",
        "user",
        "password",
        "port",
        "folder",
        "use_ssl",
        "patterns",
        "max_messages",
        "mark_seen",
    }
)


def build_sources(
    *,
    csv_paths: str | None = None,
    imap: dict | None = None,
) -> list[StatementSource]:
    """Собрать источники выписок из настроек.

    :param csv_paths: glob-шаблон выписок; пусто — CSV-источник не создаётся.
    :param imap: параметры :class:`ImapStatementSource` (``host``, ``user``,
        ``password`` и необязательные). Пустой словарь — источник не создаётся.
    :returns: список источников; пустой список, если ничего не настроено.
    :raises StatementError: если параметры переданы, но неполные (например,
        для IMAP нет пароля) — молча «забыть» источник опаснее, чем упасть
        на старте.
    """
    sources: list[StatementSource] = []

    if csv_paths and str(csv_paths).strip():
        sources.append(CsvStatementSource(str(csv_paths).strip()))

    if imap:
        unknown = sorted(set(imap) - _IMAP_KEYS)
        if unknown:
            logger.warning("Игнорирую неизвестные параметры IMAP: %s", ", ".join(unknown))
        params = {key: value for key, value in imap.items() if key in _IMAP_KEYS}
        missing = [key for key in ("host", "user", "password") if not params.get(key)]
        if missing:
            raise StatementError(
                "для IMAP не хватает параметров: " + ", ".join(missing)
            )
        sources.append(ImapStatementSource(**params))

    return sources
