"""Аудит подключения: почему пользователь не получает доступ (06.10.2026).

Разбор боевого случая: «пользователи не могут подключиться на 3 дня и
активировать подписку». Проверки ниже отвечают на два вопроса:

1. работает ли техническая цепочка «пробный доступ → клиент в панели →
      ссылка-подписка → конфиг с публичным адресом» на формах ответов настоящей
      панели 3x-ui (двойник ``tests/fake_xui.py``);
2. какие расхождения БД бота и панели ломают выдачу доступа — и что после
      починки пользователь всё-таки получает доступ.

Тесты с пометкой «известный разрыв» фиксируют текущее поведение: они зелёные
специально, чтобы при изменении кода было видно, что разрыв закрыли.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.bot import keyboards
from app.config import get_settings
from app.db.models import Order
from app.panels.registry import registry
from app.panels.xui import XuiPanel
from app.services import orders, subscriptions
from app.web.sub import build_app
from tests.fake_xui import DEFAULT_PUBLIC_HOST, FakeXuiServer
from tests.fakes import make_update
from tests.test_subscriptions import make_user

settings = get_settings()
GIB = 1024**3


# ---------------------------------------------------------------- фикстуры
@pytest.fixture
def xui_server() -> FakeXuiServer:
        """Панель 3x-ui в памяти — с ответами и маршрутами боевой 3.3.1."""
        return FakeXuiServer()


def build_panel(server: FakeXuiServer) -> XuiPanel:
        return XuiPanel(
                base_url=server.base_url,
                token="test-token",
                inbound_ids=[1],
                sub_base=server.sub_base,
                client=httpx.AsyncClient(transport=server.transport),
        )


@pytest.fixture
def xui_panel(xui_server: FakeXuiServer, monkeypatch) -> XuiPanel:
        """Панель XuiPanel, которую видят и хендлеры бота, и веб-слой."""
        panel = build_panel(xui_server)
        monkeypatch.setattr(registry, "primary", lambda: panel)

        async def fake_all_panels(session):  # noqa: ANN001 - сигнатура реестра
                return [panel]

        monkeypatch.setattr(registry, "all_panels", fake_all_panels)
        return panel


async def fetch_subscription_config(token: str, query: str = "?format=plain") -> httpx.Response:
        """Запросить у бота ссылку-подписку так, как это делает приложение клиента."""
        app = await build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(f"/sub/{token}{query}")


# ------------------------------------------------- 1. рабочая цепочка
async def test_trial_gives_three_days_and_live_config(bot, dispatcher, session, xui_server, xui_panel):
        """Пробный доступ: 3 дня, 10 ГБ, 1 устройство — и рабочий конфиг у клиента."""
        await dispatcher.feed_update(bot, make_update("/start", user_id=9400))
        bot.session.clear()
        await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9400))

        user = await subscriptions.get_user_by_tg(session, 9400)
        assert user is not None
        sub = await subscriptions.get_subscription(session, user.id)
        assert sub is not None and sub.status == "trial"
        assert sub.panel_user_uuid

        # В панель клиент ушёл в её собственных единицах: байты и миллисекунды.
        client = xui_server.clients[f"u{9400}"]
        assert client["totalGB"] == 10 * GIB
        assert client["limitIp"] == 1
        assert client["enable"] is True
        panel_expiry = datetime.fromtimestamp(client["expiryTime"] / 1000, tz=timezone.utc)
        assert abs((panel_expiry - datetime.now(timezone.utc)) - timedelta(days=3)) < timedelta(minutes=5)

        # Пользователю ушли и ссылка, и кнопки подключения в один тап.
        sent = bot.session.all_text()
        assert f"{settings.subscription_base}/{sub.subscription_token}" in sent
        urls = bot.session.button_urls()
        # Telegram пропускает только http(s): схемы приложений живут на странице.
        assert urls and all(url.startswith(("http://", "https://")) for url in urls)
        assert any(f"/connect/{sub.subscription_token}" in url for url in urls)

        # Ссылка-подписка отдаёт конфиг с публичным адресом ноды, а не с локальным.
        response = await fetch_subscription_config(sub.subscription_token)
        assert response.status_code == 200
        config = response.text.strip()
        assert f"@{DEFAULT_PUBLIC_HOST}:443" in config
        assert "127.0.0.1" not in config and "localhost" not in config


async def test_three_day_trial_matches_database_and_panel(session, xui_server, xui_panel):
        """Срок в БД бота равен сроку в панели: расхождение = «доступ кончился» на ровном месте."""
        user = await make_user(session, 9401)
        sub, granted = await subscriptions.start_trial(session, user, xui_panel)
        await session.commit()

        assert granted is True
        panel_expiry = datetime.fromtimestamp(
                xui_server.clients["u9401"]["expiryTime"] / 1000, tz=timezone.utc
        )
        assert abs(sub.expires_at - panel_expiry) < timedelta(seconds=5)
        assert abs((sub.expires_at - datetime.now(timezone.utc)) - timedelta(days=3)) < timedelta(minutes=5)


# ------------------------------------------------- 2. боевое состояние
async def test_trial_open_while_payments_closed(bot, dispatcher, session, xui_server, xui_panel, monkeypatch):
        """Боевое состояние после запуска Германии: триал открыт, оплата — позже.

        ``TRIAL_ENABLED=true`` + ``SALES_ENABLED=false``: люди получают 3 дня
        бесплатно, а платёжный путь показывает заглушку «скоро» и заказов не создаёт.
        """
        monkeypatch.setattr(settings, "sales_enabled", False)
        monkeypatch.setattr(settings, "trial_enabled", True)

        await dispatcher.feed_update(bot, make_update("/start", user_id=9410))
        bot.session.clear()
        await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9410))

        user = await subscriptions.get_user_by_tg(session, 9410)
        sub = await subscriptions.get_subscription(session, user.id)
        assert sub is not None, f"пробный доступ не выдан: {bot.session.all_text()}"
        assert sub.status == "trial"
        assert "u9410" in xui_server.clients
        assert "/sub/" in bot.session.all_text()

        # Продажи при этом закрыты: заглушка вместо счёта, заказов нет.
        bot.session.clear()
        await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9410))
        assert "ближайшие дни" in bot.session.all_text()

        bot.session.clear()
        await dispatcher.feed_update(bot, make_update(callback_data="pay:1:stars", user_id=9410))
        assert "Оплата по СБП — скоро" in bot.session.all_text()
        assert await session.scalar(select(func.count()).select_from(Order)) == 0


async def test_trial_disabled_shows_its_own_stub(bot, dispatcher, session, xui_server, xui_panel, monkeypatch):
        """Обратный флаг: TRIAL_ENABLED=false закрывает только пробный доступ."""
        monkeypatch.setattr(settings, "trial_enabled", False)

        await dispatcher.feed_update(bot, make_update("/start", user_id=9411))
        bot.session.clear()
        await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9411))

        assert "Пробный доступ ещё не открыт" in bot.session.all_text()
        user = await subscriptions.get_user_by_tg(session, 9411)
        assert await subscriptions.get_subscription(session, user.id) is None
        assert xui_server.clients == {}


# ------------------------------------------------- 3. развилки БД ↔ панель
async def test_trial_recovers_when_panel_already_has_the_client(
        bot, dispatcher, session, xui_server, xui_panel
):
        """БД бота потеряли, а клиент в панели остался.

        До починки: ``create_user`` упирался в «Duplicate email», пользователь
        получал «Сервис временно недоступен» и не мог получить свои 3 дня —
        второй раз панель клиента не создаёт.
        """
        existing_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        xui_server.add_client(
                email="u9420", uuid=existing_uuid, sub_id="existingclient01", days_left=1
        )

        await dispatcher.feed_update(bot, make_update("/start", user_id=9420))
        bot.session.clear()
        await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9420))

        user = await subscriptions.get_user_by_tg(session, 9420)
        sub = await subscriptions.get_subscription(session, user.id)
        assert sub is not None, f"пробный доступ не выдан: {bot.session.all_text()}"
        assert sub.panel_user_uuid == existing_uuid, "клиент панели не переиспользован"
        assert list(xui_server.clients) == ["u9420"], "в панели появился дубль клиента"

        # Срок именно продлён: было 1 день, стало 1 + 3.
        panel_expiry = datetime.fromtimestamp(
                xui_server.clients["u9420"]["expiryTime"] / 1000, tz=timezone.utc
        )
        assert panel_expiry - datetime.now(timezone.utc) > timedelta(days=3)
        assert "/sub/" in bot.session.all_text()


async def test_paid_activation_recreates_client_lost_in_panel(session, xui_server, xui_panel):
        """Оплата прошла, а клиента в панели уже нет (удалён руками, потерян при переносе).

        До починки: ``update_user`` падал с «пользователь не найден в панели», заказ
        оставался невыданным — деньги приняты, доступа нет.
        """
        user = await make_user(session, 9430)
        await subscriptions.start_trial(session, user, xui_panel)
        await session.commit()
        xui_server.remove_client("u9430")

        plan = next(plan for plan in await orders.list_plans(session) if plan.code == "m1")
        sub = await subscriptions.activate_plan(session, user, plan, xui_panel)
        await session.commit()

        assert sub.status == "active"
        assert "u9430" in xui_server.clients, "оплаченный доступ не выдан"
        assert sub.panel_user_uuid == xui_server.clients["u9430"]["id"]
        # Месяц отсчитывается от выдачи: клиент создан заново, старого срока нет.
        assert sub.expires_at > datetime.now(timezone.utc) + timedelta(days=29)


async def test_paid_activation_reuses_client_unknown_to_bot(session, xui_server, xui_panel):
        """Обратный случай: у бота записи нет, панель клиента знает — продлеваем его, а не плодим дубль."""
        xui_server.add_client(
                email="u9431", uuid="11111111-1111-1111-1111-111111111111", sub_id="panelonlyclient1", days_left=2
        )
        user = await make_user(session, 9431)

        plan = next(plan for plan in await orders.list_plans(session) if plan.code == "m1")
        sub = await subscriptions.activate_plan(session, user, plan, xui_panel)
        await session.commit()

        assert sub.status == "active"
        assert list(xui_server.clients) == ["u9431"]
        assert sub.panel_user_uuid == "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------- 4. доставка конфига
async def test_config_host_is_taken_from_panel_share_addr(session, xui_server, xui_panel):
        """Адрес в конфиге — ровно то, что панель положила в ``shareAddr``.

        Известный разрыв: бот адрес не проверяет. Если в панели не задан публичный
        адрес инбаунда, 3x-ui подставит адрес запроса (127.0.0.1) — все ответы при
        этом 200 OK, а у людей «не подключается». Проверка адреса — в preflight.
        """
        loopback = FakeXuiServer(public_host="127.0.0.1")
        panel = build_panel(loopback)
        client = loopback.add_client(email="u9440", sub_id="loopbackclient01")

        configs = await panel.get_configs(client["id"])

        assert configs and "@127.0.0.1:443" in configs[0]


async def test_expired_subscription_still_serves_configs_known_gap(
        session, xui_server, xui_panel
):
        """Известный разрыв: истёкшая подписка продолжает отдавать конфиг.

        Клиента в панели выключают (``disable_expired``), но ``/sub/<token>``
        отвечает 200 и конфигом: в приложении профиль выглядит рабочим, а
        подключения нет. Сейчас это осознанное поведение (ссылка одна и та же,
        после оплаты она снова рабочая), но в поддержку это приходит как
        «не подключается».
        """
        user = await make_user(session, 9450)
        sub, _ = await subscriptions.start_trial(session, user, xui_panel)
        await session.commit()

        sub.status = "expired"
        await session.commit()

        response = await fetch_subscription_config(sub.subscription_token)
        assert response.status_code == 200
        assert "vless://" in response.text


# ------------------------------------------------- 5. кнопки подключения
def test_connect_buttons_lead_to_our_page_not_to_app_scheme():
        """Схемы приложений в inline-кнопках Telegram запрещены — ведём на страницу.

        Проверено на живом боте: кнопка с ``happ://`` валит отправку сообщения
        («Bad Request: Unsupported URL protocol»), поэтому кнопки — обычные https.
        """
        sub_url = "https://vpn.example/sub/abc123"
        urls = {
                button.text: button.url
                for row in keyboards.connect_kb(sub_url).inline_keyboard
                for button in row
                if getattr(button, "url", None)
        }

        assert set(urls) == {
                "Подключить в Happ",
                "Подключить в v2rayNG",
                "Подключить в Hiddify",
        }
        for url in urls.values():
                assert url.startswith(("http://", "https://"))
                assert "/connect/abc123" in url
