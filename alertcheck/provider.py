"""Separate mock transaction provider app for Railway demo deployment.

This app reports only seeded transactions. A prefix alone cannot create a
payment result; the matching seed record must exist in Postgres.
"""
import logging
import os
import random
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import select

from .db import Account, Merchant, ProviderTransaction, SessionLocal, init_db


logger = logging.getLogger("uvicorn.error")

RESERVED_REFERENCES = frozenset({"512345678901", "712345678902", "812345678903", "912345678904"})
FIRST_NAMES = ("Ada", "Chinedu", "Fatima", "Ifeoma", "Kunle", "Ngozi", "Seyi", "Tunde")
LAST_NAMES = ("Adeyemi", "Okafor", "Bello", "Nwosu", "Adebayo", "Eze", "Lawal", "Okeke")
_TRUTHY = {"1", "true", "yes"}


def demo_feed_enabled() -> bool:
    return os.getenv("DEMO_FEED_ENABLED", "").strip().lower() in _TRUTHY


def demo_feed_interval_seconds() -> int:
    raw = os.getenv("DEMO_FEED_INTERVAL_SECONDS", "").strip()
    if not raw:
        return 600
    try:
        value = int(raw)
    except ValueError:
        return 600
    return max(30, value)


def _allocate_reference(taken: set[str]) -> str | None:
    for _ in range(8):
        candidate = f"{random.randint(0, 999_999_999_999):012d}"
        if candidate in taken or candidate in RESERVED_REFERENCES:
            continue
        taken.add(candidate)
        return candidate
    return None


def seed_demo_credit(db=None) -> list[dict]:
    """Insert one invented success credit per shop that has an active primary account.

    Writes ProviderTransaction rows only. Confirmation still happens when verify_payment runs.
    `db` is a sessionmaker (SessionLocal by default).
    """
    factory = SessionLocal if db is None else db
    created: list[dict] = []
    with factory.begin() as session:
        pairs = session.execute(
            select(Merchant, Account)
            .join(Account, Account.merchant_id == Merchant.id)
            .where(Account.is_active.is_(True), Account.is_backup.is_(False))
            .order_by(Merchant.id, Account.id)
        ).all()
        chosen: list[tuple[Merchant, Account]] = []
        seen: set[int] = set()
        for merchant, account in pairs:
            if merchant.id in seen:
                continue
            seen.add(merchant.id)
            chosen.append((merchant, account))
        taken = set(session.scalars(select(ProviderTransaction.reference)).all())
        taken.update(RESERVED_REFERENCES)
        for merchant, account in chosen:
            reference = _allocate_reference(taken)
            if reference is None:
                logger.warning("Demo credit skipped merchant=%s; no free reference", merchant.slug)
                continue
            amount_kobo = random.randint(500, 50_000) * 100
            sender_name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            sender_account_number = f"{random.randint(0, 9_999_999_999):010d}"
            session.add(ProviderTransaction(
                merchant_id=merchant.id,
                account_number=account.account_number,
                reference=reference,
                sender_name=sender_name,
                sender_account_number=sender_account_number,
                amount_kobo=amount_kobo,
                currency="NGN",
                status="success",
            ))
            created.append({
                "merchant_id": merchant.id,
                "merchant_slug": merchant.slug,
                "reference": reference,
                "sender_name": sender_name,
                "sender_account_number": sender_account_number,
                "amount_kobo": amount_kobo,
                "account_number": account.account_number,
            })
    for row in created:
        logger.info("Demo credit merchant=%s reference=%s amount_ngn=%s", row["merchant_slug"], row["reference"], row["amount_kobo"] // 100)
    return created


def demo_feed_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            seed_demo_credit()
        except Exception:
            logger.exception("Demo incoming transfer insert failed")
        if stop.wait(demo_feed_interval_seconds()):
            return


def maybe_start_demo_feed(stop: threading.Event) -> threading.Thread | None:
    if not demo_feed_enabled():
        return None
    thread = threading.Thread(target=demo_feed_loop, args=(stop,), name="alertcheck-demo-feed", daemon=True)
    thread.start()
    return thread


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    stop = threading.Event()
    thread = maybe_start_demo_feed(stop)
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=2)


app = FastAPI(title="AlertCheck demo transaction provider", lifespan=lifespan)


def authorized(authorization: str | None = Header(default=None)):
    import secrets

    expected = os.getenv("MOCK_PROVIDER_TOKEN")
    if not expected:
        raise HTTPException(503, "Provider token is not configured")
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(401, "Unauthorized")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/transactions/{reference}", dependencies=[Depends(authorized)])
def get_transaction(reference: str, merchant_id: int, account_number: str):
    with SessionLocal() as db:
        transaction = db.scalar(select(ProviderTransaction).where(
            ProviderTransaction.reference == reference,
            ProviderTransaction.merchant_id == merchant_id,
            ProviderTransaction.account_number == account_number,
        ))
        if not transaction:
            raise HTTPException(404, "Transaction not found")
        return {
            "id": transaction.id,
            "reference": transaction.reference,
            "merchant_id": transaction.merchant_id,
            "account_number": transaction.account_number,
            "sender_name": transaction.sender_name,
            "sender_account_number": transaction.sender_account_number,
            "amount_kobo": transaction.amount_kobo,
            "currency": transaction.currency,
            "status": transaction.status,
            "occurred_at": transaction.occurred_at.isoformat(),
        }
