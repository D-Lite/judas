import threading

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from alertcheck import provider
from alertcheck.db import Account, Attempt, Base, Merchant, ProviderTransaction


def _sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'feed.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(provider, "SessionLocal", sessions)
    return sessions


def _randint(sequence):
    values = iter(sequence)

    def randint(low, high):
        if high == 50_000:
            return 2_500
        if high == 9_999_999_999:
            return 4_028_163_759
        return next(values)

    return randint


def test_seed_demo_credit_writes_one_success_per_active_primary(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, monkeypatch)
    monkeypatch.setattr(provider.random, "randint", _randint(range(200_000_000_001, 200_000_000_050)))
    with sessions.begin() as db:
        primary_shop = Merchant(name="Primary Shop", slug="primary-shop", email="primary@example.test", password_hash="unused", whatsapp_number="08011111111")
        backup_only = Merchant(name="Backup Shop", slug="backup-shop", email="backup@example.test", password_hash="unused", whatsapp_number="08022222222")
        inactive_shop = Merchant(name="Inactive Shop", slug="inactive-shop", email="inactive@example.test", password_hash="unused", whatsapp_number="08033333333")
        no_account = Merchant(name="Empty Shop", slug="empty-shop", email="empty@example.test", password_hash="unused", whatsapp_number="08044444444")
        db.add_all([primary_shop, backup_only, inactive_shop, no_account])
        db.flush()
        db.add(Account(merchant_id=primary_shop.id, bank_name="Mock Bank", account_name="Primary", account_number="1111111111", is_backup=False))
        db.add(Account(merchant_id=primary_shop.id, bank_name="Mock Bank", account_name="Backup", account_number="1111111112", is_backup=True))
        db.add(Account(merchant_id=backup_only.id, bank_name="Mock Bank", account_name="Backup", account_number="2222222222", is_backup=True))
        db.add(Account(merchant_id=inactive_shop.id, bank_name="Mock Bank", account_name="Old", account_number="3333333333", is_backup=False, is_active=False))

    created = provider.seed_demo_credit()
    assert len(created) == 1
    assert created[0]["merchant_slug"] == "primary-shop"
    assert created[0]["account_number"] == "1111111111"
    assert created[0]["reference"] not in provider.RESERVED_REFERENCES
    assert len(created[0]["reference"]) == 12 and created[0]["reference"].isdigit()
    assert 50_000 <= created[0]["amount_kobo"] <= 5_000_000
    assert created[0]["amount_kobo"] % 100 == 0
    names = {f"{first} {last}" for first in provider.FIRST_NAMES for last in provider.LAST_NAMES}
    assert created[0]["sender_name"] in names
    assert len(created[0]["sender_account_number"]) == 10 and created[0]["sender_account_number"].isdigit()
    with sessions() as db:
        row = db.scalar(select(ProviderTransaction))
        assert row.status == "success"
        assert row.currency == "NGN"
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0

    again = provider.seed_demo_credit()
    assert len(again) == 1
    assert again[0]["reference"] != created[0]["reference"]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderTransaction)) == 2
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0


def test_seed_demo_credit_retries_past_reserved_and_existing_references(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, monkeypatch)
    monkeypatch.setattr(provider.random, "randint", _randint([512345678901, 111111111111, 333333333333, 333333333333, 444444444444]))
    with sessions.begin() as db:
        first = Merchant(name="First Shop", slug="first-shop", email="first@example.test", password_hash="unused", whatsapp_number="08011111111")
        second = Merchant(name="Second Shop", slug="second-shop", email="second@example.test", password_hash="unused", whatsapp_number="08022222222")
        db.add_all([first, second])
        db.flush()
        db.add(Account(merchant_id=first.id, bank_name="Mock Bank", account_name="First", account_number="1111111111"))
        db.add(Account(merchant_id=second.id, bank_name="Mock Bank", account_name="Second", account_number="2222222222"))
        db.add(ProviderTransaction(merchant_id=first.id, account_number="1111111111", reference="111111111111", sender_name="Already There", amount_kobo=100000, status="success"))

    created = provider.seed_demo_credit()
    references = {row["merchant_slug"]: row["reference"] for row in created}
    assert references == {"first-shop": "333333333333", "second-shop": "444444444444"}


def test_demo_feed_loop_stays_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("DEMO_FEED_ENABLED", raising=False)
    monkeypatch.delenv("DEMO_FEED_INTERVAL_SECONDS", raising=False)
    assert provider.demo_feed_enabled() is False
    assert provider.demo_feed_interval_seconds() == 600
    for value in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("DEMO_FEED_ENABLED", value)
        assert provider.demo_feed_enabled() is True
    monkeypatch.setenv("DEMO_FEED_ENABLED", "false")
    assert provider.demo_feed_enabled() is False
    monkeypatch.setenv("DEMO_FEED_INTERVAL_SECONDS", "10")
    assert provider.demo_feed_interval_seconds() == 30
    monkeypatch.setenv("DEMO_FEED_INTERVAL_SECONDS", "90")
    assert provider.demo_feed_interval_seconds() == 90
    assert provider.maybe_start_demo_feed(threading.Event()) is None
