from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alertcheck.db import Account, Attempt, Base, Merchant, utcnow
from alertcheck.metrics import account_health


def test_hourly_warning_requires_more_than_twenty_attempts_and_half_bad():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        shop = Merchant(name="Ada's Shop", slug="adas-shop", email="ada@example.test", password_hash="unused", whatsapp_number="08012345678")
        db.add(shop)
        db.flush()
        account = Account(merchant_id=shop.id, bank_name="Example Bank", account_name="Ada", account_number="0123456789")
        db.add(account)
        db.flush()
        for i in range(20):
            db.add(Attempt(merchant_id=shop.id, account_id=account.id, customer_chat_id=str(i), customer_name=f"Customer {i}", reference=str(5000000000 + i), amount_kobo=320000, status="pending" if i < 10 else "confirmed"))
        db.commit()
        assert account_health(db, account.id) == {"attempts": 20, "bad": 10, "rate": 50, "unhealthy": False}
        db.add(Attempt(merchant_id=shop.id, account_id=account.id, customer_chat_id="21", customer_name="Customer 21", reference="7000000021", amount_kobo=320000, status="failed"))
        db.commit()
        assert account_health(db, account.id)["unhealthy"] is True


def test_hourly_warning_is_per_account_and_excludes_old_attempts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        one = Merchant(name="One", slug="one", email="one@example.test", password_hash="unused", whatsapp_number="08012345678")
        two = Merchant(name="Two", slug="two", email="two@example.test", password_hash="unused", whatsapp_number="08087654321")
        db.add_all([one, two])
        db.flush()
        first = Account(merchant_id=one.id, bank_name="Bank A", account_name="One", account_number="0123456789")
        second = Account(merchant_id=two.id, bank_name="Bank B", account_name="Two", account_number="9876543210")
        db.add_all([first, second])
        db.flush()
        for i in range(21):
            db.add(Attempt(merchant_id=one.id, account_id=first.id, customer_chat_id=str(i), customer_name="A", reference=str(7000000000 + i), amount_kobo=10000, status="failed", created_at=utcnow() - timedelta(hours=2)))
            db.add(Attempt(merchant_id=two.id, account_id=second.id, customer_chat_id=str(i), customer_name="B", reference=str(8000000000 + i), amount_kobo=10000, status="pending"))
        db.commit()
        assert account_health(db, first.id)["attempts"] == 0
        assert account_health(db, second.id)["unhealthy"] is True
        assert len(db.scalars(select(Attempt).where(Attempt.merchant_id == one.id)).all()) == 21
