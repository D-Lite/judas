from datetime import timedelta

from sqlalchemy import select

from .db import Attempt, utcnow


def account_health(db, account_id: int):
    """Rolling-hour warning for one receiving account.

    Attempts are unique by merchant/reference in the ledger. Pending outcomes may
    later resolve, so this uses each attempt's current status.
    """
    since = utcnow() - timedelta(hours=1)
    rows = db.scalars(select(Attempt).where(Attempt.account_id == account_id, Attempt.created_at >= since)).all()
    total = len(rows)
    bad = sum(attempt.status in {"pending", "failed"} for attempt in rows)
    rate = round(100 * bad / total) if total else 0
    return {"attempts": total, "bad": bad, "rate": rate, "unhealthy": total > 20 and bad * 2 >= total}
