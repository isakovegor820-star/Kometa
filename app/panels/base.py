"""Абстракция панели управления VPN.

Наш код не знает деталей конкретной панели: он работает через PanelClient.
Это позволяет заменить 3x-ui на Remnawave без переписывания бота.

Реализации:
  * app/panels/fake.py — локальная заглушка (разработка и тесты);
  * app/panels/xui.py  — реальная панель 3x-ui.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class PanelError(RuntimeError):
    """Ошибка при обращении к панели (сеть, авторизация, валидация)."""


class PanelInboundMissing(PanelError):
    """Панель ответила, но выдать конфиг нечем: инбаундов нет или нужных ID нет.

    Отдельный класс — потому что это **не** «панель сломалась»: авторизация
    прошла, список получен, дело в настройке (``inbound_ids`` разошлись с
    панелью). У этого состояния другой текст алерта и другое действие
    оператора, поэтому вызывающий код обязан различать его от ``PanelError``.
    """


@dataclass(slots=True)
class Inbound:
    """Входящее подключение (инбаунд) в панели."""

    id: int
    remark: str
    protocol: str
    port: int
    network: str = ""
    security: str = ""


@dataclass(slots=True)
class UserSpec:
    """Параметры создаваемого пользователя."""

    email: str
    days: int
    traffic_gb: int = 0  # 0 = безлимит
    devices: int = 3
    note: str = ""
    #: UUID клиента. Пусто — панель сгенерирует сама. Один и тот же uuid на всех
    #: панелях нужен, чтобы ссылка-подписка собирала конфиги из всех стран.
    uuid: str = ""


@dataclass(slots=True)
class PanelUser:
    """Состояние пользователя в панели."""

    uuid: str
    email: str
    enabled: bool = True
    expires_at: datetime | None = None
    traffic_limit_bytes: int = 0  # 0 = безлимит
    devices_limit: int = 0
    used_bytes: int = 0
    subscription_url: str = ""
    raw: dict = field(default_factory=dict)


class PanelClient(ABC):
    """Единый интерфейс панели."""

    name: str = "base"
    #: Человеческое имя локации для подписки («🇩🇪 Германия»). Заполняет реестр:
    #: у основной панели — LOCATION_TITLE, у ноды — её название из админки.
    location_title: str = ""

    @abstractmethod
    async def health(self) -> bool:
        """Панель отвечает и авторизация проходит."""

    async def check_health(self) -> tuple[bool, str]:
        """Панель отвечает? Возвращает ``(ok, текст ошибки)``.

        Нужен отдельно от :meth:`health`: упавшая проверка должна объяснять
        причину, иначе в алерте остаётся «панель не ответила на проверку» — по
        такому тексту не отличить таймаут от 401 и от «нет маршрута», хотя
        исключение с текстом было секундой раньше.
        """
        try:
            return await self.health(), ""
        except Exception as exc:  # noqa: BLE001 - чужая панель отвечает чем угодно
            return False, str(exc)

    @abstractmethod
    async def list_inbounds(self) -> list[Inbound]:
        """Список инбаундов, из которых собирается подписка."""

    async def list_all_inbounds(self) -> list[Inbound]:
        """Все инбаунды панели, без фильтра по ``inbound_ids``.

        Нужен админке и диагностике: когда настроенные ID разошлись с панелью,
        фильтрованный список падает, и показать оператору фактические ID
        больше неоткуда. По умолчанию совпадает с :meth:`list_inbounds` —
        панели, которым нечего фильтровать, ничего не переопределяют.
        """
        return await self.list_inbounds()

    async def list_users(self) -> list[PanelUser]:
        """Все клиенты целевых инбаундов.

        Нужен для аудита: раз в сутки сверяем, кто вообще сидит на сервере,
        сколько скачал за день и нет ли «вечных» доступов без срока действия.
        Панели, которые не умеют отдавать список, возвращают пустой список.
        """
        return []

    @abstractmethod
    async def create_user(self, spec: UserSpec) -> PanelUser:
        """Создать пользователя с заданным сроком/лимитами."""

    @abstractmethod
    async def get_user(self, uuid: str) -> PanelUser | None:
        """Получить состояние пользователя или None, если его нет."""

    async def find_user_by_email(self, email: str) -> PanelUser | None:
        """Найти пользователя по логину (email).

        Нужен для восстановления: если панель уже знает такого пользователя
        (например, БД бота восстановили из бэкапа), мы не должны терять
        оплаченный доступ — находим его и продлеваем.
        По умолчанию панель может не поддерживать поиск.
        """
        return None

    @abstractmethod
    async def update_user(
        self,
        uuid: str,
        *,
        extend_days: int | None = None,
        traffic_gb: int | None = None,
        devices: int | None = None,
        enable: bool | None = None,
    ) -> PanelUser:
        """Продлить/изменить лимиты/включить-выключить доступ."""

    @abstractmethod
    async def delete_user(self, uuid: str) -> None:
        """Удалить пользователя из панели."""

    @abstractmethod
    async def get_configs(self, uuid: str) -> list[str]:
        """Готовые строки конфигов (vless://, amneziawg://, ...) для подписки."""

    async def close(self) -> None:  # pragma: no cover - переопределяется при необходимости
        """Освободить ресурсы (HTTP-клиент и т.п.)."""


# ------------------------------------------------------------------ ID инбаундов
# Разбор и проверку строки с ID держим в одном месте: раньше парсеров было три
# (форма, реестр, настройки), и они расходились в мелочах — например, «1 2» без
# запятой превращалось в двенадцатый инбаунд.


def panel_label(panel: object) -> str:
    """Имя панели для логов и сообщений: страна, если она известна.

    Все xui-панели называются ``xui``, поэтому по журналу было нельзя понять,
    какая страна не приняла клиента. ``location_title`` проставляет реестр
    («🇩🇪 Германия»); у основной панели это ``LOCATION_TITLE`` из настроек.
    """
    title = str(getattr(panel, "location_title", "") or "").strip()
    return title or str(getattr(panel, "name", "") or "").strip() or "панель"

#: Разделители, которые встречаются в строке ID: форму заполняют и «1, 2», и «1 2».
_ID_SEPARATORS = (",", ";", " ", "\t", "\n")


def parse_inbound_ids(raw: str) -> list[int]:
    """«1, 2 3» → ``[1, 2, 3]``: ID инбаундов из строки.

    Нечисловые токены отбрасываются, повторы схлопываются: строка живёт и в
    форме админки, и в БД, куда её правят руками.
    """
    tokens = (raw or "")
    for separator in _ID_SEPARATORS[1:]:
        tokens = tokens.replace(separator, _ID_SEPARATORS[0])
    result: list[int] = []
    for chunk in tokens.split(_ID_SEPARATORS[0]):
        token = chunk.strip()
        if token.isdigit() and int(token) not in result:
            result.append(int(token))
    return result


def normalize_inbound_ids(raw: str, *, limit: int = 64) -> tuple[str, str]:
    """Строка ID для сохранения: ``(«1,2,3», текст проблемы или пусто)``.

    Пустое значение означает «все инбаунды панели», поэтому мусор и обрезку
    нельзя пропускать молча: оператор, написавший «3x», иначе **расширит**
    выдачу на все инбаунды, будучи уверенным, что ограничил ноду одним.
    """
    tokens = (raw or "")
    bad: list[str] = []
    for separator in _ID_SEPARATORS[1:]:
        tokens = tokens.replace(separator, _ID_SEPARATORS[0])
    for chunk in tokens.split(_ID_SEPARATORS[0]):
        token = chunk.strip()
        if token and not token.isdigit() and token not in bad:
            bad.append(token)
    if bad:
        return "", f"ID инбаундов должны быть числами: убери {', '.join(bad[:5])}"

    value = ",".join(str(item) for item in parse_inbound_ids(raw))
    if len(value) > limit:
        return "", f"Слишком много ID инбаундов: строка длиннее {limit} символов"
    return value, ""
