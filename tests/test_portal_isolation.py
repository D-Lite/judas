import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alertcheck.db import Account, Attempt, Base, Customer, IntegrationCredential, Merchant, ProviderTransaction, utcnow
from alertcheck import web


def token_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_shops_only_see_their_own_accounts_and_attempts(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'portal.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(web, "SessionLocal", sessions)

    first = TestClient(web.app)
    second = TestClient(web.app)
    for client, name, email, phone in [
        (first, "Ada Market", "ada@example.test", "08012345678"),
        (second, "Bola Foods", "bola@example.test", "08087654321"),
    ]:
        csrf = token_from(client.get("/signup").text)
        response = client.post("/signup", data={"csrf": csrf, "shop_name": name, "email": email, "password": "long-test-password", "whatsapp_number": phone}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"

    csrf = token_from(first.get("/setup").text)
    response = first.post("/setup/accounts", data={"csrf": csrf, "bank_name": "First Bank", "account_name": "Ada Market", "account_number": "0123456789", "role": "primary"}, follow_redirects=False)
    assert response.status_code == 303

    with sessions() as db:
        ada = db.query(Merchant).filter_by(email="ada@example.test").one()
        account = db.query(Account).filter_by(merchant_id=ada.id).one()
        db.add(Attempt(merchant_id=ada.id, account_id=account.id, customer_chat_id="customer-1", customer_name="Daniel Test", reference="512345678901", amount_kobo=320000, status="confirmed"))
        db.commit()

    ada_dashboard = first.get("/dashboard").text
    bola_dashboard = second.get("/dashboard").text
    assert "Daniel Test" in ada_dashboard
    assert "₦3,200.00" in ada_dashboard
    assert "Daniel Test" not in bola_dashboard
    assert "0123456789" not in bola_dashboard
    assert "First Bank" not in bola_dashboard
    assert "₦0.00" in bola_dashboard
    assert "Daniel Test" in first.get("/dashboard?status=confirmed").text
    assert "Daniel Test" not in first.get("/dashboard?status=failed").text
    assert first.get("/dashboard?status=unknown").status_code == 400


def test_connection_keys_are_tenant_scoped_and_rotation_revokes_old_key(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'keys.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    first, second = TestClient(web.app), TestClient(web.app)
    for client, name, email, phone, account_number in [
        (first, "Ada Market", "ada@example.test", "08012345678", "0123456789"),
        (second, "Bola Foods", "bola@example.test", "08087654321", "9876543210"),
    ]:
        csrf = token_from(client.get("/signup").text)
        assert client.post("/signup", data={"csrf": csrf, "shop_name": name, "email": email, "password": "long-test-password", "whatsapp_number": phone}, follow_redirects=False).status_code == 303
        csrf = token_from(client.get("/setup").text)
        assert client.post("/setup/accounts", data={"csrf": csrf, "bank_name": "Demo Bank", "account_name": name, "account_number": account_number, "role": "primary"}, follow_redirects=False).status_code == 303

    def issue(client):
        csrf = token_from(client.get("/setup").text)
        response = client.post("/setup/connection-key", data={"csrf": csrf})
        assert response.status_code == 200
        return re.search(r"<code>(ac_[^<]+)</code>", response.text).group(1)

    first_key, second_key = issue(first), issue(second)
    assert first_key != second_key
    assert first_key not in first.get("/setup").text
    rotated_key = issue(first)
    assert rotated_key != first_key
    with sessions() as db:
        credentials = db.query(IntegrationCredential).all()
        assert len(credentials) == 2
        assert all(credential.token_hash not in {first_key, second_key, rotated_key} for credential in credentials)
        ada = db.query(Merchant).filter_by(email="ada@example.test").one()
        bola = db.query(Merchant).filter_by(email="bola@example.test").one()
        assert next(c for c in credentials if c.merchant_id == ada.id).token_hash == __import__("hashlib").sha256(rotated_key.encode()).hexdigest()
        assert next(c for c in credentials if c.merchant_id == bola.id).token_hash == __import__("hashlib").sha256(second_key.encode()).hexdigest()


def test_incoming_transfers_stay_on_the_receiving_shop(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'feed.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    ada_client, bola_client = TestClient(web.app), TestClient(web.app)
    for client, name, email, phone, account_number in [
        (ada_client, "Ada Market", "ada@example.test", "08012345678", "0123456789"),
        (bola_client, "Bola Foods", "bola@example.test", "08087654321", "9876543210"),
    ]:
        csrf = token_from(client.get("/signup").text)
        assert client.post("/signup", data={"csrf": csrf, "shop_name": name, "email": email, "password": "long-test-password", "whatsapp_number": phone}, follow_redirects=False).status_code == 303
        csrf = token_from(client.get("/setup").text)
        assert client.post("/setup/accounts", data={"csrf": csrf, "bank_name": "Demo Bank", "account_name": name, "account_number": account_number, "role": "primary"}, follow_redirects=False).status_code == 303

    now = utcnow()
    with sessions.begin() as db:
        ada = db.query(Merchant).filter_by(email="ada@example.test").one()
        bola = db.query(Merchant).filter_by(email="bola@example.test").one()
        ada_account = db.query(Account).filter_by(merchant_id=ada.id).one()
        db.add(Attempt(merchant_id=ada.id, account_id=ada_account.id, customer_chat_id="channel", customer_name="Receipt Match", reference="600000000010", amount_kobo=250000, status="confirmed"))
        db.add(ProviderTransaction(merchant_id=ada.id, account_number=ada_account.account_number, reference="600000000010", sender_name="Receipt Match", amount_kobo=250000, currency="NGN", status="success", occurred_at=now))
        for index in range(9):
            db.add(ProviderTransaction(merchant_id=ada.id, account_number=ada_account.account_number, reference=f"{600000000020 + index:012d}", sender_name=f"Feed Sender {index}", amount_kobo=150000, currency="NGN", status="success", occurred_at=now - timedelta(minutes=9 - index)))
        db.add(ProviderTransaction(merchant_id=bola.id, account_number="9876543210", reference="600000000099", sender_name="Bola Secret Transfer", amount_kobo=180000, currency="NGN", status="success", occurred_at=now))

    ada_dashboard = ada_client.get("/dashboard").text
    bola_dashboard = bola_client.get("/dashboard").text
    assert "Receipt Match" in ada_dashboard
    assert "Checked · Confirmed" in ada_dashboard
    assert "Feed Sender 8" in ada_dashboard
    assert "Not checked yet" in ada_dashboard
    assert "Feed Sender 0" not in ada_dashboard
    assert "Bola Secret Transfer" not in ada_dashboard
    assert "Bola Secret Transfer" in bola_dashboard
    assert "Feed Sender 8" not in bola_dashboard
    assert "Receipt Match" not in bola_dashboard
    assert "600000000010" not in bola_dashboard


def test_csv_downloads_stay_inside_the_signed_in_shop(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'csv.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    ada_client, bola_client = TestClient(web.app), TestClient(web.app)
    for client, name, email, phone, account_number in [
        (ada_client, "Ada Market", "ada@example.test", "08012345678", "0123456789"),
        (bola_client, "Bola Foods", "bola@example.test", "08087654321", "9876543210"),
    ]:
        csrf = token_from(client.get("/signup").text)
        assert client.post("/signup", data={"csrf": csrf, "shop_name": name, "email": email, "password": "long-test-password", "whatsapp_number": phone}, follow_redirects=False).status_code == 303
        csrf = token_from(client.get("/setup").text)
        assert client.post("/setup/accounts", data={"csrf": csrf, "bank_name": "Demo Bank", "account_name": name, "account_number": account_number, "role": "primary"}, follow_redirects=False).status_code == 303
    with sessions.begin() as db:
        ada = db.query(Merchant).filter_by(email="ada@example.test").one()
        bola = db.query(Merchant).filter_by(email="bola@example.test").one()
        ada_account = db.query(Account).filter_by(merchant_id=ada.id).one()
        bola_account = db.query(Account).filter_by(merchant_id=bola.id).one()
        ada_customer = Customer(merchant_id=ada.id, sender_account_number="2048163759", display_name="Ada Ledger")
        bola_customer = Customer(merchant_id=bola.id, sender_account_number="9182736450", display_name="Bola Hidden")
        db.add_all([ada_customer, bola_customer])
        db.flush()
        db.add(Attempt(merchant_id=ada.id, account_id=ada_account.id, customer_id=ada_customer.id, customer_chat_id="channel", customer_name="Ada Ledger", sender_account_number="2048163759", reference="600000000010", amount_kobo=250000, status="confirmed", claimed_at=utcnow()))
        db.add(Attempt(merchant_id=bola.id, account_id=bola_account.id, customer_id=bola_customer.id, customer_chat_id="channel", customer_name="Bola Hidden", sender_account_number="9182736450", reference="600000000099", amount_kobo=180000, status="confirmed", claimed_at=utcnow()))
        db.add(ProviderTransaction(merchant_id=ada.id, account_number=ada_account.account_number, sender_account_number="2048163759", reference="600000000010", sender_name="Ada Ledger", amount_kobo=250000, status="success"))
    guest = TestClient(web.app)
    assert guest.get("/dashboard/attempts.csv", follow_redirects=False).status_code == 303
    ada_attempts = ada_client.get("/dashboard/attempts.csv")
    bola_attempts = bola_client.get("/dashboard/attempts.csv")
    assert ada_attempts.status_code == 200
    assert "text/csv" in ada_attempts.headers["content-type"]
    assert "judas-transactions.csv" in ada_attempts.headers["content-disposition"]
    assert "Ada Ledger" in ada_attempts.text
    assert "claimed_at" in ada_attempts.text
    assert "Bola Hidden" not in ada_attempts.text
    assert "Bola Hidden" in bola_attempts.text
    assert "Ada Ledger" not in bola_attempts.text
    ada_customers = ada_client.get("/dashboard/customers.csv").text
    assert "Ada Ledger" in ada_customers
    assert "2048163759" in ada_customers
    assert "Bola Hidden" not in ada_customers
    incoming = ada_client.get("/dashboard/incoming.csv").text
    assert "Ada Ledger" in incoming
    assert "Bola Hidden" not in incoming
    week = ada_client.get("/dashboard/report.csv?period=week")
    assert "judas-week.csv" in week.headers["content-disposition"]
    assert "Ada Ledger" in week.text
    assert "Bola Hidden" not in week.text
    activity = ada_client.get("/dashboard").text
    customers_page = ada_client.get("/dashboard?tab=customers").text
    downloads = ada_client.get("/dashboard?tab=downloads").text
    download_links = (
        'href="/dashboard/attempts.csv"',
        'href="/dashboard/customers.csv"',
        'href="/dashboard/report.csv?period=week"',
        'href="/dashboard/incoming.csv"',
    )
    for link in download_links:
        assert link not in activity
        assert link not in customers_page
        assert link in downloads
    assert 'href="/dashboard?tab=downloads&amp;period=week"' in activity or 'href="/dashboard?tab=downloads&period=week"' in activity
    assert "Reports for this shop, downloaded as CSV." in downloads
    assert "Who paid this week" in downloads
    assert "judas-transactions.csv" in downloads
    assert "judas-customers.csv" in downloads
    assert "judas-week.csv" in downloads
    assert "judas-incoming.csv" in downloads
    assert "Ada Ledger" in downloads
    assert "2,500.00" in downloads
    assert "Bola Hidden" not in downloads
    assert "Ada Ledger" in customers_page
    assert "Bola Hidden" not in customers_page
    kept = ada_client.get("/dashboard?tab=downloads&period=month&status=confirmed").text
    assert 'href="/dashboard/report.csv?period=month"' in kept
    assert "judas-month.csv" in kept
    assert "Who paid this month" in kept
    assert 'href="/dashboard?status=confirmed"' in kept
    assert 'href="/dashboard/attempts.csv"' not in ada_client.get("/dashboard?status=confirmed").text
    assert ada_client.get("/dashboard?tab=nope").status_code == 400
