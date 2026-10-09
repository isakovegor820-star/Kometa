#!/usr/bin/env python3
"""Кадры для стори 1080×1920 в палитре бренда.

Зачем отдельный инструмент. Стори — единственный канал, где картинку видно
раньше текста, а готовых вертикальных кадров у бренда нет: баннеры и обложка
собраны горизонтально (1280×720, 2560×1440). Кадр для стори нужен ровно один
раз в наборе — «что это за сервис», — и он должен совпадать с брендом, а не
выглядеть как чужая реклама.

Палитра и шрифты — те же, что в ``brand/tools/make_assets.py`` (см. ``brand/README.md``).

Запуск (нужен Pillow; в боевом окружении бота его нет — это инструмент машины
дизайнера, поэтому запускается любым питоном с Pillow)::

    python3 brand/tools/make_story.py

Результат:

* ``brand/story/kometa-story-hook-1080x1920.png``  — крючок: «вечером интернет тупит?»;
* ``brand/story/kometa-story-offer-1080x1920.png`` — предложение: 3 дня бесплатно, цена, чипы.

Тексты кадров — публичные: их проверяет та же рамка, что посты и канал
(``docs/КАНАЛ.md`` §5, ``tests/test_public_texts_clean.py``). Ничего про доступ
к чему-либо закрытому, только качество связи.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
OUT_DIR = Path(__file__).resolve().parent.parent / "story"

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"
#: Arial не содержит знака рубля (вместо него рисуется пустой квадрат). Знак
#: берём из системного SF Pro: им же набран ценник на кадре-предложении.
FONT_RUBLE = "/System/Library/Fonts/SFNS.ttf"

BG_TOP = (6, 67, 47)  # #06432F — тень / край
BG_BOTTOM = (2, 35, 26)  # #02231A — глубокий фон
EMERALD = (26, 171, 117)  # #1AAB75 — свет изумруда
MINT = (69, 230, 152)  # #45E698 — акцент
LIME = (220, 255, 92)  # #DCFF5C — острие
SOFT = (155, 231, 196)  # подписи
WHITE = (240, 255, 249)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:  # pragma: no cover - не на macOS
        return ImageFont.load_default(size)


def background() -> Image.Image:
    """Вертикальный градиент плюс изумрудное свечение в центре кадра."""
    img = Image.new("RGB", (W, H), BG_BOTTOM)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        k = y / (H - 1)
        draw.line(
            [(0, y), (W, y)],
            fill=tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * k) for i in range(3)),
        )

    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cx, cy, r = W // 2, 820, 620
    gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=EMERALD)
    glow = glow.filter(ImageFilter.GaussianBlur(260))
    return Image.blend(img, Image.blend(img, glow, 0.55), 0.55)


def letterspaced(
    draw: ImageDraw.ImageDraw, text: str, y: int, fnt: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int], spacing: int = 16,
) -> None:
    """Надпись с разрядкой — фирменная манера бренда (KOMETA в макетах)."""
    widths = [draw.textlength(ch, font=fnt) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = (W - total) / 2
    for ch, width in zip(text, widths):
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += width + spacing


def centered(
    draw: ImageDraw.ImageDraw, text: str, y: int, fnt: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    width = draw.textlength(text, font=fnt)
    draw.text(((W - width) / 2, y), text, font=fnt, fill=fill)


def bold_variation(fnt: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
    """Взять полужирное начертание вариативного шрифта (SF Pro — variable font)."""
    try:
        fnt.set_variation_by_name("Bold")
    except Exception:  # pragma: no cover - шрифт без вариаций
        pass
    return fnt


def centered_mixed(
    draw: ImageDraw.ImageDraw, chunks: list[tuple[str, ImageFont.FreeTypeFont]],
    y: int, fill: tuple[int, int, int],
) -> None:
    """Строка из кусков разными шрифтами — по центру кадра.

    Нужна из-за одной буквы: «₽» есть в SF Pro и нет в Arial. Ставить весь
    ценник другим шрифтом — заметная разница в начертании, поэтому другим
    шрифтом набирается только сам знак.
    """
    widths = [draw.textlength(text, font=fnt) for text, fnt in chunks]
    x = (W - sum(widths)) / 2
    for (text, fnt), width in zip(chunks, widths):
        draw.text((x, y), text, font=fnt, fill=fill)
        x += width


def pill(
    draw: ImageDraw.ImageDraw, cx: int, y: int, text: str, fnt: ImageFont.FreeTypeFont,
    *, outline: tuple[int, int, int], fill: tuple[int, int, int], pad_x: int = 34,
    pad_y: int = 18,
) -> tuple[int, int]:
    """Чип с подписью. Возвращает ширину: чипы ставим в ряд по центру."""
    width = draw.textlength(text, font=fnt) + pad_x * 2
    height = fnt.size + pad_y * 2
    box = [cx - width / 2, y, cx + width / 2, y + height]
    draw.rounded_rectangle(box, radius=height // 2, outline=outline, width=3, fill=(4, 44, 32))
    draw.text((cx - draw.textlength(text, font=fnt) / 2, y + pad_y - 2), text, font=fnt, fill=fill)
    return round(width)


def wordmark(draw: ImageDraw.ImageDraw) -> None:
    """Знак сервиса: имя в разрядку и лаймовая точка-комета — как на аватарке."""
    fnt = font(FONT_BOLD, 40)
    letterspaced(draw, "KOMETA", 268, fnt, MINT, spacing=18)
    draw.ellipse([W / 2 + 118, 306, W / 2 + 130, 318], fill=LIME)


def frame_hook() -> Image.Image:
    """Крючок: вопрос, который человек узнаёт как свой вечерний опыт."""
    img = background()
    draw = ImageDraw.Draw(img)
    wordmark(draw)
    headline = font(FONT_BOLD, 112)
    centered(draw, "Вечером интернет", 720, headline, WHITE)
    centered(draw, "тупит?", 848, headline, LIME)
    centered(draw, "Причина — вечерняя перегрузка сети", 1060, font(FONT_REG, 46), SOFT)
    centered(draw, "у оператора, а не поломка у тебя", 1122, font(FONT_REG, 46), SOFT)
    return img


def frame_offer() -> Image.Image:
    """Предложение: пробный доступ, цена и три чипа — то, что отвечает «сколько стоит»."""
    img = background()
    draw = ImageDraw.Draw(img)
    wordmark(draw)
    centered(draw, "3 дня бесплатно", 700, font(FONT_BOLD, 124), LIME)
    centered(draw, "карта не нужна · автосписаний нет", 880, font(FONT_REG, 46), WHITE)

    chips = ("Безлимит", "Три локации", "До 3 устройств")
    chip_font = font(FONT_BOLD, 36)
    widths = [round(draw.textlength(text, font=chip_font) + 68) for text in chips]
    gap = 24
    total = sum(widths) + gap * (len(chips) - 1)
    x = (W - total) / 2
    for text, width in zip(chips, widths):
        pill(
            draw, round(x + width / 2), 1010, text, chip_font,
            outline=(26, 110, 80), fill=MINT,
        )
        x += width + gap

    price_font = font(FONT_BOLD, 54)
    centered_mixed(
        draw,
        [
            ("от 120 ", price_font),
            ("₽", bold_variation(font(FONT_RUBLE, 54))),
            (" в месяц", price_font),
        ],
        1190,
        WHITE,
    )
    centered(draw, "Kometa · защищённое подключение", 1690, font(FONT_REG, 36), SOFT)
    return img


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, image in (
        ("kometa-story-hook-1080x1920.png", frame_hook()),
        ("kometa-story-offer-1080x1920.png", frame_offer()),
    ):
        path = OUT_DIR / name
        image.save(path, format="PNG", optimize=True)
        print(f"✅ {path.relative_to(Path.cwd()) if Path.cwd() in path.parents else path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
