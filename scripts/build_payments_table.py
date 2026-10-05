"""Сборка сравнительной таблицы платёжных посредников из TSV-файлов исследования.

Исследование по категориям лежит в ``docs/research/*.tsv`` (по строке на сервис).
Скрипт склеивает их в один XLSX с фильтрами и подсветкой: удобно сравнивать
каналы приёма денег и выбирать, что подключать к боту.

Запуск:
    python scripts/build_payments_table.py

Колонки TSV (ровно 16, первая строка — заголовок):
    id, Название, Категория, VPN-политика, Гео, Способы оплаты, Комиссия,
    Сбор за подключение, Резерв/холд, Выплаты, Требования к подключающему,
    Рекуррент, Интеграция, Риски, Источники, Уверенность
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "docs" / "research"
OUTPUT = ROOT / "docs" / "ОПЛАТЫ-СРАВНЕНИЕ.xlsx"

COLUMNS = [
    "id",
    "Название",
    "Категория",
    "VPN-политика",
    "Гео",
    "Способы оплаты",
    "Комиссия",
    "Сбор за подключение",
    "Резерв/холд",
    "Выплаты",
    "Требования к подключающему",
    "Рекуррент",
    "Интеграция",
    "Риски",
    "Источники",
    "Уверенность",
]

WIDTHS = [22, 24, 18, 26, 14, 30, 34, 22, 20, 30, 30, 12, 30, 46, 40, 12]

#: Подсветка колонки «VPN-политика»: зелёный — берут, красный — запрет.
FILL_YES = PatternFill("solid", fgColor="D9F2D9")
FILL_NO = PatternFill("solid", fgColor="F8D7DA")
FILL_GRAY = PatternFill("solid", fgColor="FFF3CD")
FILL_UNKNOWN = PatternFill("solid", fgColor="EDEDED")

UNKNOWN = ("не подтверждено", "не применимо", "неизвестно", "нет данных")
#: «серая зона» — только как слово: подстрока «сер» ловится в слове «сервис».
GRAY_RE = re.compile(r"\bсер(ая|ой|ую|ые|ый|ым|о)\b|\bнеясн|\bспорн|\bмолч|\bне регламент")
NEGATIVE = ("прямой запрет", "прямо запрещ", "запрещено", "запрещен", "отказ", "не подключа",
            "prohibit", "не разреш", "нарушение правил")
POSITIVE = ("разрешено", "принимает", "да:", "да,", "работает с vpn", "не запрещено",
            "нет запрета", "allow", "обслужива")


def classify(value: str) -> PatternFill:
    """Цвет для колонки «VPN-политика»: берут / серая зона / запрет / неизвестно.

    Порядок проверок важен: «не подтверждено» не должно попадать в «запрет»
    из-за слова «нет», а «серая зона, прямого запрета нет» — в «запрет»
    из-за отрицания.
    """
    text = (value or "").strip().lower()
    if not text or text in {"—", "-"}:
        return FILL_UNKNOWN
    if any(word in text for word in UNKNOWN):
        return FILL_UNKNOWN
    if GRAY_RE.search(text):
        return FILL_GRAY
    if any(word in text for word in NEGATIVE):
        return FILL_NO
    if any(word in text for word in POSITIVE):
        return FILL_YES
    return FILL_UNKNOWN


#: Порядок чтения TSV. Предметные категории идут раньше обзорных: если сервис
#: описан и в обзорном файле, и в профильном (например, ЮKassa — в «маркетплейсах»
#: и в «российском эквайринге»), в таблицу попадает профильное описание.
PRIORITY = [
    "crypto.tsv",
    "ru-acquiring.tsv",
    "global-highrisk.tsv",
    "telegram-digital.tsv",
    "marketplaces.tsv",
]

RANK = {"высокая": 3, "средняя": 2, "низкая": 1}

#: Разные категории называют один сервис по-разному и нумеруют строки
#: по-своему (A07, 17, cryptopay), поэтому ключ уникальности — название сервиса,
#: а не id. Здесь сводим составные названия к основному сервису.
ALIASES = {
    "crypto pay / cryptobot": "crypto pay",
    "digiseller / plati.market": "plati.market",
}


def confidence_rank(row: list[str]) -> int:
    return RANK.get(row[-1].strip().lower(), 0)


def dedupe_key(raw_name: str) -> str:
    """Ключ уникальности: название без домена в скобках, без лишних пробелов."""
    text = re.sub(r"\(.*?\)", "", raw_name).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return ALIASES.get(text, text)


def read_rows() -> list[list[str]]:
    """Собрать строки из всех TSV, убрав дубли по названию сервиса.

    Один и тот же сервис встречается в нескольких категориях (Crypto Pay,
    FunPay, ЮKassa). При совпадении названия оставляем строку с более высокой
    уверенностью, а при равной — из файла с более высоким приоритетом.
    """
    rows: list[list[str]] = []
    seen: dict[str, tuple[str, int]] = {}
    all_files = {path.name: path for path in RESEARCH.glob("*.tsv")}
    ordered = [all_files.pop(name) for name in PRIORITY if name in all_files]
    ordered += [all_files[name] for name in sorted(all_files)]
    if not ordered:
        raise SystemExit(f"Не найдено ни одного TSV в {RESEARCH}")
    for path in ordered:
        text = path.read_text(encoding="utf-8").strip().splitlines()
        if not text:
            continue
        header = [cell.strip() for cell in text[0].split("\t")]
        if header[: len(COLUMNS)] != COLUMNS:
            print(f"! {path.name}: заголовок не совпал с ожидаемым — пропускаю", file=sys.stderr)
            print(f"  получено: {header[:6]}...", file=sys.stderr)
            continue
        for line_no, line in enumerate(text[1:], start=2):
            if not line.strip():
                continue
            cells = line.split("\t")
            if len(cells) != len(COLUMNS):
                print(
                    f"! {path.name}:{line_no}: {len(cells)} колонок вместо {len(COLUMNS)} — строка пропущена",
                    file=sys.stderr,
                )
                continue
            row = [cell.strip() for cell in cells]
            key = dedupe_key(row[1])
            rank = confidence_rank(row)
            previous = seen.get(key)
            if previous is not None:
                if rank <= previous[1]:
                    print(f"· {path.name}:{line_no}: {row[1]} — дубль ({previous[0]}, уверенность выше) — пропускаю")
                    continue
                print(f"↑ {path.name}:{line_no}: {row[1]} заменяет строку из {previous[0]} (уверенность выше)")
                rows = [r for r in rows if dedupe_key(r[1]) != key]
            seen[key] = (path.name, rank)
            rows.append(row)
    return rows


def build(rows: list[list[str]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Посредники"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.append(COLUMNS)
    for idx, _ in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(idx)].width = WIDTHS[idx - 1]
    ws.row_dimensions[1].height = 32

    for row in rows:
        ws.append(row)

    policy_col = COLUMNS.index("VPN-политика") + 1
    conf_col = COLUMNS.index("Уверенность") + 1
    for row_idx in range(2, ws.max_row + 1):
        for col_idx in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border
        ws.cell(row=row_idx, column=policy_col).fill = classify(
            ws.cell(row=row_idx, column=policy_col).value or ""
        )
        ws.cell(row=row_idx, column=conf_col).alignment = Alignment(vertical="top", horizontal="center")

    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{ws.max_row}"

    # Второй лист: только те, кто VPN принимает или колеблется — короткий шортлист.
    short = wb.create_sheet("Шортлист")
    short.append(["Название", "Категория", "VPN-политика", "Комиссия", "Требования", "Интеграция"])
    for row in rows:
        fill = classify(row[3])
        if fill in (FILL_YES, FILL_GRAY):
            short.append([row[1], row[2], row[3], row[6], row[10], row[12]])
    for idx, width in enumerate([26, 18, 28, 36, 30, 32], start=1):
        short.column_dimensions[get_column_letter(idx)].width = width
    for cell in short[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
    for row_idx in range(2, short.max_row + 1):
        for col_idx in range(1, 7):
            short.cell(row=row_idx, column=col_idx).alignment = Alignment(vertical="top", wrap_text=True)
    short.freeze_panes = "A2"
    short.auto_filter.ref = f"A1:F{short.max_row}"

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT)
    print(f"Готово: {OUTPUT} — строк в таблице: {len(rows)}, в шортлисте: {short.max_row - 1}")


if __name__ == "__main__":
    build(read_rows())
