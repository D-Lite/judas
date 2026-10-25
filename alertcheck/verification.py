"""Match a spoken claim to the shared provider feed and record the attempt."""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import Account, Attempt, Customer, Merchant, ProviderTransaction, SessionLocal, utcnow
from .money import mask_account, names_match, normalize_name
from .reports import format_claim_time


# Stable stand-in when the MCP request has no conversation id. Retries that
# still carry this placeholder keep the id already stored on the attempt.
CHANNEL_PLACEHOLDER = "channel"


@dataclass(frozen=True)
class Claim:
    merchant_id: int
    customer_chat_id: str
    sender_account_number: str
    amount_kobo: int
    customer_name: str | None = None
    reference: str | None = None
    receiving_account_number: str | None = None


def _same_claimant(stored_chat_id: str, incoming_chat_id: str) -> bool:
    if stored_chat_id == incoming_chat_id:
        return True
    return incoming_chat_id == CHANNEL_PLACEHOLDER


def synthetic_reference(merchant_id: int, sender_account: str, amount_kobo: int, name: str) -> str:
    payload = f"{merchant_id}|{sender_account}|{amount_kobo}|{name.casefold()}".encode()
    digits = str(int(hashlib.sha256(payload).hexdigest(), 16))
    return digits[:20] if len(digits) >= 12 else digits.zfill(12)


def _account_choices(accounts: list[Account]) -> tuple[list[dict], list[dict]]:
    masked = [{"bank": account.bank_name, "account": mask_account(account.account_number)} for account in accounts]
    full = [{"bank_name": account.bank_name, "account_number": account.account_number} for account in accounts]
    return masked, full


def _ask_payload(accounts: list[Account], message: str) -> dict:
    masked, full = _account_choices(accounts)
    return {"message": message, "receiving_accounts": masked, "accounts_to_ask": full}


def _utc_iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _already_claimed(db, attempt: Attempt, claim: Claim) -> dict:
    merchant = db.get(Merchant, claim.merchant_id)
    timezone_name = merchant.timezone if merchant and merchant.timezone else "Africa/Lagos"
    moment = attempt.claimed_at or attempt.updated_at or attempt.created_at
    spoken = format_claim_time(moment, timezone_name)
    message = f"This transfer was already claimed on {spoken}."
    result = {"status": "already_claimed", "attempt_id": attempt.id, "reference": attempt.reference, "claimed_at": _utc_iso(moment), "reason": message, "message": message}
    if attempt.sender_account_number == claim.sender_account_number:
        result["customer_name"] = attempt.customer_name
    return result


def _link_customer(db, attempt: Attempt, name: str, sender_account: str) -> None:
    if attempt.status != "confirmed" or attempt.customer_id:
        return
    customer = db.scalar(select(Customer).where(Customer.merchant_id == attempt.merchant_id, Customer.sender_account_number == sender_account))
    if customer is None:
        customer = Customer(merchant_id=attempt.merchant_id, sender_account_number=sender_account, display_name=name)
        nested = db.begin_nested()
        try:
            db.add(customer)
            db.flush()
            nested.commit()
        except IntegrityError:
            nested.rollback()
            customer = db.scalar(select(Customer).where(Customer.merchant_id == attempt.merchant_id, Customer.sender_account_number == sender_account))
    attempt.customer_id = customer.id


def _names_equal(stored: str, claimed: str) -> bool:
    try:
        return names_match(stored, claimed)
    except ValueError:
        return False


