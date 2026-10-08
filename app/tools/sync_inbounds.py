"""Довести состав клиентов до настроек: досоздать их в «новых» инбаундах нод.

Зачем отдельный инструмент. ``create_user`` добавляет клиента во все настроенные
инбаунды ноды — но только в момент выдачи. Если набор ``inbound_ids`` расширили
позже (подключили резервный порт, добавили второй Reality), у клиентов, выданных
раньше, профиля на новом порту нет: в приложении у них на один конфиг меньше, и
запасной канал им недоступен. ``/sync`` в боте такую дырку не видит: он
проверяет, что клиент есть **на панели**, а не в каждом её инбаунде.

Именно это случилось 07-08.10.2026: из настроек нод выпал Reality-8443 —
14 клиентов на NL и 8 на FI остались без резервного порта.

Запуск на сервере (по умолчанию — только показывает, ничего не пишет)::

    .venv/bin/python -m app.tools.sync_inbounds            # dry-run
    .venv/bin/python -m app.tools.sync_inbounds --apply    # записать

Код возврата 1, если есть что досоздать (в dry-run) или если что-то не удалось.
Клиенты переносятся **как есть**: тот же uuid, subId, срок и лимиты — панель
получает уже существующий объект, новых подписок и дублей не появляется.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field

from sqlalchemy import select


def _as_dict(value: object) -> dict:
    """``settings`` панели приходит и строкой, и объектом."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def _clients_of(inbound: dict) -> list[dict]:
    clients = _as_dict(inbound.get("settings")).get("clients") or []
    return [item for item in clients if isinstance(item, dict) and item.get("email")]


def _protocol_of(inbounds: list[dict], email: str) -> str:
    """Протокол инбаунда, в котором уже лежит этот клиент (пусто — не нашли)."""
    for inbound in inbounds:
        if any(str(client.get("email")) == email for client in _clients_of(inbound)):
            return str(inbound.get("protocol") or "")
    return ""


def plan(inbounds: list[dict], configured: list[int], only: str = "") -> list[tuple[int, str, dict]]:
    """Что досоздать: ``[(id инбаунда, email, объект клиента), …]``.

    Правило: клиент, который уже есть на панели, должен быть и в каждом
    настроенном инбаунде **того же протокола**. Протокол сравнивать обязательно:
    у AmneziaWG своя форма клиента, и подставлять туда vless-объект нельзя.

    :param only: проверить механику на одном клиенте (email).

    Функция чистая — её и проверяют тесты.
    """
    by_email: dict[str, dict] = {}
    for inbound in inbounds:
        for client in _clients_of(inbound):
            by_email.setdefault(str(client["email"]), client)

    todo: list[tuple[int, str, dict]] = []
    for inbound in inbounds:
        inbound_id = int(inbound.get("id") or 0)
        if inbound_id not in configured:
            continue
        protocol = str(inbound.get("protocol") or "")
        here = {str(client.get("email")) for client in _clients_of(inbound)}
        for email, client in by_email.items():
            if email in here or (only and email != only):
                continue
            source = _protocol_of(inbounds, email)
            if source and source != protocol:
                continue
            todo.append((inbound_id, email, client))
    return todo


@dataclass(slots=True)
class PanelPlan:
    """Что предстоит сделать по одной панели."""

    code: str
    title: str
    missing: list[tuple[int, str]] = field(default_factory=list)
    error: str = ""


async def _load_nodes(session) -> list:  # noqa: ANN001 - AsyncSession, тип не тянем в CLI
    from app.db.models import Node

    return list(
        (
            await session.scalars(
                select(Node).where(Node.is_active.is_(True)).order_by(Node.id)
            )
        ).all()
    )


