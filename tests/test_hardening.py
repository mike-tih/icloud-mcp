"""Security hardening: URL allow-list, header injection, token auth, credential policy."""

import asyncio
import logging

import httpx
import pytest
from starlette.middleware import Middleware

from icloud_mcp import calendar, contacts, mail_utils, server
from icloud_mcp.config import _parse_categories, config
from icloud_mcp.urls import ensure_icloud_url, is_trusted_host


def test_trusted_hosts():
    assert is_trusted_host("p72-caldav.icloud.com")
    assert is_trusted_host("contacts.icloud.com")
    assert is_trusted_host("p12-contacts.icloud.com.")
    assert not is_trusted_host("icloud.com.attacker.net")
    assert not is_trusted_host("attacker.com")


def test_ensure_icloud_url_rejects_foreign_and_plain_http():
    ok = "https://p72-caldav.icloud.com/123/calendars/home/x.ics"
    assert ensure_icloud_url(ok) == ok
    for bad in [
        "https://attacker.com/x.ics",
        "http://p72-caldav.icloud.com/x.ics",
        "https://user:pw@p72-caldav.icloud.com/x.ics",
        "p72-caldav.icloud.com/x.ics",
        "",
    ]:
        with pytest.raises(ValueError):
            ensure_icloud_url(bad)


def test_calendar_client_refuses_foreign_host():
    with pytest.raises(ValueError, match="untrusted host"):
        calendar._client_for_url("https://evil.example/cal/", "me@icloud.com", "pw")


def test_contacts_refuse_foreign_host(monkeypatch):
    monkeypatch.setattr(contacts, "require_auth", lambda: ("me@icloud.com", "pw"))
    for fn in (contacts.get_contact, contacts.delete_contact):
        with pytest.raises(ValueError, match="untrusted host"):
            fn("https://evil.example/card.vcf")
    with pytest.raises(ValueError, match="untrusted host"):
        contacts.update_contact("https://evil.example/card.vcf", name="x")


def test_parse_recipients_validates_and_rejects_injection():
    assert mail_utils.parse_recipients("a@x.com, Bob <b@x.com>;c@x.com") == [
        "a@x.com",
        "Bob <b@x.com>",
        "c@x.com",
    ]
    with pytest.raises(ValueError, match="line breaks"):
        mail_utils.parse_recipients("a@x.com\r\nBcc: victim@x.com")
    with pytest.raises(ValueError, match="Invalid email"):
        mail_utils.parse_recipients("not-an-address")
    with pytest.raises(ValueError, match="line breaks"):
        mail_utils.validate_header_text("Hi\nX-Injected: 1", "subject")
    assert mail_utils.bare_addresses(["Bob <b@x.com>", "a@x.com"]) == ["b@x.com", "a@x.com"]


def test_clean_rich_text_modes(monkeypatch):
    html = "Reynier Park<br>2803 Reynier Ave<br><b>LA</b>"
    monkeypatch.setattr(config, "HTML_MODE", "text")
    assert "<br>" not in mail_utils.clean_rich_text(html)
    assert "Reynier Park" in mail_utils.clean_rich_text(html)
    assert mail_utils.clean_rich_text("plain, text") == "plain, text"
    monkeypatch.setattr(config, "HTML_MODE", "raw")
    assert mail_utils.clean_rich_text(html) == html
    monkeypatch.setattr(config, "HTML_MODE", "markdown")
    rendered = mail_utils.clean_rich_text('See <a href="https://x.test/map">map</a><br><b>LA</b><ul><li>one</li></ul>')
    assert "[map](https://x.test/map)" in rendered and "**LA**" in rendered and "- one" in rendered


def test_parse_categories():
    assert _parse_categories(None) == {"calendar", "contacts", "email"}
    assert _parse_categories("mail, Calendar") == {"email", "calendar"}
    with pytest.raises(ValueError):
        _parse_categories("calendar,bogus")
    with pytest.raises(ValueError):
        _parse_categories(" , ")


def test_http_credential_policy(monkeypatch, caplog):
    monkeypatch.setattr(config, "FALLBACK_EMAIL", "a@icloud.com")
    monkeypatch.setattr(config, "FALLBACK_PASSWORD", "pw")

    monkeypatch.setattr(config, "MCP_AUTH_TOKEN", None)
    monkeypatch.setattr(config, "ALLOW_ENV_CREDENTIALS", None)
    with caplog.at_level(logging.WARNING):
        assert server._http_credential_policy() == []
    assert config.ENV_CREDENTIALS_ACTIVE is False
    assert "ignored over HTTP" in caplog.text

    monkeypatch.setattr(config, "MCP_AUTH_TOKEN", "s3cret")
    middleware = server._http_credential_policy()
    assert config.ENV_CREDENTIALS_ACTIVE is True
    assert len(middleware) == 1

    monkeypatch.setattr(config, "MCP_AUTH_TOKEN", None)
    monkeypatch.setattr(config, "ALLOW_ENV_CREDENTIALS", True)
    server._http_credential_policy()
    assert config.ENV_CREDENTIALS_ACTIVE is True

    config.ENV_CREDENTIALS_ACTIVE = True  # restore for other tests


def test_env_credentials_ignored_when_inactive(monkeypatch):
    from icloud_mcp import auth

    monkeypatch.setattr(auth, "_request_headers", lambda: {})
    monkeypatch.setattr(config, "ENV_CREDENTIALS_ACTIVE", False)
    with pytest.raises(auth.AuthenticationError):
        auth.get_credentials()


def test_token_middleware():
    app = server.mcp.http_app(
        path="/mcp", middleware=[Middleware(server.TokenAuthMiddleware, token="s3cret")]
    )

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            health = await client.get("/health")
            anon = await client.post("/mcp", json={})
            wrong = await client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
            bearer = await client.post("/mcp", json={}, headers={"Authorization": "Bearer s3cret"})
            header = await client.post("/mcp", json={}, headers={"X-MCP-Token": "s3cret"})
            return health.status_code, anon.status_code, wrong.status_code, bearer.status_code, header.status_code

    health, anon, wrong, bearer, header = asyncio.run(run())
    assert health == 200
    assert anon == 401 and wrong == 401
    assert bearer != 401 and header != 401


def test_redact_secrets(monkeypatch):
    from icloud_mcp import auth

    monkeypatch.setattr(auth, "get_credentials", lambda: ("a@icloud.com", "hunter2"))
    assert auth.redact_secrets("login failed for hunter2 at host") == "login failed for *** at host"
