import re
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import Context
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers

from alertcheck import mcp_tools, verification
from alertcheck.db import Account, Attempt, Base, Customer, Merchant, ProviderTransaction, ensure_columns, utcnow
from alertcheck.reports import build_report, period_start
from alertcheck.verification import Claim, verify_claim


def _db(tmp_path, monkeypatch, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(verification, "SessionLocal", sessions)
    return sessions


def _shop(db, slug, email, accounts):
    merchant = Merchant(name=slug, slug=slug, email=email, password_hash="unused", whatsapp_number="08012345678")
    db.add(merchant)
    db.flush()
    for number, backup in accounts:
        db.add(Account(merchant_id=merchant.id, bank_name="Demo Bank" if not backup else "Backup Bank", account_name=slug, account_number=number, is_backup=backup))
    db.flush()
    return merchant


def _credit(db, merchant_id, shop_account, sender, name, amount, reference, status="success"):
    db.add(ProviderTransaction(merchant_id=merchant_id, account_number=shop_account, sender_account_number=sender, sender_name=name, amount_kobo=amount, reference=reference, status=status, currency="NGN"))


def _ctx(merchant_id):
    request = SimpleNamespace(scope={"alertcheck_merchant_id": merchant_id}, headers=Headers({}))
    return Context(request_context=SimpleNamespace(request=request, meta=None))


def test_confirm_claims_transfer_and_blocks_every_later_claim(tmp_path, monkeypatch):
    sessions = _db(tmp_path, monkeypatch, "claimed.db")
    with sessions.begin() as db:
        merchant = _shop(db, "test-shop", "shop@example.test", [("1111111111", False)])
        _credit(db, merchant.id, "1111111111", "2048163759", "Daniel Test", 320000, "512345678901")
        merchant_id = merchant.id
    first = mcp_tools.verify_payment("2048163759", "3200", "Daniel Test", ctx=_ctx(merchant_id))
    assert first["status"] == "confirmed"
    assert first["claimed_at"]
    assert "Payment confirmed" in first["customer_message"]
    with sessions() as db:
        attempt = db.scalar(select(Attempt))
        claimed_at = attempt.claimed_at
        assert claimed_at is not None
        assert db.scalar(select(func.count()).select_from(Customer)) == 1
        assert len(db.scalar(select(Customer)).attempts) == 1
    again = mcp_tools.verify_payment("2048163759", "3.2k", "Daniel Test", ctx=_ctx(merchant_id))
    assert again["status"] == "already_claimed"
    assert again["attempt_id"] == first["attempt_id"]
    assert again["claimed_at"] == first["claimed_at"]
    assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2}", again["customer_message"])
    assert again["customer_message"].startswith("This transfer was already claimed on ")
    assert "Payment confirmed" not in again["customer_message"]
    other = mcp_tools.verify_payment("9182736450", "3200", "Someone Else", "512345678901", ctx=_ctx(merchant_id))
    assert other["status"] == "already_claimed"
    assert other["attempt_id"] == first["attempt_id"]
    assert "customer_name" not in other
    assert "2048163759" not in other["customer_message"]
    assert "9182736450" not in other["customer_message"]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 1
        assert db.scalar(select(func.count()).select_from(Customer)) == 1
        stored = db.scalar(select(Attempt))
        assert stored.claimed_at == claimed_at
        assert stored.status == "confirmed"
        assert stored.amount_kobo == 320000
        assert stored.sender_account_number == "2048163759"


def test_one_account_confirms_without_shop_account_and_known_sender_skips_name(tmp_path, monkeypatch):
    sessions = _db(tmp_path, monkeypatch, "known.db")
    with sessions.begin() as db:
        merchant = _shop(db, "test-shop", "shop@example.test", [("1111111111", False)])
        _credit(db, merchant.id, "1111111111", "2048163759", "Daniel Test", 320000, "512345678901")
        _credit(db, merchant.id, "1111111111", "2048163759", "Daniel Test", 500000, "512345678902")
        merchant_id = merchant.id
    first = verify_claim(Claim(merchant_id, "channel", "2048163759", 320000, "Daniel Test"))
    assert first["status"] == "confirmed"
    assert "accounts_to_ask" not in first
    second = verify_claim(Claim(merchant_id, "channel", "2048163759", 500000, None))
    assert second["status"] == "confirmed"
    nickname = verify_claim(Claim(merchant_id, "channel", "2048163759", 320000, "Dan", "512345678901"))
    assert nickname["status"] == "already_claimed"
    with sessions() as db:
        customers = db.scalars(select(Customer)).all()
        assert len(customers) == 1
        assert customers[0].display_name == "Daniel Test"
        assert len(customers[0].attempts) == 2


