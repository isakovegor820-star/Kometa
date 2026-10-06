#!/usr/bin/env python3
"""Макеты админ-панели для Open Design: живые страницы на демо-данных → PNG.

Зачем так, а не «нарисовать картинки в редакторе»:

* страницы берутся из работающего приложения — те же шаблоны, тот же CSS,
  те же данные из БД. Дизайн физически не может разойтись с реализацией;
* если шаблон сломается, сломается и макет — это ещё и дымовой тест страниц.

Как работает:

1. поднимает временную SQLite-базу и наполняет её реалистичными данными
   (клиенты, оплаты, возвраты, ноды, алерты, промокоды, рассылки, команда);
2. запрашивает страницы панели через ASGI-транспорт httpx — без сети и браузера;
3. вкладывает CSS/JS внутрь HTML (артефакт самодостаточен) и сохраняет
   в ``design/screens/``;
4. по флагу ``--render`` просит Open Design отрендерить PNG в ``design/renders/``;
   по флагу ``--lint`` — прогоняет анти-слоп линтер Open Design.

Запуск::

    python3 design/build.py                     # только HTML
    python3 design/build.py --render            # HTML + PNG
    python3 design/build.py --render --lint     # ещё и линтер
    python3 design/build.py --only orders       # одна страница
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))

SCREENS = WORKSPACE / "design" / "screens"
RENDERS = WORKSPACE / "design" / "renders"
STATIC = WORKSPACE / "app" / "web" / "static"

OD_APP = Path("/Applications/Open Design.app/Contents/Resources/app")
OD_CLI = OD_APP / "prebundled" / "daemon" / "daemon-cli.mjs"
OD_DATA = Path.home() / "Library/Application Support/Open Design/namespaces/release-stable/data"
OD_PROJECT = "kometa-admin"
OD_DAEMON = "http://127.0.0.1:65445"

DEMO_PASSWORD = "demo-panel-password"
DEMO_DB = Path(tempfile.gettempdir()) / "kometa-design-demo.db"

#: Страницы, которые попадают в макеты: путь → имя файла.
PAGES: dict[str, str] = {
    "/admin": "dashboard",
    "/admin/orders?status=all": "orders",
    "/admin/users": "users",
    # Первый клиент демо-базы: карточка с подпиской, заказами, событиями и заметками.
    "/admin/users/1": "user-card",
    "/admin/refunds": "refunds",
    "/admin/finance": "finance",
    "/admin/nodes": "nodes",
    "/admin/alerts": "alerts",
    "/admin/audit": "audit",
    "/admin/referrals": "referrals",
    "/admin/plans": "plans",
    "/admin/broadcast": "broadcast",
    "/admin/team": "team",
}

#: Дополнительные варианты тех же страниц (нужны обе темы и вид с телефона).
VARIANTS: dict[str, tuple[str, dict]] = {
    "dashboard-dark": ("/admin", {"dark": True}),
    "dashboard-mobile": ("/admin", {"mobile": True}),
}


def prepare_env() -> None:
    """Настройки ДО импорта приложения: движок БД создаётся на уровне модуля."""
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    os.environ["DB_URL"] = f"sqlite+aiosqlite:///{DEMO_DB}"
    os.environ.setdefault("BOT_TOKEN", "000000:DEMO_TOKEN")
    os.environ["PANEL_TYPE"] = "fake"
    os.environ["ADMIN_IDS"] = "1"
    os.environ["ADMIN_PANEL_PASSWORD"] = DEMO_PASSWORD
    os.environ["ADMIN_PANEL_SECRET"] = "design-demo-secret"
    os.environ["ADMIN_LOCAL_ONLY"] = "true"
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8080"
    os.environ["MANUAL_PAYMENT_DETAILS"] = "СБП: +7 900 000-00-00"
    os.environ["SALES_ENABLED"] = "true"
    os.environ["AUTOPAY_ENABLED"] = "true"
    os.environ["STARS_ENABLED"] = "true"
    os.environ["MONTHLY_COSTS_RUB"] = "24900"


async def seed() -> None:
    """Наполнить демо-базу так, как выглядит живой сервис на несколько сотен клиентов."""
    from sqlalchemy import select

    from app.db.models import AdminAccount, Broadcast, Event, Node, Plan, UserNote
    from app.db.session import SessionMaker, init_db, seed_plans
    from app.panels.registry import registry
    from app.services import (
        alerts as alerts_service,
        events,
        orders as orders_service,
        promo,
        referral,
        subscriptions,
    )
    from app.web.security import hash_password

    await init_db()
    await seed_plans()

    now = datetime.now(timezone.utc)
    panel = registry.primary()

    people = [
        ("Анна Ковалёва", "anna_k", 473829104, "active", "vip"),
        ("Дмитрий Соколов", "dsokolov", 811223344, "trial", ""),
        ("Ирина Петрова", "irina.p", 552341987, "active", ""),
        ("Максим Орлов", None, 900112233, "expired", "шеринг"),
        ("Ольга Смирнова", "olga_s", 128374651, "blocked", "конфликт"),
        ("Сергей Волков", "svolkov", 745213098, "active", ""),
        ("Пётр Лебедев", "petr", 661029384, "trial", ""),
        ("Мария Зайцева", "mzaytseva", 333222111, "active", "vip"),
    ]

    async with SessionMaker() as session:
        plans = list((await session.scalars(select(Plan).order_by(Plan.sort_order))).all())
        created: list = []

        for index, (name, username, tg_id, status, tag) in enumerate(people):
            user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=username)
            user.first_name = name
            user.tags = tag
            user.created_at = now - timedelta(days=30 + index * 9)
            user.bonus_days_balance = index % 3
            created.append(user)

            if status == "trial":
                await subscriptions.start_trial(session, user, panel)
            elif status in {"active", "blocked", "expired"}:
                plan = plans[index % len(plans)]
                order = await orders_service.create_order(session, user, plan, provider="manual")
                await orders_service.mark_paid(session, order, panel)
                sub = await subscriptions.get_subscription(session, user.id)
                if sub is not None and status == "expired":
                    sub.expires_at = now - timedelta(days=3)
                    sub.status = "expired"
                if sub is not None and status == "blocked":
                    await subscriptions.set_enabled(sub, panel, False)

        # История оплат за месяц: без неё график выручки выглядит одним столбиком.
        for index in range(38):
            user = created[index % len(created)]
            plan = plans[index % len(plans)]
            order = await orders_service.create_order(session, user, plan, provider="manual")
            await orders_service.mark_paid(session, order, panel)
            paid_at = now - timedelta(days=index % 30, hours=(index * 5) % 20, minutes=index * 3 % 60)
            order.paid_at = paid_at
            order.created_at = paid_at - timedelta(minutes=25)

        # Очередь оплат — то, что модератор видит утром.
        for index, user in enumerate(created[:4]):
            await orders_service.create_order(
                session, user, plans[index % len(plans)], provider="platega_sbp" if index == 1 else "manual"
            )

        # Возврат: деньги вернули, доступ закрыли.
        refund_order = await orders_service.create_order(session, created[4], plans[2], provider="stars")
        await orders_service.mark_paid(session, refund_order, panel)
        await orders_service.refund_order(
            session, refund_order, [panel], actor="владелец", note="чарджбэк от платёжного канала"
        )

        # Отменённый и просроченный заказы — чтобы у вкладок были данные.
        canceled = await orders_service.create_order(session, created[5], plans[0], provider="manual")
        await orders_service.cancel_order(session, canceled, reason="клиент оформил повторно")
        stale = await orders_service.create_order(session, created[6], plans[1], provider="manual")
        stale.status = "expired"

        # Промокоды: акционный и «блогерский».
        await promo.create_admin_code(session, "LAUNCH50", percent=50, uses_limit=100, days=30, note="запуск")
        await promo.create_admin_code(session, "BLOGGER30", percent=30, uses_limit=0, days=0, note="блогер")
        await referral.attach_referrer(session, created[1], created[0].referral_code)

        # Ноды: живая, упавшая и выключенная.
        session.add_all(
            [
                Node(
                    code="de", title="🇩🇪 Германия", country="DE", host="150.241.106.75", panel_type="fake",
                    panel_url="http://150.241.106.75:2053/panel", panel_token="demo-token-de",
                    inbound_ids="1,2", priority=10, is_active=True,
                    last_check_at=now - timedelta(minutes=4), last_check_ok=True,
                ),
                Node(
                    code="nl", title="🇳🇱 Нидерланды", country="NL", host="203.0.113.44", panel_type="fake",
                    panel_url="http://203.0.113.44:2053/panel", panel_token="demo-token-nl",
                    inbound_ids="3", priority=20, is_active=True,
                    last_check_at=now - timedelta(minutes=4), last_check_ok=False,
                ),
                Node(
                    code="jp", title="🇯🇵 Япония", country="JP", host="198.51.100.9", panel_type="fake",
                    panel_url="http://198.51.100.9:2053/panel", panel_token="demo-token-jp",
                    inbound_ids="1", priority=30, is_active=False,
                    last_check_at=now - timedelta(days=2), last_check_ok=True,
                ),
            ]
        )

        # Алерты: то, что панель показывает вместо сообщений в Telegram.
        await alerts_service.raise_alert(
            session, "node_down", "Нода «🇳🇱 Нидерланды» не отвечает", severity="err",
            message="панель не ответила на проверку: ConnectTimeout", fingerprint="node:nl",
        )
        await alerts_service.raise_alert(
            session, "payment_unmatched", "Поступление 349 ₽ без заказа", severity="warn",
            message="перевод 06.10 18:12, комментарий пустой — сопоставь вручную", fingerprint="payment:349",
        )
        await alerts_service.raise_alert(
            session, "node_degraded", "Нода «🇩🇪 Германия»: нет инбаундов", severity="warn",
            message="панель отвечает, но инбаундов нет — клиенты не получат конфиг",
            fingerprint="node:de:inbounds",
        )

        # Заметки поддержки: контекст не теряется между сменами.
        session.add_all(
            [
                UserNote(
                    user_id=created[0].id, author="модератор", author_role="moderator",
                    text="Писал в поддержку: не подключался Владивосток 2 дня. Начислили 3 дня компенсации.",
                    created_at=now - timedelta(hours=5),
                ),
                UserNote(
                    user_id=created[0].id, author="владелец", author_role="owner",
                    text="Просил счёт для бухгалтерии — отправили выписку по заказам.",
                    created_at=now - timedelta(days=3),
                ),
            ]
        )

        # Команда: владелец входит паролем из .env, модератор и поддержка — своими.
        session.add_all(
            [
                AdminAccount(
                    login="moderator", display_name="Кирилл (модератор)", role="moderator",
                    password_hash=hash_password("moder-pass-demo"), tg_id=2,
                    last_login_at=now - timedelta(hours=1),
                ),
                AdminAccount(
                    login="support", display_name="Лена (поддержка)", role="support",
                    password_hash=hash_password("support-pass-demo"), tg_id=3,
                    last_login_at=now - timedelta(hours=9),
                ),
            ]
        )

        # Рассылки: завершённая и идущая.
        session.add_all(
            [
                Broadcast(
                    text="Обновили приложение? Расскажи, как работает связь — нам важна обратная связь.",
                    audience="active", status="done", total=283, sent=279, failed=4,
                    created_by="владелец", created_at=now - timedelta(days=2),
                    finished_at=now - timedelta(days=2) + timedelta(minutes=18),
                ),
                Broadcast(
                    text="Добавили локацию Япония — переключить можно в приложении.",
                    audience="active", status="running", total=291, sent=143, failed=1,
                    created_by="модератор", created_at=now - timedelta(minutes=12),
                ),
            ]
        )

        # Действия команды: журнал не должен быть пустым.
        for kind, actor, role, payload in (
            ("admin.order_confirm", "владелец", "owner", {"order_id": 4101, "amount": 499}),
            ("admin.grant_days", "модератор", "moderator", {"days": 7, "reason": "компенсация"}),
            ("admin.order_refund", "владелец", "owner", {"order_id": 4098, "amount": 890}),
            ("admin.node_toggle", "владелец", "owner", {"node": "nl", "active": False}),
            ("admin.promo_created", "модератор", "moderator", {"code": "LAUNCH50"}),
        ):
            event = Event(kind=kind, payload=json.dumps(payload, ensure_ascii=False))
            event.actor_name = actor
            event.actor_role = role
            event.source = "web"
            event.ip = "127.0.0.1"
            # Разносим действия по времени: в макете журнал должен выглядеть
            # как история смены, а не как список из одной секунды.
            event.created_at = now - timedelta(hours=len(session.new) * 3 + 1)
            session.add(event)

        await events.log_event(session, events.ORDER_PAID, user_id=created[0].id, payload={"order_id": 4099})
        await session.commit()


async def capture(only: str = "") -> list[tuple[str, str]]:
    """Забрать HTML страниц панели. Возвращает [(имя, html)]."""
    import httpx

    from app.web.sub import build_app

    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    captured: list[tuple[str, str]] = []

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as client:
        login = await client.post("/admin/login", data={"password": DEMO_PASSWORD})
        assert login.status_code == 303, f"вход в демо-панель не удался: {login.status_code}"

        targets = {**{name: (url, {}) for url, name in PAGES.items()}, **VARIANTS}
        for name, (url, options) in targets.items():
            if only and name != only:
                continue
            response = await client.get(url)
            assert response.status_code == 200, f"{url} → {response.status_code}"
            captured.append((name, postprocess(response.text, **options)))

        if not only or only == "login":
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
                page = await anon.get("/admin/login")
                captured.append(("login", postprocess(page.text)))

    return captured


def postprocess(html: str, *, dark: bool = False, mobile: bool = False) -> str:
    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    js = (STATIC / "admin.js").read_text(encoding="utf-8")

    html = html.replace('<link rel="stylesheet" href="/admin/static/admin.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<script src="/admin/static/admin.js" defer></script>', f"<script>\n{js}\n</script>")

    # Снимок делается по всей высоте страницы: липкие блоки в таком рендере
    # дублируются на каждом экране, поэтому в макете они обычные.
    html = html.replace(
        "</style>",
        "</style>\n<style>\n/* только для статичного снимка */\n"
        ".sidebar{position:static!important;height:auto!important}\n"
        ".topbar{position:static!important}\n"
        "table.data th{position:static!important}\n"
        ".bulkbar{position:static!important}\n"
        "</style>",
        1,
    )

    if dark:
        html = html.replace('<html lang="ru" data-theme="light">', '<html lang="ru" data-theme="dark">')
        html = html.replace("applyTheme(currentTheme());", 'applyTheme("dark");')

    if mobile:
        unwrapped = _unwrap_media(css, 1080) + "\n" + _unwrap_media(css, 640)
        html = html.replace(
            "</style>\n<style>\n/* только для статичного снимка */",
            "</style>\n<style>\n"
            "html{background:var(--surface-3)}\n"
            "body{width:414px;margin:0 auto;min-height:100vh;box-shadow:0 0 0 1px var(--border)}\n"
            ".sidebar{display:none}\n"
            ".grid-2 > div:nth-child(2){display:none}\n"
            f"{unwrapped}\n"
            "</style>\n<style>\n/* только для статичного снимка */",
            1,
        )
    return html


def _unwrap_media(css: str, max_width: int) -> str:
    """Вынуть правила из @media (max-width: N) и вернуть их безусловными.

    Нужно для макета телефона: Open Design рендерит страницу в широком окне,
    поэтому мобильные медиазапросы в снимке не срабатывают.
    """
    marker = f"@media (max-width: {max_width}px)"
    start = css.find(marker)
    if start == -1:
        return ""
    brace = css.find("{", start)
    depth = 0
    end = brace
    for index in range(brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    return css[brace + 1 : end]


def export_png(paths: list[Path], lint: bool) -> int:
    if not OD_CLI.exists():
        print(f"не найден Open Design CLI: {OD_CLI}", file=sys.stderr)
        return 1

    project_dir = OD_DATA / "projects" / OD_PROJECT
    project_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.copy(path, project_dir / path.name)

    failures = 0
    for path in paths:
        out = RENDERS / f"{path.stem}.png"
        result = subprocess.run(
            [
                "node", str(OD_CLI), "export", path.name,
                "--project", OD_PROJECT,
                "--format", "image", "--image-format", "png",
                "--out", str(out),
                "--daemon-url", OD_DAEMON, "--json",
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            failures += 1
            print(f"ПРОБЛЕМА {path.stem}: {(result.stderr or result.stdout).strip()[:300]}")
        else:
            print(f"PNG: {out.relative_to(WORKSPACE)}")

    if lint:
        for path in paths:
            result = subprocess.run(
                ["node", str(OD_CLI), "lint", str(path), "--json", "--daemon-url", OD_DAEMON],
                capture_output=True, text=True, timeout=180,
            )
            try:
                report = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                report = {"raw": (result.stdout or result.stderr)[:200]}
            findings = report.get("findings") or report.get("issues") or []
            count = len(findings) if isinstance(findings, list) else findings
            print(f"линтер {path.stem}: {count}")
            if isinstance(findings, list):
                for item in findings[:4]:
                    rule = item.get("rule") or item.get("code") or item.get("id")
                    message = item.get("message") or item.get("detail") or ""
                    print(f"    · {rule}: {message}"[:200])
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать макеты админ-панели")
    parser.add_argument("--render", action="store_true", help="отрендерить PNG через Open Design")
    parser.add_argument("--lint", action="store_true", help="прогнать анти-слоп линтер Open Design")
    parser.add_argument("--only", default="", help="собрать только одну страницу по имени")
    args = parser.parse_args()

    prepare_env()
    asyncio.run(seed())

    SCREENS.mkdir(parents=True, exist_ok=True)
    RENDERS.mkdir(parents=True, exist_ok=True)

    captured = asyncio.run(capture(args.only))
    paths: list[Path] = []
    for name, html in captured:
        target = SCREENS / f"{name}.html"
        target.write_text(html, encoding="utf-8")
        paths.append(target)
        print(f"экран: {target.relative_to(WORKSPACE)}  ({len(html) // 1024} КБ)")

    if not paths:
        print(f"нет страниц для сборки (--only {args.only})", file=sys.stderr)
        return 1
    if not args.render:
        print("\nготово. PNG: добавь --render (нужен запущенный Open Design)")
        return 0
    return export_png(paths, args.lint)


if __name__ == "__main__":
    raise SystemExit(main())
