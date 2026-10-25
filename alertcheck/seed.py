"""Seed invented mock-provider records for one already-created merchant.

Usage: python -m alertcheck.seed --merchant shop-slug [--hourly]
"""
import argparse

from sqlalchemy import select

from .db import Account, Attempt, Merchant, ProviderTransaction, SessionLocal, init_db


CASES = [
    ("512345678901", "Daniel Test", 320000, "success", "2048163759"),
    ("712345678902", "Amina Test", 500000, "failed", "3159274860"),
    ("812345678903", "Tunde Test", 210000, "pending", "4260385971"),
    ("912345678904", "Bisi Test", 250000, "success", "5371496082"),  # receipt can claim ₦3,200: mismatch
]


def seed(merchant_slug: str, hourly: bool = False):
    init_db()
    with SessionLocal.begin() as db:
        merchant = db.scalar(select(Merchant).where(Merchant.slug == merchant_slug))
        if not merchant:
            raise ValueError(f"No merchant named {merchant_slug!r}")
        account = db.scalar(select(Account).where(Account.merchant_id == merchant.id, Account.is_backup.is_(False)))
        backup = db.scalar(select(Account).where(Account.merchant_id == merchant.id, Account.is_backup.is_(True)))
        if not account:
            raise ValueError("Configure the primary account before seeding")
        for reference, name, amount, status, sender_account in CASES:
            if not db.scalar(select(ProviderTransaction.id).where(ProviderTransaction.merchant_id == merchant.id, ProviderTransaction.reference == reference)):
                db.add(ProviderTransaction(merchant_id=merchant.id, account_number=account.account_number, reference=reference, sender_name=name, sender_account_number=sender_account, amount_kobo=amount, status=status))
        if hourly:
            # Exactly 21 distinct attempts: 11 pending/failed, 10 confirmed.
            # The 52% rate crosses the configured >20 / >=50% warning threshold.
            for index in range(21):
                ref = f"{7 if index < 11 else 5}{10000000000 + index:011d}"
                status = "failed" if index < 6 else "pending" if index < 11 else "confirmed"
                if not db.scalar(select(Attempt.id).where(Attempt.merchant_id == merchant.id, Attempt.reference == ref)):
                    db.add(Attempt(merchant_id=merchant.id, account_id=account.id, customer_chat_id=f"demo-{index}", customer_name=f"Demo customer {index + 1}", reference=ref, amount_kobo=100000, status=status, reason="Seeded dashboard demonstration"))
            if backup:
                for index in range(21):
                    ref = f"{5}{20000000000 + index:011d}"
                    if not db.scalar(select(Attempt.id).where(Attempt.merchant_id == merchant.id, Attempt.reference == ref)):
                        db.add(Attempt(merchant_id=merchant.id, account_id=backup.id, customer_chat_id=f"backup-demo-{index}", customer_name=f"Backup demo {index + 1}", reference=ref, amount_kobo=100000, status="confirmed", reason="Seeded healthy backup demonstration"))
    return [case[0] for case in CASES]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merchant", required=True, help="Shop slug shown in the portal URL / database")
    parser.add_argument("--hourly", action="store_true", help="Add 21 invented attempts for the account-health demo")
    args = parser.parse_args()
    print("Seeded references:", ", ".join(seed(args.merchant, args.hourly)))
