"""Пинг ноды из терминала: TCP-соединение и, по желанию, TLS-рукопожатие.

Зачем отдельный инструмент: из админки видно только «панель отвечает», а
вопрос «дозвонится ли клиент и какой у него будет пинг» решается на порту
инбаунда. Тот же код, что в фоновой пробе, но запускается руками — удобно
проверить новую ноду до добавления в панель.

Примеры:

    .venv/bin/python -m app.tools.ping_node --host 150.241.106.75 --port 443
    .venv/bin/python -m app.tools.ping_node --host 1.2.3.4 --port 8443 --sni vpn.example.com --repeat 3
"""

from __future__ import annotations

import argparse
import asyncio
import statistics

from app.services.probe import probe_endpoint


async def run(host: str, port: int, sni: str, repeat: int, timeout: float) -> int:
    """Замерить задержку несколько раз и напечатать результат.

    :return: 0, если хотя бы одна попытка успешна, иначе 1 (удобно в скриптах).
    """
    results = []
    for attempt in range(1, repeat + 1):
        result = await probe_endpoint(host, port, sni=sni, timeout=timeout)
        results.append(result)
        mark = "✅" if result.ok else "❌"
        if result.ok:
            print(f"{mark} попытка {attempt}: {result.ms} мс ({result.detail})")
        else:
            print(f"{mark} попытка {attempt}: {result.stage} — {result.detail}")

    ok_ms = [r.ms for r in results if r.ok]
    if not ok_ms:
        print(f"\nИтог: {host}:{port} недоступен")
        return 1

    print(
        f"\nИтог: {host}:{port} доступен · "
        f"успешно {len(ok_ms)} из {len(results)} · "
        f"медиана {int(statistics.median(ok_ms))} мс · минимум {min(ok_ms)} мс"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Пинг ноды: TCP и TLS-рукопожатие с замером задержки.")
    parser.add_argument("--host", required=True, help="адрес ноды (IP или домен)")
    parser.add_argument("--port", type=int, required=True, help="порт инбаунда, например 443")
    parser.add_argument("--sni", default="", help="домен для TLS-рукопожатия (пусто — только TCP)")
    parser.add_argument("--repeat", type=int, default=1, help="сколько попыток сделать (по умолчанию 1)")
    parser.add_argument("--timeout", type=float, default=5.0, help="таймаут одной попытки, секунды")
    args = parser.parse_args(argv)

    return asyncio.run(run(args.host, args.port, args.sni, max(1, args.repeat), max(0.5, args.timeout)))


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