def test_several_accounts_ask_only_when_the_match_is_not_proven(tmp_path, monkeypatch):
    sessions = _db(tmp_path, monkeypatch, "multi.db")
    with sessions.begin() as db:
        merchant = _shop(db, "test-shop", "shop@example.test", [("1111111111", False), ("2222222222", True)])
        _credit(db, merchant.id, "1111111111", "2048163759", "Daniel Test", 320000, "512345678901")
        _credit(db, merchant.id, "2222222222", "2048163759", "Daniel Test", 320000, "512345678909")
        merchant_id = merchant.id
    empty = verify_claim(Claim(merchant_id, "channel", "3159274860", 210000, "New Person"))
    assert empty["status"] == "pending"
    assert {item["account_number"] for item in empty["accounts_to_ask"]} == {"1111111111", "2222222222"}
    assert all("account_number" in item and "bank_name" in item for item in empty["accounts_to_ask"])
    ambiguous = verify_claim(Claim(merchant_id, "channel", "2048163759", 320000, "Daniel Test"))
    assert ambiguous["status"] == "review"
    assert "accounts_to_ask" in ambiguous
    chosen = verify_claim(Claim(merchant_id, "channel", "2048163759", 320000, "Daniel Test", None, "2222222222"))
    assert chosen["status"] == "confirmed"
    assert chosen["reference"] == "512345678909"
    wrong = verify_claim(Claim(merchant_id, "channel", "3159274860", 210000, "New Person", None, "0000000000"))
    assert wrong["status"] == "review"
    assert "does not belong" in wrong["message"]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Customer)) == 1
        assert db.scalar(select(Attempt).where(Attempt.reference == "512345678901")) is None


def test_pending_attempt_can_become_claimed_once(tmp_path, monkeypatch):
    sessions = _db(tmp_path, monkeypatch, "pending.db")
    with sessions.begin() as db:
        merchant = _shop(db, "test-shop", "shop@example.test", [("1111111111", False)])
        _credit(db, merchant.id, "1111111111", "2048163759", "Daniel Test", 320000, "512345678905", status="pending")
        merchant_id = merchant.id
        credit_id = db.scalar(select(ProviderTransaction.id))
    first = verify_claim(Claim(merchant_id, "thread-real", "2048163759", 320000, "Daniel Test", "512345678905"))
    assert first["status"] == "pending"
    with sessions.begin() as db:
        db.get(ProviderTransaction, credit_id).status = "success"
    second = verify_claim(Claim(merchant_id, "channel", "2048163759", 320000, "Daniel Test", "512345678905"))
    assert second["status"] == "confirmed"
    with sessions() as db:
        stored = db.scalar(select(Attempt))
        assert stored.customer_chat_id == "thread-real"
        assert stored.claimed_at is not None
    third = verify_claim(Claim(merchant_id, "someone-else", "7777777777", 100, "Other", "512345678905"))
    assert third["status"] == "already_claimed"
    assert "customer_name" not in third


def test_report_week_lists_people_and_new_customers(tmp_path, monkeypatch):
    sessions = _db(tmp_path, monkeypatch, "report.db")
    start = period_start(utcnow().astimezone(ZoneInfo("Africa/Lagos")), "week")
    with sessions.begin() as db:
        merchant = _shop(db, "test-shop", "shop@example.test", [("1111111111", False)])
        account_id = db.scalar(select(Account.id))
        returning = Customer(merchant_id=merchant.id, sender_account_number="2048163759", display_name="Returning Payer", created_at=start - timedelta(days=10))
        fresh = Customer(merchant_id=merchant.id, sender_account_number="3159274860", display_name="Fresh Payer", created_at=start + timedelta(hours=1))
        db.add_all([returning, fresh])
        db.flush()
        db.add(Attempt(merchant_id=merchant.id, account_id=account_id, customer_id=returning.id, customer_chat_id="channel", customer_name="Returning Payer", sender_account_number="2048163759", reference="100000000001", amount_kobo=100000, status="confirmed", created_at=start - timedelta(days=3)))
        db.add(Attempt(merchant_id=merchant.id, account_id=account_id, customer_id=returning.id, customer_chat_id="channel", customer_name="Returning Payer", sender_account_number="2048163759", reference="100000000002", amount_kobo=250000, status="confirmed", created_at=start + timedelta(hours=2)))
        db.add(Attempt(merchant_id=merchant.id, account_id=account_id, customer_id=fresh.id, customer_chat_id="channel", customer_name="Fresh Payer", sender_account_number="3159274860", reference="100000000003", amount_kobo=400000, status="confirmed", created_at=start + timedelta(hours=3)))
        merchant_id = merchant.id
    with sessions() as db:
        merchant = db.get(Merchant, merchant_id)
        report = build_report(db, merchant, "week")
    assert report["new_customer_count"] == 1
    assert [person["name"] for person in report["customers"]] == ["Fresh Payer", "Returning Payer"]
    assert report["customers"][0]["confirmed_amount_ngn"] == 4000
    assert report["customers"][1]["confirmed_count"] == 1
    assert report["customers"][1]["confirmed_amount_ngn"] == 2500


def test_ensure_columns_backfills_claimed_at(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE attempts (id INTEGER PRIMARY KEY, status VARCHAR(24), created_at DATETIME, updated_at DATETIME)"))
        connection.execute(text("INSERT INTO attempts (status, created_at, updated_at) VALUES ('confirmed', '2026-01-01', '2026-01-02')"))
    ensure_columns(engine)
    with engine.connect() as connection:
        claimed = connection.execute(text("SELECT claimed_at FROM attempts")).scalar_one()
    assert "2026-01-02" in str(claimed)
    ensure_columns(engine)
