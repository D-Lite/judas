import re
import hashlib
from types import SimpleNamespace

from fastapi.testclient import TestClient
from mcp.server.fastmcp import Context
from mcp.types import RequestParams
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers

from alertcheck import chord_config, mcp_tools, web
from alertcheck.db import Base, IntegrationCredential, Merchant, PlatformSecret
from alertcheck.verification import CHANNEL_PLACEHOLDER


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_platform_key_is_encrypted_owner_only_and_env_wins(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    monkeypatch.setattr(chord_config, "SessionLocal", sessions)
    monkeypatch.setenv("SESSION_SECRET", "a-very-long-test-secret-for-platform-key-encryption")
    monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", "another-long-admin-password")
    monkeypatch.delenv("CHORD_API_KEY", raising=False)
    client = TestClient(web.app)

    login_page = client.get("/platform/chord")
    assert "Operator sign in" in login_page.text
    assert client.post("/platform/chord/key", data={"csrf": csrf(login_page.text), "api_key": "chord-example-secret-key"}).status_code == 403
    assert client.post("/platform/login", data={"csrf": csrf(login_page.text), "password": "another-long-admin-password"}, follow_redirects=False).status_code == 303
    page = client.get("/platform/chord")
    assert "NOT SET" in page.text
    response = client.post("/platform/chord/key", data={"csrf": csrf(page.text), "api_key": "chord-example-secret-key"}, follow_redirects=False)
    assert response.status_code == 303
    assert "chord-example-secret-key" not in client.get("/platform/chord").text
    with sessions() as db:
        row = db.get(PlatformSecret, "chord_api_key")
        assert row and "chord-example-secret-key" not in row.encrypted_value
        assert chord_config.chord_api_key(db) == "chord-example-secret-key"
        monkeypatch.setenv("CHORD_API_KEY", "environment-key-override")
        assert chord_config.chord_api_key(db) == "environment-key-override"
        assert chord_config.chord_key_source(db) == "environment"
    assert "SET VIA ENV" in client.get("/platform/chord").text


def test_mcp_endpoint_rejects_missing_key_and_accepts_shop_key(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(mcp_tools, "SessionLocal", sessions)
    with sessions.begin() as db:
        merchant = Merchant(name="Test Shop", slug="test-shop", email="test@example.test", password_hash="unused", whatsapp_number="08012345678")
        db.add(merchant)
        db.flush()
        db.add(IntegrationCredential(merchant_id=merchant.id, token_hash=hashlib.sha256(b"ac_test-shop-secret").hexdigest()))

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    with TestClient(web.app) as client:
        assert client.post("/mcp/", json=init).status_code == 401
        response = client.post("/mcp/", json=init, headers={"Authorization": "Bearer ac_test-shop-secret", "Accept": "application/json, text/event-stream"})
        assert response.status_code == 200, response.text
        assert "serverInfo" in response.text


def _context(headers=None, meta=None, with_request=True):
    request = SimpleNamespace(headers=Headers(headers or {})) if with_request else None
    return Context(request_context=SimpleNamespace(request=request, meta=meta))


def test_verify_payment_schema_requires_only_sender_account_and_amount():
    tool = mcp_tools.mcp._tool_manager.get_tool("verify_payment")
    history = mcp_tools.mcp._tool_manager.get_tool("customer_history")
    assert set(tool.parameters["required"]) == {"sender_account_number", "amount"}
    assert {"customer_name", "reference", "receiving_account_number"} <= set(tool.parameters["properties"])
    for forbidden in ("customer_chat_id", "ctx", "amount_ngn"):
        assert forbidden not in tool.parameters["properties"]
    assert "customer_name" not in tool.parameters["required"]
    assert "receiving_account_number" not in tool.parameters["required"]
    assert "reference" not in tool.parameters["required"]
    assert "do not ask their name again" in tool.description
    assert "Never ask for a chat ID" in tool.description
    assert "accounts_to_ask" in tool.description
    assert history is not None
    assert "merchant_id" not in history.parameters.get("properties", {})
    assert "do not ask for their name again" in history.description


def test_channel_id_comes_from_headers_or_meta_or_a_stable_placeholder():
    assert mcp_tools.customer_chat_id_from_context(_context({"MCP-Session-Id": "  thread-7  "})) == "thread-7"
    assert mcp_tools.customer_chat_id_from_context(_context({"x-thread-id": "thread-9"})) == "thread-9"
    assert mcp_tools.customer_chat_id_from_context(_context({"mcp-session-id": "None", "x-conversation-id": "conv-2"})) == "conv-2"
    assert mcp_tools.customer_chat_id_from_context(_context({"mcp-session-id": "x" * 121, "x-chord-thread-id": "kept"})) == "kept"
    assert mcp_tools.customer_chat_id_from_context(_context()) == CHANNEL_PLACEHOLDER
    assert mcp_tools.customer_chat_id_from_context(_context(with_request=False)) == CHANNEL_PLACEHOLDER
    meta = RequestParams.Meta.model_validate({"thread_id": "from-meta"})
    assert mcp_tools.customer_chat_id_from_context(_context(meta=meta)) == "from-meta"
    assert mcp_tools.customer_chat_id_from_context(_context({"x-chord-conversation-id": "from-header"}, meta=meta)) == "from-header"