async def _scan(node, only: str = "") -> tuple[PanelPlan, list[tuple[int, str, dict]]]:  # noqa: ANN001
    """Панель ноды → план: что досоздать и чем (объекты клиентов как есть)."""
    from app.panels.base import parse_inbound_ids
    from app.panels.registry import registry

    item = PanelPlan(code=node.code, title=node.title or node.code)
    panel = registry.for_node(node)
    try:
        raw = list(await panel.raw_inbounds())
    except Exception as exc:  # noqa: BLE001 - панель отвечает чем угодно
        item.error = str(exc)
        return item, []

    todo = plan(raw, parse_inbound_ids(node.inbound_ids), only=only)
    item.missing = [(inbound_id, email) for inbound_id, email, _client in todo]
    return item, todo


async def collect(session=None, only: str = "") -> list[PanelPlan]:  # noqa: ANN001
    """Собрать план по всем активным нодам (панель спрашиваем как есть)."""
    from app.db.session import SessionMaker, init_db

    if session is None:
        await init_db()
        async with SessionMaker() as own_session:
            return await collect(own_session, only=only)

    plans: list[PanelPlan] = []
    for node in await _load_nodes(session):
        if (node.panel_type or "").lower() != "xui":
            continue
        item, _todo = await _scan(node, only=only)
        plans.append(item)
    return plans


async def apply_plan(session=None, only: str = "") -> list[tuple[str, int, str, str]]:  # noqa: ANN001
    """Досоздать всё, что нашлось. Возвращает ``[(код ноды, id, email, результат)]``."""
    from app.db.session import SessionMaker, init_db
    from app.panels.registry import registry

    if session is None:
        await init_db()
        async with SessionMaker() as own_session:
            return await apply_plan(own_session, only=only)

    results: list[tuple[str, int, str, str]] = []
    for node in await _load_nodes(session):
        if (node.panel_type or "").lower() != "xui":
            continue
        item, todo = await _scan(node, only=only)
        if item.error:
            results.append((node.code, 0, "", f"панель не ответила: {item.error}"))
            continue
        panel = registry.for_node(node)
        for inbound_id, email, client in todo:
            try:
                await panel.add_client_to_inbounds(client, [inbound_id])
            except Exception as exc:  # noqa: BLE001 - панель отвечает чем угодно
                results.append((node.code, inbound_id, email, f"ошибка: {exc}"))
                continue
            results.append((node.code, inbound_id, email, "добавлен"))
    return results


async def run(apply: bool = False, only: str = "") -> int:  # noqa: ANN001
    """Показать (и при ``apply`` — записать) недостающих. Возвращает код возврата."""
    plans = await collect(only=only)
    total = sum(len(item.missing) for item in plans)
    for item in plans:
        head = f"{item.title} ({item.code})"
        if item.error:
            print(f"{head}: ⚠️ {item.error}")
            continue
        if not item.missing:
            print(f"{head}: ✅ клиенты во всех настроенных инбаундах")
            continue
        by_inbound: dict[int, list[str]] = {}
        for inbound_id, email in item.missing:
            by_inbound.setdefault(inbound_id, []).append(email)
        for inbound_id, emails in sorted(by_inbound.items()):
            print(f"{head}: инбаунд {inbound_id} — нет {len(emails)}: {', '.join(emails)}")

    if not apply:
        print(f"\nК ДОБАВЛЕНИЮ (dry-run): {total}. Записать: --apply")
        return 1 if total else 0

    results = await apply_plan(only=only)
    for code, inbound_id, email, status in results:
        print(f"  {code}: инбаунд {inbound_id} ← {email}: {status}")
    failed = [row for row in results if row[3] != "добавлен"]
    print(f"\nДОБАВЛЕНО: {len(results) - len(failed)}. Ошибок: {len(failed)}")
    for row in failed:
        print("  ⚠️", row)
    return 1 if failed else 0


def main() -> int:  # pragma: no cover - ручной запуск
    parser = argparse.ArgumentParser(description="Досоздать клиентов в настроенных инбаундах нод")
    parser.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию dry-run)")
    parser.add_argument("--only", default="", help="один email клиента — проверить механику")
    args = parser.parse_args()

    from app.panels.registry import registry

    async def _run() -> int:
        try:
            return await run(apply=args.apply, only=args.only)
        finally:
            await registry.close()

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
