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
from app.panels.xui import XuiPanel


class PanelRegistry:
    """Ленивое создание и переиспользование клиентов панелей."""

    def __init__(self) -> None:
        self._cache: dict[str, PanelClient] = {}

    def primary(self) -> PanelClient:
        settings = get_settings()
        key = "primary"
        if key not in self._cache:
            self._cache[key] = self._build(
                panel_type=settings.panel_type,
                base_url=settings.panel_url,
                token=settings.panel_token,
                username=settings.panel_username,
                password=settings.panel_password,
                inbound_ids=settings.inbound_id_list,
                sub_base=settings.panel_sub_base,
            )
        return self._cache[key]

    def for_node(self, node: Node) -> PanelClient:
        key = f"node:{node.code}"
        if key not in self._cache:
            self._cache[key] = self._build(
                panel_type=node.panel_type,
                base_url=node.panel_url,
                token=node.panel_token,
                username="",
                password="",
                inbound_ids=[int(x) for x in node.inbound_ids.replace(" ", "").split(",") if x.strip().isdigit()],
                sub_base="",
            )
        return self._cache[key]

    async def all_panels(self, session: AsyncSession) -> list[PanelClient]:
        panels = [self.primary()]
        nodes = (await session.scalars(select(Node).where(Node.is_active.is_(True)).order_by(Node.priority))).all()
        for node in nodes:
            panels.append(self.for_node(node))
        return panels

    async def close(self) -> None:
        for panel in self._cache.values():
            await panel.close()
        self._cache.clear()

    # ------------------------------------------------------------------
    @staticmethod
    def _build(
        *,
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
            return XuiPanel(
                base_url=base_url,
                token=token,
                username=username,
                password=password,
                inbound_ids=inbound_ids,
                sub_base=sub_base,
            )
        return FakePanel()


registry = PanelRegistry()
