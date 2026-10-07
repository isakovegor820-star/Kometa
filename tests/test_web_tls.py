"""Тесты TLS веб-слоя: без https клиенты не принимают ссылку-подписку.

Happ и v2rayNG отвечают «Небезопасная схема HTTP запрещена», поэтому на боевом
сервере веб-слой должен слушать https с сертификатом Let's Encrypt.
"""

from __future__ import annotations

from app.config import get_settings
from app.main import _uvicorn_kwargs


def test_uvicorn_without_certificates_uses_plain_http(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_ssl_cert", "")
    monkeypatch.setattr(settings, "web_ssl_key", "")

    kwargs = _uvicorn_kwargs()

    assert "ssl_certfile" not in kwargs
    assert "ssl_keyfile" not in kwargs
    assert kwargs["port"] == settings.web_port


def test_uvicorn_with_certificates_enables_tls(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_ssl_cert", "/etc/letsencrypt/live/x/fullchain.pem")
    monkeypatch.setattr(settings, "web_ssl_key", "/etc/letsencrypt/live/x/privkey.pem")

    kwargs = _uvicorn_kwargs()

    assert kwargs["ssl_certfile"] == "/etc/letsencrypt/live/x/fullchain.pem"
    assert kwargs["ssl_keyfile"] == "/etc/letsencrypt/live/x/privkey.pem"


def test_partial_tls_settings_are_ignored(monkeypatch):
    """Задан только сертификат — включать TLS нельзя, будет падение на старте."""
    settings = get_settings()
    monkeypatch.setattr(settings, "web_ssl_cert", "/tmp/cert.pem")
    monkeypatch.setattr(settings, "web_ssl_key", "")

    assert "ssl_certfile" not in _uvicorn_kwargs()