def verify_claim(claim: Claim) -> dict:
    if claim.amount_kobo <= 0:
        raise ValueError("A positive NGN amount is required")
    if not claim.customer_chat_id.strip():
        raise ValueError("Customer chat identity is required")
    sender = claim.sender_account_number
    if len(sender) != 10 or not sender.isdigit():
        raise ValueError("Sender account must be one 10-digit number")

    with SessionLocal.begin() as db:
        if claim.reference:
            claimed = db.scalar(select(Attempt).where(Attempt.merchant_id == claim.merchant_id, Attempt.reference == claim.reference, Attempt.status == "confirmed"))
            if claimed:
                return _already_claimed(db, claimed, claim)
        accounts = db.scalars(
            select(Account).where(Account.merchant_id == claim.merchant_id, Account.is_active.is_(True)).order_by(Account.is_backup, Account.id)
        ).all()
        if not accounts:
            return {"status": "review", "message": "This shop has no receiving account yet. Do not confirm.", "reason": "Primary receiving account is not configured"}

        if claim.receiving_account_number:
            selected = [account for account in accounts if account.account_number == claim.receiving_account_number]
            if not selected:
                return {"status": "review", "message": "That receiving account does not belong to this shop. Do not confirm.", "reason": "Receiving account does not belong to this merchant"}
            search_accounts = selected
        else:
            search_accounts = list(accounts)
        search_numbers = {account.account_number for account in search_accounts}

        known = db.scalar(select(Customer).where(Customer.merchant_id == claim.merchant_id, Customer.sender_account_number == sender))
        if known:
            match_name = known.display_name
        elif claim.customer_name:
            match_name = normalize_name(claim.customer_name)
        else:
            return {"status": "review", "message": "Ask for their name once. Do not confirm this payment.", "reason": "Customer name is required for a sender account this shop has not seen confirmed before"}

        rows = db.scalars(select(ProviderTransaction).where(
            ProviderTransaction.merchant_id == claim.merchant_id,
            ProviderTransaction.sender_account_number == sender,
            ProviderTransaction.amount_kobo == claim.amount_kobo,
            ProviderTransaction.account_number.in_(search_numbers),
        )).all()
        rows = [row for row in rows if row.sender_name and _names_equal(row.sender_name, match_name)]
        if claim.reference:
            exact = [row for row in rows if row.reference == claim.reference]
            if exact:
                rows = exact
            else:
                referenced = db.scalar(select(ProviderTransaction).where(ProviderTransaction.merchant_id == claim.merchant_id, ProviderTransaction.reference == claim.reference))
                if referenced is not None:
                    return _save(db, claim, accounts, search_accounts[0], "review", "Name, amount, sender account, or shop account does not match that reference", claim.reference, match_name, ask=False)
                rows = []

        shop_accounts_on_rows = {row.account_number for row in rows}
        if len(rows) > 1 and len(shop_accounts_on_rows) > 1:
            message = "More than one transfer matches on different shop accounts. Ask which shop account they paid into, then pass that number as receiving_account_number. These are the shop's saved accounts."
            return _save(db, claim, accounts, search_accounts[0], "review", "More than one transfer matches; ask which shop account they paid into.", synthetic_reference(claim.merchant_id, sender, claim.amount_kobo, match_name), match_name, ask=True, message=message)
        if len(rows) > 1:
            return _save(db, claim, accounts, search_accounts[0], "review", "More than one transfer matches; ask for the receipt reference.", synthetic_reference(claim.merchant_id, sender, claim.amount_kobo, match_name), match_name, ask=False)
        if len(rows) == 1:
            row = rows[0]
            shop = next(account for account in accounts if account.account_number == row.account_number)
            status, reason = _status_for(row.status)
            return _save(db, claim, accounts, shop, status, reason, row.reference, match_name, ask=False)
        ask = claim.receiving_account_number is None and len(accounts) > 1
        message = "No matching incoming transfer was found."
        if ask:
            message = "No matching incoming transfer was found. This shop has more than one receiving account. Ask which shop account they paid into so the next check can be narrower, then pass that number as receiving_account_number. These are the shop's saved accounts."
        return _save(db, claim, accounts, search_accounts[0], "pending", "No matching incoming transfer was found", synthetic_reference(claim.merchant_id, sender, claim.amount_kobo, match_name), match_name, ask=ask, message=message)


def _status_for(provider_status: str) -> tuple[str, str]:
    if provider_status == "success":
        return "confirmed", "Incoming credit matched the claim"
    if provider_status == "pending":
        return "pending", "Provider has not confirmed the credit"
    if provider_status == "failed":
        return "failed", "Provider reports that the transfer failed"
    return "review", "Unknown provider status"


def _save(db, claim: Claim, accounts: list[Account], shop: Account, status: str, reason: str, reference: str, match_name: str, ask: bool, message: str | None = None) -> dict:
    synthetic = synthetic_reference(claim.merchant_id, claim.sender_account_number, claim.amount_kobo, match_name)
    existing = db.scalar(select(Attempt).where(Attempt.merchant_id == claim.merchant_id, Attempt.reference == reference).with_for_update())
    if existing is None and reference != synthetic:
        prior = db.scalar(select(Attempt).where(Attempt.merchant_id == claim.merchant_id, Attempt.reference == synthetic).with_for_update())
        if prior is not None and prior.sender_account_number in (None, claim.sender_account_number) and prior.amount_kobo == claim.amount_kobo:
            prior.reference = reference
            existing = prior
    if existing:
        if existing.status == "confirmed" or existing.claimed_at is not None:
            return _already_claimed(db, existing, claim)
        sender_differs = bool(existing.sender_account_number) and existing.sender_account_number != claim.sender_account_number
        if not _same_claimant(existing.customer_chat_id, claim.customer_chat_id) or existing.amount_kobo != claim.amount_kobo or sender_differs:
            return {"attempt_id": existing.id, "status": "review", "reason": "Reference was already claimed with different details", "reference": existing.reference, "message": "Reference was already claimed with different details"}
        existing.status = status
        existing.reason = reason
        existing.customer_name = match_name[:120]
        existing.sender_account_number = claim.sender_account_number
        existing.account_id = shop.id
        if status == "confirmed" and existing.claimed_at is None:
            existing.claimed_at = utcnow()
            _link_customer(db, existing, match_name, claim.sender_account_number)
        db.flush()
        return _result(existing.id, existing.status, existing.reason, existing.reference, accounts, ask, message, existing.claimed_at)
    attempt = Attempt(
        merchant_id=claim.merchant_id,
        account_id=shop.id,
        customer_chat_id=claim.customer_chat_id,
        customer_name=match_name[:120],
        sender_account_number=claim.sender_account_number,
        reference=reference,
        amount_kobo=claim.amount_kobo,
        status=status,
        reason=reason,
        claimed_at=utcnow() if status == "confirmed" else None,
    )
    db.add(attempt)
    db.flush()
    if status == "confirmed":
        _link_customer(db, attempt, match_name, claim.sender_account_number)
        db.flush()
    return _result(attempt.id, status, reason, reference, accounts, ask, message, attempt.claimed_at)


def _result(attempt_id: int, status: str, reason: str, reference: str, accounts: list[Account], ask: bool, message: str | None, claimed_at=None) -> dict:
    result = {"attempt_id": attempt_id, "status": status, "reason": reason, "reference": reference}
    if claimed_at is not None:
        result["claimed_at"] = _utc_iso(claimed_at)
    if message:
        result["message"] = message
    if ask:
        result.update(_ask_payload(accounts, message or reason))
    return result
