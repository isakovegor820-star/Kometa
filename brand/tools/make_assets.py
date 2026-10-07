#!/usr/bin/env python3
"""Сборка ассетов аватара Kometa из SVG-исходника.

Делает всё, что не требует Chromium:
  * достаёт <svg> из HTML-артефакта и сохраняет векторный исходник;
  * режет рендеры Open Design (2880x2000) в квадратные PNG нужных размеров;
  * собирает круглые версии с прозрачностью и превью-лист.

Рендер PNG выполняет Open Design (см. build_avatar.sh), сюда приходят .raw.png.
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"
PALETTE = [
    ("#02231A", "глубокий фон"),
    ("#06432F", "тень / край"),
    ("#0E7350", "основной зелёный"),
    ("#1AAB75", "свет изумруда"),
    ("#45E698", "акцент"),
    ("#DCFF5C", "лайм / острие"),
]


def font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def extract_svg(html_path: Path, out_path: Path) -> str:
    html = html_path.read_text(encoding="utf-8")
    match = SVG_RE.search(html)
    if not match:
        raise SystemExit(f"в {html_path} не найден <svg>")
    svg = match.group(0)
    if "xmlns=" not in svg:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + svg + "\n", encoding="utf-8")
    return svg


def square_crop(raw: Path) -> Image.Image:
    """Артефакт рендерится как 100vmin по центру страницы — берём центральный квадрат."""
    im = Image.open(raw).convert("RGB")
    w, h = im.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def round_mask(size: int, ss: int = 4) -> Image.Image:
    big = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * ss - 1, size * ss - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def crop_16x9(raw: Path) -> Image.Image:
    """Баннер вёрстан как кадр 16:9 по центру страницы — берём центральную полосу."""
    im = Image.open(raw).convert("RGB")
    w, h = im.size
    target_h = round(w * 9 / 16)
    if target_h > h:  # страница уже кадра — режем по ширине
        target_w = round(h * 16 / 9)
        left = (w - target_w) // 2
        return im.crop((left, 0, left + target_w, h))
    top = (h - target_h) // 2
    return im.crop((0, top, w, top + target_h))


def save_banner(raw: Path, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    band = crop_16x9(raw)
    for px in (1280, 2560):
        band.resize((px, round(px * 9 / 16)), Image.LANCZOS).save(
            out_dir / f"{name}-{px}x{round(px * 9 / 16)}.png", optimize=True
        )


def save_set(square: Image.Image, base: Path, name: str, with_round: bool = True,
             with_jpeg: bool = False) -> None:
    base.mkdir(parents=True, exist_ok=True)
    for px in (1024, 512, 256):
        square.resize((px, px), Image.LANCZOS).save(base / f"{name}-{px}.png", optimize=True)
    if with_round:
        rnd = square.resize((512, 512), Image.LANCZOS)
        rnd.putalpha(round_mask(512))
        rnd.save(base / f"{name}-round-512.png", optimize=True)
    if with_jpeg:
        # BotFather иногда капризничает на тяжёлый PNG — дублируем в JPEG
        square.resize((512, 512), Image.LANCZOS).save(
            base / f"{name}-512.jpg", format="JPEG", quality=92, optimize=True, progressive=True
        )


def data_uri(svg: str) -> str:
    payload = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{payload}"


def build_preview_html(svg: str, out_html: Path) -> None:
    uri = data_uri(svg)
    swatches = "".join(
        f'<div class="sw"><span style="background:{hex_}"></span><b>{hex_}</b><i>{label}</i></div>'
        for hex_, label in PALETTE
    )
    rows = ""
    for theme in ("dark", "light"):
        rows += f'''
      <div class="chat {theme}">
        <img class="ava" src="{uri}" alt="">
        <div class="meta">
          <div class="top"><b>Kometa VPN</b><span>12:04</span></div>
          <p>Подписка активна · 2 устройства</p>
        </div>
      </div>'''
    html = f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Kometa — превью аватара</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; background: #070b0d; color: #e8f3ee;
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', system-ui, sans-serif; overflow: hidden; }}
  .page {{ width: 100vw; height: 100vh; padding: 54px 64px; display: grid; gap: 34px;
    grid-template-columns: 1fr 1.05fr; align-content: start; }}
  h1 {{ margin: 0; font-size: 40px; letter-spacing: -0.02em; }}
  h1 small {{ display: block; margin-top: 8px; font-size: 17px; font-weight: 500; color: #7ba894; letter-spacing: 0; }}
  .card {{ background: #0c1417; border: 1px solid #17272b; border-radius: 26px; padding: 30px; }}
  .hero {{ display: flex; align-items: center; gap: 28px; }}
  .hero > img {{ width: 260px; height: 260px; border-radius: 50%; flex: none;
    box-shadow: 0 24px 60px rgba(20,220,130,.22); }}
  .scales {{ display: flex; align-items: flex-end; gap: 20px; }}
  .scales figure {{ margin: 0; text-align: center; }}
  .scales img {{ display: block; border-radius: 50%; margin: 0 auto 8px; }}
  .scales .s128 {{ width: 128px; height: 128px; }}
  .scales .s64  {{ width: 64px;  height: 64px; }}
  .scales .s40  {{ width: 40px;  height: 40px; }}
  .scales figcaption {{ font-size: 12px; color: #6f9486; }}
  .chat {{ display: flex; align-items: center; gap: 14px; padding: 14px 18px; border-radius: 18px; }}
  .chat.dark {{ background: #17212b; }}
  .chat.light {{ background: #ffffff; color: #101b16; margin-top: 14px; }}
  .chat .ava {{ width: 54px; height: 54px; border-radius: 50%; }}
  .meta {{ flex: 1; min-width: 0; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .top b {{ font-size: 16px; }}
  .top span {{ font-size: 12px; color: #6f8a80; }}
  .chat.light .top span {{ color: #8ba49a; }}
  .meta p {{ margin: 4px 0 0; font-size: 13.5px; color: #8fb3a4; }}
  .chat.light .meta p {{ color: #5d7a6f; }}
  .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: .14em; color: #6f9486; margin: 0 0 14px; }}
  .palette {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-top: 20px; }}
  .sw span {{ display: block; height: 54px; border-radius: 12px; border: 1px solid #1d3034; }}
  .sw b {{ display: block; font-size: 12px; margin-top: 8px; }}
  .sw i {{ font-style: normal; font-size: 11px; color: #6f9486; }}
</style></head>
<body><div class="page">
  <div>
    <h1>Kometa — аватар бота<small>Зелёный рост: комета-стрела, изумруд + лайм</small></h1>
    <div class="card" style="margin-top:26px">
      <p class="label">Основной знак</p>
      <div class="hero"><img src="{uri}" alt="аватар"><div class="scales">
        <figure><img class="s128" src="{uri}" alt=""><figcaption>128</figcaption></figure>
        <figure><img class="s64" src="{uri}" alt=""><figcaption>64</figcaption></figure>
        <figure><img class="s40" src="{uri}" alt=""><figcaption>40</figcaption></figure>
      </div></div>
    </div>
    <div class="card" style="margin-top:22px">
      <p class="label">Палитра</p>
      <div class="palette">{swatches}</div>
    </div>
  </div>
  <div>
    <div class="card">
      <p class="label">В списке чатов Telegram</p>
      {rows}
    </div>
    <div class="card" style="margin-top:22px">
      <p class="label">Крупный кроп</p>
      <img src="{uri}" style="width:100%;border-radius:24px" alt="аватар крупно">
    </div>
  </div>
</div></body></html>'''
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["prepare", "finalize"], help="prepare — исходники, finalize — PNG")
    ap.add_argument("--brand", required=True, help="каталог brand/")
    ap.add_argument("--raw", default="/tmp/od-avatar-raw", help="каталог с *.raw.png от Open Design")
    args = ap.parse_args()

    brand = Path(args.brand)
    raw = Path(args.raw)
    avatar_dir = brand / "avatar"
    source_dir = brand / "source"

    svg = extract_svg(source_dir / "avatar-a-comet-arrow.html", avatar_dir / "kometa-avatar.svg")
    build_preview_html(svg, source_dir / "preview-sheet.html")
    if args.stage == "prepare":
        print("подготовлено: kometa-avatar.svg, preview-sheet.html")
        return 0

    plan = [
        ("avatar-a-comet-arrow", "kometa-avatar", avatar_dir, True, True),
        ("avatar-b-growth-bars", "kometa-bars", avatar_dir / "alternates", True, True),
        ("avatar-c-monogram-k", "kometa-monogram", avatar_dir / "alternates", True, True),
    ]
    for raw_name, out_name, out_dir, with_round, with_jpeg in plan:
        path = raw / f"{raw_name}.raw.png"
        if not path.exists():
            print(f"пропуск: нет {path}", file=sys.stderr)
            continue
        save_set(square_crop(path), out_dir, out_name, with_round, with_jpeg)
        print(f"{out_name}: готово")

    # баннеры 16:9: приветствие в боте и обложка канала
    banner_dir = brand / "banner"
    for raw_name, out_name in (
        ("banner-welcome", "kometa-welcome"),
        ("banner-channel-cover", "kometa-channel-cover"),
    ):
        path = raw / f"{raw_name}.raw.png"
        if not path.exists():
            print(f"пропуск: нет {path}", file=sys.stderr)
            continue
        save_banner(path, banner_dir, out_name)
        print(f"{out_name}: готово")

    # превью-лист рендерится из артефакта и просто уменьшается
    sheet = raw / "preview-sheet.raw.png"
    if sheet.exists():
        im = Image.open(sheet).convert("RGB")
        im.resize((1440, 1000), Image.LANCZOS).save(avatar_dir / "preview.png", optimize=True)
        print("preview.png: готово")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
