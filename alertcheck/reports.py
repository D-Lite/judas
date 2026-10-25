"""Shop-scoped activity summaries shared by the tool and the dashboard."""
import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .db import Attempt, Customer, utcnow


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def period_start(now: datetime, period: str) -> datetime:
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("Use today, week or month")


def build_report(db, merchant, period: str) -> dict:
    now = utcnow().astimezone(ZoneInfo(merchant.timezone))
    start = as_utc(period_start(now, period))
    rows = db.scalars(select(Attempt).where(Attempt.merchant_id == merchant.id, Attempt.created_at >= start)).all()
    confirmed = [row for row in rows if row.status == "confirmed"]
    customers = db.scalars(select(Customer).where(Customer.merchant_id == merchant.id)).all()
    new_customer_count = 0
    people = []
    for customer in customers:
        confirmed_attempts = [attempt for attempt in customer.attempts if attempt.status == "confirmed"]
        if not confirmed_attempts:
            continue
        first = min(as_utc(attempt.created_at) for attempt in confirmed_attempts)
        if first >= start:
            new_customer_count += 1
        in_period = [attempt for attempt in confirmed_attempts if as_utc(attempt.created_at) >= start]
        if in_period:
            people.append({
                "name": customer.display_name,
                "sender_account_number": customer.sender_account_number,
                "confirmed_count": len(in_period),
                "confirmed_amount_ngn": sum(attempt.amount_kobo for attempt in in_period) / 100,
            })
    people.sort(key=lambda item: (-item["confirmed_amount_ngn"], item["name"]))
    return {
        "period": period,
        "shop": merchant.name,
        "week_start": start.isoformat(),
        "confirmed_count": len(confirmed),
        "confirmed_amount_ngn": sum(row.amount_kobo for row in confirmed) / 100,
        "pending_count": sum(row.status == "pending" for row in rows),
        "failed_count": sum(row.status == "failed" for row in rows),
        "review_count": sum(row.status == "review" for row in rows),
        "new_customer_count": new_customer_count,
        "customers": people,
    }


def format_claim_time(moment: datetime | None, timezone_name: str) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    spoken = moment.astimezone(ZoneInfo(timezone_name or "Africa/Lagos")).strftime("%d %b %Y, %H:%M")
    return spoken[1:] if spoken.startswith("0") else spoken


def csv_bytes(rows: list[list]) -> bytes:
    buffer = io.StringIO()
    buffer.write("\ufeff")
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8")


def attempts_csv(attempts) -> bytes:
    rows = [["date", "customer_name", "sender_account", "reference", "amount_naira", "status", "reason", "claimed_at"]]
    for attempt in attempts:
        rows.append([
            attempt.created_at.isoformat(),
            attempt.customer_name,
            attempt.sender_account_number or "",
            attempt.reference,
            f"{attempt.amount_kobo / 100:.2f}",
            attempt.status,
            attempt.reason or "",
            attempt.claimed_at.isoformat() if attempt.claimed_at else "",
        ])
    return csv_bytes(rows)


def customers_csv(customers) -> bytes:
    rows = [["name", "sender_account", "first_seen", "confirmed_count", "confirmed_total_naira"]]
    for customer in customers:
        confirmed = [attempt for attempt in customer.attempts if attempt.status == "confirmed"]
        rows.append([
            customer.display_name,
            customer.sender_account_number,
            customer.created_at.isoformat(),
            len(confirmed),
            f"{sum(attempt.amount_kobo for attempt in confirmed) / 100:.2f}",
        ])
    return csv_bytes(rows)


def report_csv(report: dict) -> bytes:
    rows = [[
        "record",
        "name",
        "sender_account",
        "confirmed_count",
        "confirmed_amount_ngn",
        "pending_count",
        "failed_count",
        "review_count",
        "new_customer_count",
    ]]
    rows.append([
        "summary",
        "",
        "",
        report["confirmed_count"],
        report["confirmed_amount_ngn"],
        report["pending_count"],
        report["failed_count"],
        report["review_count"],
        report["new_customer_count"],
    ])
    for person in report["customers"]:
        rows.append([
            "person",
            person["name"],
            person["sender_account_number"],
            person["confirmed_count"],
            person["confirmed_amount_ngn"],
            "",
            "",
            "",
            "",
        ])
    return csv_bytes(rows)


def incoming_csv(transfers) -> bytes:
    rows = [["date", "sender_name", "sender_account", "amount_naira", "reference", "checked_status"]]
    for transfer in transfers:
        checked = f"checked: {transfer['attempt_status']}" if transfer.get("attempt_status") else "not checked yet"
        rows.append([
            transfer["occurred_at"].isoformat(),
            transfer["sender_name"],
            transfer.get("sender_account_number") or "",
            f"{transfer['amount_kobo'] / 100:.2f}",
            transfer["reference"],
            checked,
        ])
    return csv_bytes(rows)
