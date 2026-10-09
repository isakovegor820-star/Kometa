#!/usr/bin/env python3
"""Сборка самодостаточных HTML-макетов экранов бота Kometa.

Зачем генератор, а не 10 файлов руками: у всех экранов один визуальный кит и один
мокап клиента Telegram. Кит лежит в kit.css, мокап каждого экрана — в parts/*.html
между маркерами <!--PHONE--> … <!--/PHONE-->. Сводный борд собирается из ТЕХ ЖЕ
фрагментов, поэтому борд и отдельный экран не могут разойтись.

Каждый результат — один самодостаточный HTML: токены + кит внутри <style>,
картинки бренда — data-URI base64 (никаких внешних файлов и CDN).

Запуск (из корня репозитория):

    python3 design/bot/tools/screens/build.py                 # все 9 экранов + борд
    python3 design/bot/tools/screens/build.py --only 01-start-new
    python3 design/bot/tools/screens/build.py --out /tmp/проверка   # ничего не перезаписывая

PNG в design/bot/renders/ — артефакт: собирается из этих HTML командой
design/bot/tools/screens/render.sh. Руками PNG не правим.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path

#: Корень репозитория: …/design/bot/tools/screens/build.py → на четыре уровня выше.
ROOT = Path(__file__).resolve().parents[4]
SRC = Path(__file__).resolve().parent
OUT = ROOT / "design" / "bot" / "screens"

BANNER = ROOT / "design" / "bot" / "assets" / "banner" / "kometa-start-hero-1280x720.jpg"
AVATAR = ROOT / "brand" / "avatar" / "kometa-avatar-round-512.png"

if not (ROOT / "app" / "bot" / "texts.py").exists():  # страховка от переноса файла
    sys.exit(f"не нашли корень репозитория от {Path(__file__).resolve()} — ожидали {ROOT}")

#: Общие фрагменты клиента Telegram: строка состояния и шапка чата.
STATUS = """<div class="tg-status">
      <span>18:05</span>
      <span class="ico">
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><rect x="0" y="12" width="3" height="6" rx="1"/><rect x="4.5" y="9" width="3" height="9" rx="1"/><rect x="9" y="6" width="3" height="12" rx="1"/><rect x="13.5" y="3" width="3" height="15" rx="1" opacity=".45"/></svg>
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M2 7.2c4.6-3.9 11.4-3.9 16 0"/><path d="M4.8 10.6c3-2.6 7.4-2.6 10.4 0"/><path d="M7.6 13.9c1.4-1.2 3.4-1.2 4.8 0"/><circle cx="10" cy="16.6" r="1" fill="currentColor" stroke="none"/></svg>
        <svg viewBox="0 0 24 20" fill="none" aria-hidden="true"><rect x="1" y="5" width="18" height="10" rx="3" stroke="currentColor" stroke-width="1.4"/><rect x="3" y="7" width="12" height="6" rx="1.6" fill="currentColor"/><path d="M21 8.5v3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </span>
    </div>"""

HEAD = """<div class="tg-head">
      <svg class="tg-back" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 5 7 11l6.5 6"/></svg>
      <img class="tg-ava" src="{{AVATAR}}" alt="Kometa">
      <div class="tg-who"><span class="tg-name">Kometa</span><span class="tg-sub">бот</span></div>
      <span class="tg-dots">⋮</span>
    </div>"""

#: (номер, slug, заголовок вкладки, подпись для борда)
SCREENS: list[tuple[str, str, str, str]] = [
    ("01", "start-new", "/start — новый клиент",
     "Одно сообщение: фото, подпись на 279 знаков и три кнопки"),
    ("02", "start-active", "/start — действующий клиент",
     "Статус в трёх строках и четыре кнопки вместо восьми"),
    ("03", "start-expired", "/start — подписка закончилась",
     "Спокойно и честно: доступ отключён, настройки целы, один путь вернуть"),
    ("04", "menu-active", "Моя подписка — карточка",
     "Карточка подписки, ссылка-подписка и локации с замером задержки"),
    ("05", "plans", "Тарифы",
     "Три срока, цена за месяц, честные бейджи выгоды, промокод"),
    ("06", "pay", "Оплата заказа",
     "Сумма со скидкой строкой, способы оплаты, один главный CTA"),
    ("07", "connect", "Как подключить",
     "Инструкция в 3 шага и кнопки приложений: профиль добавляется сам"),
    ("08", "support", "Поддержка",
     "Честные сроки ответа и что написать, чтобы решить вопрос с первого раза"),
    ("09", "error-node", "Локация недоступна",
     "Деградация без паники: что случилось, что сделать, куда нажать"),
]


def data_uri(path: Path) -> str:
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def avatar_uri(path: Path, size: int = 96) -> str:
    """Аватар для шапки чата: он показывается 36 px, при экспорте ×2 нужен 72 px.

    Исходный 512 px (197 КБ → 263 КБ base64) в каждом файле раздувает HTML:
    на борде из девяти мокапов data-URI перестаёт открываться вовсе
    (Chromium: ERR_INVALID_URL). Поэтому уменьшаем знак до 96 px.
    """
    try:
        import io

        from PIL import Image

        with Image.open(path) as img:
            small = img.convert("RGBA").resize((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            small.save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 - без Pillow просто берём исходник
        return data_uri(path)


def fill(text: str, assets: dict[str, str]) -> str:
    for key, value in assets.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def extract_phone(markup: str) -> str:
    match = re.search(r"<!--PHONE-->(.*?)<!--/PHONE-->", markup, re.S)
    if match is None:
        raise SystemExit("в part-файле нет маркеров <!--PHONE--> … <!--/PHONE-->")
    return match.group(1).strip()


def page(title: str, kit: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{title} — Kometa Bot</title>\n<style>\n{kit}\n</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Сборка HTML-макетов экранов бота Kometa.")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NN-slug",
        help="собрать только эти экраны (например --only 01-start-new); можно повторять",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT,
        help=f"куда писать HTML (по умолчанию {OUT.relative_to(ROOT)})",
    )
    parser.add_argument("--no-board", action="store_true", help="не собирать сводный борд 00-board")
    args = parser.parse_args(argv)

    selected = [s for s in SCREENS if not args.only or f"{s[0]}-{s[1]}" in args.only]
    unknown = set(args.only) - {f"{s[0]}-{s[1]}" for s in SCREENS}
    if unknown:
        parser.error(f"неизвестные экраны: {', '.join(sorted(unknown))}")

    out_dir: Path = args.out
    kit_raw = (SRC / "kit.css").read_text(encoding="utf-8")
    assets = {"BANNER": data_uri(BANNER), "AVATAR": avatar_uri(AVATAR)}
    kit = fill(kit_raw, assets)
    out_dir.mkdir(parents=True, exist_ok=True)

    phones: list[str] = []
    made: list[tuple[str, str, str, str]] = []
    for num, slug, title, desc in selected:
        part_path = SRC / "parts" / f"{num}-{slug}.html"
        if not part_path.exists():
            print(f"· {num}-{slug}.html  — part ещё не написан, пропускаю")
            continue
        part = part_path.read_text(encoding="utf-8").strip()
        # Сначала вставляем общие фрагменты (в них тоже есть {{AVATAR}}),
        # потом — ассеты: иначе placeholder внутри HEAD останется неразобранным.
        part = fill(part, {"STATUS": STATUS, "HEAD": HEAD})
        part = fill(part, assets)
        # Линтер Open Design (P2) требует якорь на каждом верхнеуровневом <section>.
        part = part.replace('<section class="stage">', '<section class="stage" data-od-id="stage">')
        phones.append(extract_phone(part))
        made.append((num, slug, title, desc))
        html = page(title, kit, part)
        (out_dir / f"{num}-{slug}.html").write_text(html, encoding="utf-8")
        print(f"· {num}-{slug}.html  {len(html) // 1024} КБ  → {out_dir}")

    if args.no_board or args.only:
        if args.only:
            print("· 00-board.html  — не собираю: выбран режим --only (борд строится из всех девяти)")
        return

    cells = []
    for (num, slug, title, desc), phone in zip(made, phones, strict=True):
        cells.append(
            f"""    <figure class="cell">
      <div class="shot">{phone}</div>
      <figcaption><span class="no">Экран {num}</span><span class="nm">{title}</span><span class="ds">{desc}</span></figcaption>
    </figure>"""
        )
    board_body = f"""<main class="board">
  <header class="board-head">
    <div>
      <div class="eyebrow">Kometa Bot · дизайн-система v1</div>
      <h1>9 ключевых экранов бота</h1>
    </div>
    <p class="sub">Тёмный клиент Telegram, изумрудный акцент, один главный призыв на экран.
    Тексты — <code>design/bot/START-COPY.md</code> и <code>app/bot/texts.py</code>,
    цены — тарифы <code>seed_plans</code>.</p>
  </header>
  <div class="grid">
{chr(10).join(cells)}
  </div>
  <div class="legend">
    <span><b>Цвета, радиусы, кегли</b> — design/bot/tokens.css</span>
    <span><b>Кнопки</b> — primary: одно главное действие · danger: ровно одна на экран · neutral: навигация</span>
    <span><b>Задержка локаций</b> — замер с сервера, не с телефона (app/web/sub.py)</span>
  </div>
</main>"""
    board = page("Сводный борд 3×3", kit, board_body)
    (out_dir / "00-board.html").write_text(board, encoding="utf-8")
    print(f"· 00-board.html  {len(board) // 1024} КБ  → {out_dir}")


if __name__ == "__main__":
    main()
