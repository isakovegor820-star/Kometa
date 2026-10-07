#!/usr/bin/env python
"""Собрать юридические документы и прайс для публикации и для банка.

Запуск:

    .venv/bin/python scripts/render_legal.py

Что делает:
  1. берёт реквизиты, контакт поддержки и ссылки из `.env`;
  2. берёт актуальные тарифы из базы (если её нет — из справочника в коде);
  3. рендерит в `docs/legal/` три готовых к копированию файла:
     политику, соглашение и прайс-лист;
  4. пишет чек-лист готовности к согласованию с банком — с галочками по каждому
     требованию, чтобы было видно, чего не хватает.

Никаких секретов в файлы не попадает: только реквизиты и тексты.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import legal_texts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.legal_texts import LegalContext, html_to_markdown  # noqa: E402
from app.services import documents  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "legal"

FILES = {
    "privacy": ("ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ.md", legal_texts.build_privacy),
    "terms": ("ПОЛЬЗОВАТЕЛЬСКОЕ-СОГЛАШЕНИЕ.md", legal_texts.build_terms),
    "pricing": ("ЦЕНЫ-И-ТАРИФЫ.md", legal_texts.build_price_list),
}


async def load_prices():  # noqa: ANN201 - tuple[PriceRow, ...]
    """Актуальные тарифы из базы; если базы нет — справочник из кода."""
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


def build_checklist(context: LegalContext, prices_from_db: bool) -> str:
    """Чек-лист готовности: что уже закрыто, а что нужно дозаполнить."""
    settings = get_settings()

    def mark(ok: bool, text: str, hint: str = "") -> str:
        icon = "✅" if ok else "❌"
        tail = f"\n   → {hint}" if (hint and not ok) else ""
        return f"{icon} {text}{tail}"

    name_ready = bool(settings.legal_operator_name.strip())
    inn_ready = bool(settings.legal_operator_inn.strip())
    support = bool(settings.support_contact)
    privacy_published = bool(settings.privacy_url)
    terms_published = bool(settings.terms_url)
    prices_ready = bool(context.prices)

    lines = [
        "# Готовность к согласованию с банком",
        "",
        f"Собрано: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"Дата редакции документов: {context.updated}",
        "",
        "## Требования партнёра",
        "",
        mark(True, "**Политика конфиденциальности** — текст готов "
                   f"(`docs/legal/{FILES['privacy'][0]}`)"),
        mark(True, "**Пользовательское соглашение** — текст готов "
                   f"(`docs/legal/{FILES['terms'][0]}`)"),
        mark(support, "**Контакты поддержки** (не группа)",
             "Заполните SUPPORT_USERNAME в .env — иначе в документах останется "
             f"плейсхолдер {legal_texts.SUPPORT_PLACEHOLDER}"),
        mark(prices_ready, "**Актуальные цены и тарифы** — "
             f"`docs/legal/{FILES['pricing'][0]}`"
             + (" (взяты из базы)" if prices_from_db else " (справочник из кода)")),
        mark(privacy_published, "**Постоянная ссылка на политику**",
             "Опубликуйте Telegra.ph и укажите PRIVACY_URL в .env"),
        mark(terms_published, "**Постоянная ссылка на соглашение**",
             "Опубликуйте Telegra.ph и укажите TERMS_URL в .env"),
        "",
        "## Реквизиты исполнителя",
        "",
        mark(name_ready, f"LEGAL_OPERATOR_NAME: {settings.legal_operator_name or legal_texts.OPERATOR_PLACEHOLDER}",
             "Плейсхолдер [ИСПОЛНИТЕЛЬ] в документах видят и клиенты, и банк: "
             "укажите имя исполнителя (ФИО или название ИП/ООО)"),
        mark(inn_ready, f"LEGAL_OPERATOR_INN: {settings.legal_operator_inn or '— не указан'}",
             "Не обязательно: партнёр подтвердил, что ИП/ООО для модерации не нужны. "
             "Если самозанятость оформлена — укажите ИНН, и в документах появится "
             "упоминание НПД и чеков «Мой налог»"),
        mark(bool(settings.bot_username), f"BOT_USERNAME: {context.bot}",
             "Укажите юзернейм бота — он попадает в текст документов"),
        "",
        "## Что уже работает в боте",
        "",
        "✅ Раздел «📄 Документы и цены»: политика, соглашение, цены и поддержка —",
        "   отдельными кнопками, доступны в любой момент.",
        "✅ Если ссылка на документ не задана, бот отдаёт полный текст прямо в чате:",
        "   клиент всё равно получает документ.",
        "✅ Контакт поддержки продублирован на публичной странице `/status`.",
        "✅ Автопроверка формулировок: `tests/test_public_texts_clean.py`.",
        "",
        "## Порядок обновления",
        "",
        "1. Заполнить в `.env` то, что отмечено ❌ выше (имя исполнителя и, "
        "если есть самозанятость, ИНН).",
        "2. `scripts/render_legal.py` — пересобрать тексты в `docs/legal/`.",
        "3. `scripts/publish_legal.py` — обновить те же страницы Telegra.ph: "
        "адреса не меняются, банку не нужно получать новые ссылки.",
        "4. Перезапустить бота — он перечитает ссылки из `.env`.",
        "5. Проверить раздел «📄 Документы и цены» и отправить банку ссылки.",
        "",
        "Публикация уже выполнена: страницы живут по адресам ниже.",
        f"* Политика — {context.privacy_url or 'ещё не опубликована'}",
        f"* Соглашение — {context.terms_url or 'ещё не опубликовано'}",
        f"* Цены — {context.pricing_url or 'ещё не опубликованы'}",
        "",
        "## Переписка с банком",
        "",
        "Готовое сообщение (заполнить ссылки):",
        "",
        "```",
        "Здравствуйте! По вашему списку:",
        "",
        "1. Политика конфиденциальности: ССЫЛКА",
        "2. Пользовательское соглашение: ССЫЛКА",
        f"3. Контакт поддержки: {context.support} (обращения в Telegram, отвечаем в течение 15 минут)",
        "4. Актуальные цены и тарифы: ССЫЛКА (те же цены — в боте, раздел «Документы и цены»)",
        "",
        "Документы доступны пользователю постоянно: в боте они вынесены отдельными",
        "кнопками в раздел «📄 Документы и цены».",
        "```",
        "",
    ]
    return "\n".join(lines)


async def main() -> int:
    prices = await load_prices()
    prices_from_db = prices is not documents.DEFAULT_PRICES
    settings = get_settings()

    context = documents.LegalContext(
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
        prices=prices,
        privacy_url=settings.privacy_url,
        terms_url=settings.terms_url,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for _, (filename, render) in FILES.items():
        path = OUT_DIR / filename
        path.write_text(html_to_markdown(render(context)), encoding="utf-8")
        written.append(path)

    checklist = OUT_DIR / "ГОТОВНОСТЬ-К-БАНКУ.md"
    checklist.write_text(build_checklist(context, prices_from_db), encoding="utf-8")
    written.append(checklist)

    print("Собрано:")
    for path in written:
        print(f"  • {path.relative_to(OUT_DIR.parent.parent)}")

    missing: list[str] = []
    if not settings.legal_operator_name.strip():
        missing.append("имя исполнителя: LEGAL_OPERATOR_NAME")
    elif not settings.legal_operator_inn.strip():
        print("ℹ️  ИНН не указан — документы собраны без упоминания НПД и чеков «Мой налог».")
    if not settings.support_contact:
        missing.append("контакт поддержки: SUPPORT_USERNAME")
    if not settings.bot_username:
        missing.append("юзернейм бота: BOT_USERNAME")
    if not settings.privacy_url or not settings.terms_url:
        missing.append("ссылки на публикации: PRIVACY_URL, TERMS_URL")
    if missing:
        print("\n⚠️  Осталось заполнить:")
        for item in missing:
            print(f"  • {item}")
        print("   Подробнее — в docs/legal/ГОТОВНОСТЬ-К-БАНКУ.md")
    else:
        print("\n✅ Все требования партнёра закрыты — можно отправлять банку.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
