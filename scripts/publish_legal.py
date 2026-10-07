#!/usr/bin/env python
"""Публикация юридических документов в Telegra.ph.

Запуск:

    .venv/bin/python scripts/publish_legal.py            # опубликовать/обновить
    .venv/bin/python scripts/publish_legal.py --dry-run  # проверить разметку без сети

Что делает:
  1. собирает документы из `app/legal_texts.py` (те же тексты, что отдаёт бот);
  2. заводит анонимный аккаунт Telegra.ph (или берёт сохранённый токен);
  3. публикует три страницы: политику, соглашение и прайс;
  4. прописывает ссылки в `.env` (PRIVACY_URL, TERMS_URL, PRICING_URL) — после
     перезапуска бота кнопки в разделе «📄 Документы и цены» становятся ссылками.

Повторный запуск **обновляет те же страницы**: адрес не меняется, поэтому
в банке и у клиентов не появляются «новые» ссылки. Для этого токен аккаунта
сохраняется в `.env` (TELEGRAPH_ACCESS_TOKEN), а пути страниц — в
`data/telegraph_pages.json`. Оба файла не попадают в git.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import legal_texts  # noqa: E402
from app.config import BASE_DIR, get_settings  # noqa: E402
from app.legal_texts import LegalContext  # noqa: E402
from app.services import documents  # noqa: E402

API = "https://api.telegra.ph/"
STATE_FILE = BASE_DIR / "data" / "telegraph_pages.json"
ENV_FILE = BASE_DIR / ".env"

#: Ключ документа → (заголовок страницы, функция рендера)
DOCS: dict[str, tuple[str, str]] = {
    "privacy": ("Политика конфиденциальности", "privacy"),
    "terms": ("Пользовательское соглашение", "terms"),
    "pricing": ("Цены и тарифы Kometa", "pricing"),
}

#: Ключ документа → переменная в .env, куда попадает ссылка
ENV_KEYS = {"privacy": "PRIVACY_URL", "terms": "TERMS_URL", "pricing": "PRICING_URL"}


# ------------------------------------------------------------------ сеть
def telegraph_call(method: str, **params: object) -> dict:
    """Запрос к API Telegra.ph. Список узлов content передаётся JSON-строкой."""
    payload = {}
    for key, value in params.items():
        if isinstance(value, (list, dict)):
            payload[key] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            payload[key] = "true" if value else "false"
        else:
            payload[key] = str(value)

    request = urllib.request.Request(API + method, data=urllib.parse.urlencode(payload).encode())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read().decode())
    except Exception as exc:  # noqa: BLE001 - сеть может быть недоступна
        raise SystemExit(f"❌ Telegra.ph недоступен: {exc}") from exc

    if not answer.get("ok"):
        raise SystemExit(f"❌ Telegra.ph вернул ошибку: {answer.get('error')}")
    return answer["result"]


# ------------------------------------------------------------------ разметка
BOLD_LINE = re.compile(r"^<b>(?P<text>.+)</b>$")
INLINE = re.compile(r"(<b>.*?</b>|<i>.*?</i>)", re.DOTALL)


def inline_nodes(text: str) -> list:
    """Разобрать <b>/<i> в узлы Telegra.ph."""
    nodes: list = []
    for chunk in INLINE.split(text):
        if not chunk:
            continue
        if chunk.startswith("<b>"):
            nodes.append({"tag": "b", "children": [chunk[3:-4]]})
        elif chunk.startswith("<i>"):
            nodes.append({"tag": "i", "children": [chunk[3:-4]]})
        else:
            nodes.append(chunk)
    return nodes


def to_nodes(document: str) -> list[dict]:
    """Превратить документ (HTML-разметка Telegram) в узлы Telegra.ph.

    Telegra.ph принимает не HTML, а дерево узлов, поэтому конвертируем
    осознанно: абзацы → p, отдельные жирные строки → h3, списки «•» → ul/li.
    """
    blocks = [block.strip() for block in document.split("\n\n") if block.strip()]
    nodes: list[dict] = []

    for index, block in enumerate(blocks):
        lines = [line for line in block.split("\n") if line.strip()]

        # Первый блок: название документа (уходит в заголовок страницы) + дата редакции.
        if index == 0 and BOLD_LINE.match(lines[0]):
            rest = lines[1:]
            if rest:
                nodes.append({"tag": "p", "children": [{"tag": "i", "children": [" ".join(rest)]}]})
            continue

        if len(lines) == 1:
            match = BOLD_LINE.match(lines[0])
            if match:
                nodes.append({"tag": "h3", "children": inline_nodes(match.group("text"))})
                continue

        # Блок может смешивать абзац и список: «2.1. Текст:» + пункты «•».
        paragraph: list[str] = []
        bullets: list[str] = []

        def flush_paragraph() -> None:
            if paragraph:
                nodes.append({"tag": "p", "children": inline_nodes(" ".join(paragraph))})
                paragraph.clear()

        def flush_bullets() -> None:
            if bullets:
                nodes.append(
                    {
                        "tag": "ul",
                        "children": [{"tag": "li", "children": inline_nodes(item)} for item in bullets],
                    }
                )
                bullets.clear()

        for line in lines:
            if line.startswith("• "):
                flush_paragraph()
                bullets.append(line[2:].strip())
            else:
                flush_bullets()
                paragraph.append(line.strip())
        flush_paragraph()
        flush_bullets()

    return nodes


# ------------------------------------------------------------------ состояние
def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def write_env(**updates: str) -> None:
    """Обновить ключи в .env, не задевая остальные строки и секреты."""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = text.splitlines()
    remaining = dict(updates)

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_account(env: dict[str, str], author_name: str, author_url: str) -> str:
    """Токен анонимного аккаунта Telegra.ph: из .env или новый."""
    token = env.get("TELEGRAPH_ACCESS_TOKEN", "")
    if token:
        return token
    result = telegraph_call(
        "createAccount",
        short_name="kometa",
        author_name=author_name,
        author_url=author_url,
    )
    token = result["access_token"]
    write_env(TELEGRAPH_ACCESS_TOKEN=token)
    print("🔑 Создан аккаунт Telegra.ph, токен сохранён в .env (TELEGRAPH_ACCESS_TOKEN)")
    return token


def publish(token: str, key: str, title: str, nodes: list[dict], author_name: str, author_url: str, state: dict) -> str:
    """Создать страницу или обновить уже созданную — адрес сохраняется."""
    known = state.get(key, {})
    if known.get("path"):
        telegraph_call(
            "editPage",
            access_token=token,
            path=known["path"],
            title=title,
            content=nodes,
            author_name=author_name,
            author_url=author_url,
        )
        print(f"♻️  Обновлено: {title} — {known['url']}")
        return known["url"]

    page = telegraph_call(
        "createPage",
        access_token=token,
        title=title,
        content=nodes,
        author_name=author_name,
        author_url=author_url,
        return_content="false",
    )
    state[key] = {"path": page["path"], "url": page["url"]}
    save_state(state)
    print(f"✅ Опубликовано: {title} — {page['url']}")
    return page["url"]


async def load_prices():  # noqa: ANN201 - tuple[PriceRow, ...]
    """Цены берём из базы — те же, что видит клиент в боте."""
    try:
        from app.db.session import SessionMaker
        from app.services import orders

        async with SessionMaker() as session:
            plans = await orders.list_plans(session)
        if plans:
            return documents.prices_from_plans(plans)
    except Exception as exc:  # noqa: BLE001 - база может быть недоступна
        print(f"⚠️  Тарифы из базы не прочитаны ({exc}); беру справочник из кода.")
    return documents.DEFAULT_PRICES


def build_context(prices) -> LegalContext:  # noqa: ANN001
    settings = get_settings()
    return LegalContext(
        operator=settings.legal_operator_name.strip() or legal_texts.OPERATOR_PLACEHOLDER,
        inn=settings.legal_operator_inn.strip() or legal_texts.INN_PLACEHOLDER,
        support=settings.support_contact or legal_texts.SUPPORT_PLACEHOLDER,
        bot=("@" + settings.bot_username.lstrip("@")) if settings.bot_username else legal_texts.BOT_PLACEHOLDER,
        updated=settings.legal_updated_at,
        devices=3,
        locations=settings.locations_note,
        trial_days=settings.trial_days,
        trial_gb=settings.trial_gb,
        order_ttl_minutes=settings.order_ttl_minutes,
        prices=tuple(prices),
        privacy_url=settings.privacy_url,
        terms_url=settings.terms_url,
        pricing_url=settings.pricing_url,
    )


def render_document(key: str, context: LegalContext) -> str:
    if key == "privacy":
        return legal_texts.build_privacy(context)
    if key == "terms":
        return legal_texts.build_terms(context)
    return legal_texts.build_price_list(context)


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация документов в Telegra.ph")
    parser.add_argument("--dry-run", action="store_true", help="показать разметку без обращения к сети")
    parser.add_argument("--only", choices=sorted(DOCS), help="опубликовать только один документ")
    args = parser.parse_args()

    import asyncio

    settings = get_settings()
    if not settings.support_contact:
        print("⚠️  SUPPORT_USERNAME не заполнен: в документах останется плейсхолдер поддержки.")
    if not settings.legal_operator_name.strip():
        print("⚠️  Имя исполнителя не заполнено: в документах будет [ИСПОЛНИТЕЛЬ].")
    elif not settings.legal_operator_inn.strip():
        print("ℹ️  ИНН не указан: документы публикуются без упоминания НПД и чеков «Мой налог».")

    context = build_context(asyncio.run(load_prices()))
    keys = [args.only] if args.only else list(DOCS)

    if args.dry_run:
        for key in keys:
            title, _ = DOCS[key]
            nodes = to_nodes(render_document(key, context))
            print(f"\n=== {title}: узлов {len(nodes)} ===")
            print(json.dumps(nodes[:6], ensure_ascii=False, indent=2)[:1500])
        return 0

    author_name = legal_texts.SERVICE
    author_url = f"https://t.me/{settings.bot_username.lstrip('@')}" if settings.bot_username else ""
    env = read_env()
    token = ensure_account(env, author_name, author_url)
    state = load_state()

    urls: dict[str, str] = {}
    for key in keys:
        title, _ = DOCS[key]
        nodes = to_nodes(render_document(key, context))
        urls[key] = publish(token, key, title, nodes, author_name, author_url, state)

    write_env(**{ENV_KEYS[key]: url for key, url in urls.items()})
    print("\nСсылки записаны в .env:")
    for key, url in urls.items():
        print(f"  {ENV_KEYS[key]}={url}")
    print("\nДальше: перезапустить бота — кнопки в разделе «📄 Документы и цены» станут ссылками.")
    if not settings.legal_operator_name.strip():
        print(
            "\n⚠️  В документах остался плейсхолдер [ИСПОЛНИТЕЛЬ].\n"
            "   Заполните LEGAL_OPERATOR_NAME в .env и повторите этот же скрипт:\n"
            "   страницы обновятся, ссылки не изменятся."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
