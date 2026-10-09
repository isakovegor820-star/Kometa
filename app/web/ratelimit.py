"""Лимиты публичных запросов по IP и короткий кэш подписки.

Зачем. Публичные адреса — единственная часть сервиса, открытая всему интернету:

* ``/sub/{token}`` и ``/connect/{token}`` — по токену, который можно подобрать
  или «подсмотреть», каждый запрос стоит двух HTTP-вызовов к панелям. Тридцать
  запросов подряд от одного IP — это уже нагрузка, а не клиент;
* ``/payments/*/webhook`` — тела читаются целиком до проверки подписи, поэтому
  поток запросов с одного IP съедал бы память (находка аудита 08.10.2026).

Лимит держим в памяти процесса: сервис однопроцессный (uvicorn + бот), а
ради честного распределённого лимита заводить Redis ради старта не нужно. При
горизонтальном масштабировании счётчики придётся вынести наружу — об этом
напоминает формулировка в ответе 429.

Ответ 429 понятный: сколько ждать и что делать, без внутренних деталей.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Префиксы путей, к которым применяется лимит, и настройка лимита для них.
SUB_PREFIXES: tuple[str, ...] = ("/sub/", "/connect/")
WEBHOOK_SUFFIX = "/webhook"


@dataclass(slots=True)
class Decision:
    """Решение лимитера: пропустить или отдать 429."""

    allowed: bool
    retry_after: int = 0
    limit: int = 0


class RateLimiter:
    """Скользящее окно по ключу (IP + группа адресов).

    Хранит время последних запросов. Скользящее окно выбрано вместо «счётчик в
    фиксированной минуте»: иначе на границе минуты проходит двойная порция.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, *, limit: int, window_seconds: int, now: float | None = None) -> Decision:
        if limit <= 0:
            return Decision(allowed=True, limit=0)
        moment = time.time() if now is None else now
        window_start = moment - window_seconds
        recent = [stamp for stamp in self._hits.get(key, []) if stamp > window_start]
        if len(recent) >= limit:
            self._hits[key] = recent
            oldest = recent[0]
            retry_after = max(1, int(window_seconds - (moment - oldest)) + 1)
            return Decision(allowed=False, retry_after=retry_after, limit=limit)
        recent.append(moment)
        self._hits[key] = recent
        return Decision(allowed=True, limit=limit)

    def reset(self, key: str | None = None) -> None:
        """Сбросить счётчики: нужен тестам и ручной разблокировке."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)

    def tracked(self) -> int:
        return len(self._hits)


@dataclass
class SubCache:
    """Короткий кэш данных панелей для одной подписки.

    Кэшируем не HTTP-ответ целиком, а **собранные данные панелей** (конфиги,
    трафик, каналы): формат ответа выбирается по User-Agent, и один и тот же
    токен отдаётся и как base64, и как Clash YAML. Продление доступа при этом
    видно сразу — срок берётся из БД, а не из кэша.
    """

    max_items: int = 500
    _items: dict[str, tuple[float, object]] = field(default_factory=dict)

    @staticmethod
    def ttl_seconds() -> int:
        """Срок жизни записи. Читаем из настроек каждый раз: владелец может
        поменять SUB_CACHE_SECONDS и перезапустить сервис, а тесты — подменить
        значение на время проверки."""
        from app.config import get_settings

        return max(0, int(get_settings().sub_cache_seconds))

    def get(self, key: str, *, now: float | None = None):  # noqa: ANN201
        item = self._items.get(key)
        if item is None:
            return None
        moment = time.time() if now is None else now
        stored_at, value = item
        if moment - stored_at > self.ttl_seconds():
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: object, *, now: float | None = None) -> None:
        self._items[key] = (time.time() if now is None else now, value)
        if len(self._items) > self.max_items:
            # Выкидываем самые старые записи: словарь не должен расти бесконечно.
            for stale in sorted(self._items, key=lambda item: self._items[item][0])[: self.max_items // 5]:
                self._items.pop(stale, None)

    def clear(self) -> None:
        self._items.clear()

    def size(self) -> int:
        return len(self._items)


def public_client_ip(request) -> str:  # noqa: ANN001 - starlette Request
    """IP клиента публичного адреса.

    Заголовку ``X-Forwarded-For`` верим только при ``ADMIN_TRUST_PROXY=true``
    (там же, где панель): иначе его подделает кто угодно и лимит обойдут одним
    заголовком. Без доверия к прокси считаем адрес сокета.
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.admin_trust_proxy:
        forwarded = request.headers.get("x-forwarded-for") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def limit_for_path(path: str, settings) -> tuple[str, int] | None:  # noqa: ANN001
    """Группа лимита и порог для пути. None — путь не лимитируем.

    ``/sub/{token}`` и ``/connect/{token}`` — общий лимит «клиентские адреса»:
    переход со страницы подключения сразу дёргает и ``/connect``, и ``/sub``,
    считать их разными корзинами значит удваивать порог для одного клиента.
    """
    if any(path.startswith(prefix) for prefix in SUB_PREFIXES):
        return "sub", int(settings.rate_limit_requests)
    if path.startswith("/payments/") and path.endswith(WEBHOOK_SUFFIX):
        return "webhook", int(settings.rate_limit_webhook_requests)
    return None
