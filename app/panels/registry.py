"""Реестр панелей: собирает клиентов панелей из настроек и таблицы nodes.

MVP: одна панель из .env. При росте — панели нод из БД, а ссылка-подписка
объединяет конфиги всех активных нод.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Node
from app.panels.base import PanelClient
from app.panels.fake import FakePanel


class PanelRegistry:
    """Ленивое создание и переиспользование клиентов панелей."""

    def __init__(self) -> None:
        self._cache: dict[str, PanelClient] = {}

    def primary(self) -> PanelClient:
        settings = get_settings()
        key = "primary"
        if key not in self._cache:
            self._cache[key] = self._build(
                title=settings.location_title,
                panel_type=settings.panel_type,
                base_url=settings.panel_url,
                token=settings.panel_token,
                username=settings.panel_username,
                password=settings.panel_password,
                inbound_ids=settings.inbound_id_list,
                sub_base=settings.panel_sub_base,
            )
        return self._cache[key]

    def invalidate(self, code: str | None = None) -> None:
        """Сбросить кэш клиентов панелей.

        Нужно после правки ноды в админке: иначе останется старый клиент с
        прежним адресом/токеном, и подписка будет молча ходить не туда.
        """
        if code:
            self._cache.pop(f"node:{code}", None)
        else:
            self._cache.clear()

    def for_node(self, node: Node) -> PanelClient:
        key = f"node:{node.code}"
        if key not in self._cache:
            self._cache[key] = self._build(
                title=node.title,
                panel_type=node.panel_type,
                base_url=node.panel_url,
                token=node.panel_token,
                username="",
                password="",
                inbound_ids=[int(x) for x in node.inbound_ids.replace(" ", "").split(",") if x.strip().isdigit()],
                sub_base=node.sub_base or (f"http://{node.host}:2096/sub/" if node.host else ""),
            )
        return self._cache[key]

    async def all_panels(self, session: AsyncSession) -> list[PanelClient]:
        return [panel for _node, panel in await self.all_panels_with_nodes(session)]

    async def all_panels_with_nodes(
        self, session: AsyncSession
    ) -> list[tuple[Node | None, PanelClient]]:
        """Пары (нода, клиент панели): подписке нужен канал ноды.

        ``all_panels`` отдаёт только клиентов, и по ним нельзя понять, какую
        локацию помечать резервной — а от этого зависит группа автовыбора
        в подписке.
        """
        nodes = (await session.scalars(select(Node).where(Node.is_active.is_(True)).order_by(Node.priority))).all()
        pairs: list[tuple[Node | None, PanelClient]] = [(None, self.primary())]
        pairs.extend((node, self.for_node(node)) for node in nodes)
        return pairs

    async def close(self) -> None:
        for panel in self._cache.values():
            await panel.close()
        self._cache.clear()

    # ------------------------------------------------------------------
    @staticmethod
    def _build(
        *,
        title: str = "",
        panel_type: str,
        base_url: str,
        token: str,
        username: str,
        password: str,
        inbound_ids: list[int],
        sub_base: str,
    ) -> PanelClient:
        kind = (panel_type or "fake").lower()
        if kind == "xui":
            # импорт внутри ветки: без реальной панели модуль не обязателен
            from app.panels.xui import XuiPanel

            client: PanelClient = XuiPanel(
                base_url=base_url,
                token=token,
                username=username,
                password=password,
                inbound_ids=inbound_ids,
                sub_base=sub_base,
            )
        else:
            client = FakePanel()

        # Имя локации нужно подписке: у каждой страны оно своё («🇩🇪 Германия»,
        # «🇯🇵 Япония»), иначе клиент видит служебное имя инбаунда из панели.
        client.location_title = title
        return client


registry = PanelRegistry()
