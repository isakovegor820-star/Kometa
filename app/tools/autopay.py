"""CLI: разовый прогон автопроверки платежей.

Зачем: посмотреть, что система видит в выписке, не дожидаясь планировщика
и не запуская бота целиком. Удобно для отладки и для cron-задачи.

Запуск:
    .venv/bin/python -m app.tools.autopay            # один прогон
    .venv/bin/python -m app.tools.autopay --dry-run  # только показать, что нашли
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import get_settings
from app.db.session import SessionMaker, init_db
from app.panels.registry import registry
from app.services import autopay


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Разовая проверка поступлений по выписке")
    parser.add_argument("--dry-run", action="store_true", help="ничего не подтверждать, только показать")
    parser.add_argument("--verbose", action="store_true", help="подробные логи")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s | %(message)s")

    settings = get_settings()
    if not args.dry_run and not settings.autopay_enabled:
        print("Автоплатёж выключен: поставь AUTOPAY_ENABLED=true в .env (или запусти с --dry-run)")
        return 1

    await init_db()
    sources = autopay.build_statement_sources()
    if not sources:
        print(
            "Источники выписки не настроены.\n"
            "Заполни STATEMENT_CSV_GLOB (файлы выписки) или BANK_IMAP_* (почта банка) в .env."
        )
        return 1

    print(f"Источники: {', '.join(source.name for source in sources)}")
    if args.dry_run:
        # В dry-run выключаем подтверждение, но оставляем чтение
        settings.autopay_enabled = True
        settings_copy = settings.statement_state_file
        settings.statement_state_file = "/tmp/kometa-autopay-dryrun.json"
        print("Режим проверки: заказы не подтверждаются, состояние не сохраняется в проект")

    async with SessionMaker() as session:
        result = await autopay.reconcile(session, registry.primary(), bot=None, sources=sources)
        if not args.dry_run:
            await session.commit()

    print("\nИтог:", result.as_text())
    for payment in result.unmatched:
        print(f"  ⚠️  {payment.amount_kopecks / 100:.2f} ₽ — {payment.comment or 'без комментария'} ({payment.source})")
    for error in result.errors:
        print(f"  ❌ {error}")

    if args.dry_run:
        settings.statement_state_file = settings_copy

    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
