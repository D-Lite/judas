"""Merchant-scoped Streamable HTTP tools consumed by Chord."""
import hashlib
import os
from urllib.parse import urlsplit

from sqlalchemy import select
from starlette.responses import PlainTextResponse

from .db import Account, Customer, IntegrationCredential, Merchant, SessionLocal
from .metrics import account_health
from .money import format_naira, mask_account, normalize_name, normalize_sender_account, parse_amount_to_kobo
from .reports import build_report
from .verification import CHANNEL_PLACEHOLDER, Claim, verify_claim

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings


public_url = os.getenv("PUBLIC_BASE_URL")
public_host = urlsplit(public_url).netloc if public_url else os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
allowed_hosts = ["127.0.0.1:*", "localhost:*", "localhost", "testserver"]
if public_host:
    allowed_hosts.append(public_host)
mcp = FastMCP(
    "AlertCheck tools",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=[]),
)


class MerchantBearerGate:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
        if not authorization.startswith("Bearer ac_"):
            await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
            return
        token_hash = hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
        with SessionLocal() as db:
            credential = db.scalar(select(IntegrationCredential).where(IntegrationCredential.token_hash == token_hash))
            if not credential:
                await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
                return
            merchant_id = credential.merchant_id
        # The SDK receives a Starlette request carrying this scope for each tool call.
        # Identity is established by the bearer key, never a model argument.
        scope["alertcheck_merchant_id"] = merchant_id
        await self.inner(scope, receive, send)


def merchant_id_from_context(ctx: Context) -> int:
    request = ctx.request_context.request
    merchant_id = request.scope.get("alertcheck_merchant_id") if request else None
    if not isinstance(merchant_id, int):
        raise ValueError("Merchant authorization is missing")
    return merchant_id


_CHANNEL_HEADERS = (
    "mcp-session-id",
    "x-chord-thread-id",
    "x-chord-conversation-id",
    "x-conversation-id",
    "x-thread-id",
)
_META_ID_KEYS = (
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
    "session_id",
    "sessionId",
    "thread",
    "conversation",
    "session",
)


def _clean_channel_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned == "None" or len(cleaned) > 120:
        return None
    return cleaned


def _channel_id_from_meta(meta: object) -> str | None:
    if meta is None:
        return None
    found: dict[str, object] = {}
    if isinstance(meta, dict):
        found.update(meta)
    else:
        for attr in ("model_extra", "__pydantic_extra__"):
            extra = getattr(meta, attr, None)
            if isinstance(extra, dict):
                found.update(extra)
        for key in _META_ID_KEYS:
            if key not in found:
                value = getattr(meta, key, None)
                if isinstance(value, str):
                    found[key] = value
    for key in _META_ID_KEYS:
        cleaned = _clean_channel_id(found.get(key))
        if cleaned:
            return cleaned
    return None


def customer_chat_id_from_context(ctx: Context) -> str:
    """Use a conversation id only when the HTTP request or MCP metadata carries one."""
    try:
        request_context = ctx.request_context
    except (LookupError, ValueError):
        return CHANNEL_PLACEHOLDER
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None) if request is not None else None
    if headers is not None:
        for name in _CHANNEL_HEADERS:
            cleaned = _clean_channel_id(headers.get(name))
            if cleaned:
                return cleaned
    cleaned = _channel_id_from_meta(getattr(request_context, "meta", None))
    return cleaned or CHANNEL_PLACEHOLDER


@mcp.tool()
def verify_payment(sender_account_number: str, amount: str, customer_name: str | None = None, reference: str | None = None, receiving_account_number: str | None = None, *, ctx: Context) -> dict:
    """Check a payment as soon as you have the account the money was sent from and the amount. Pass the amount as the customer said it. A name is required only for a sender account this shop has not seen confirmed before. If you already know this sender account belongs to a customer, or customer_history shows that account, do not ask their name again. Pass the sender account and the amount. Do not ask for the shop's receiving account on a clear match. Ask which shop account they paid only when this shop has more than one saved account and the transfer is still ambiguous or not in the feed, or when the same sender account and amount match transfers on different shop accounts. accounts_to_ask lists this shop's saved accounts; ask the customer which one they paid, and pass that number back as receiving_account_number. An optional receipt reference can disambiguate. Never ask for a chat ID. Never invent a payment status. If the result is already_claimed, say only the customer_message and do not confirm the transfer again."""
    merchant_id = merchant_id_from_context(ctx)
    try:
        amount_kobo = parse_amount_to_kobo(amount)
        sender = normalize_sender_account(sender_account_number)
        stated_name = normalize_name(customer_name) if customer_name and str(customer_name).strip() else None
        supplied_reference = reference.strip() if isinstance(reference, str) and reference.strip() else None
        receiving = normalize_sender_account(receiving_account_number) if receiving_account_number and str(receiving_account_number).strip() else None
        result = verify_claim(Claim(merchant_id, customer_chat_id_from_context(ctx), sender, amount_kobo, stated_name, supplied_reference, receiving))
    except ValueError as exc:
        return {"status": "review", "message": str(exc), "customer_message": str(exc)}
    spoken = format_naira(amount_kobo)
    ref = result.get("reference")
    ref_bit = f" Ref {ref}." if ref else ""
    if result.get("message"):
        result["customer_message"] = result["message"]
    else:
        result["customer_message"] = {
            "confirmed": f"Payment confirmed: {spoken}.{ref_bit}",
            "pending": f"Your {spoken} transfer is pending. We have not confirmed the credit yet.{ref_bit}",
            "failed": f"This {spoken} transfer was reported failed.{ref_bit}",
            "review": f"We need to review the {spoken} transfer.{ref_bit}",
        }.get(result["status"], result.get("reason", "We need to review this transfer."))
    if result.get("attempt_id"):
        result["merchant_feed_event"] = f"{result['status'].upper()} · {spoken}{ref_bit} Attempt {result['attempt_id']}"
    return result


def _customer_view(customer: Customer, include_transactions: bool) -> dict:
    confirmed = [attempt for attempt in customer.attempts if attempt.status == "confirmed"]
    view = {
        "name": customer.display_name,
        "account": mask_account(customer.sender_account_number),
        "confirmed_count": len(confirmed),
        "confirmed_amount_ngn": sum(attempt.amount_kobo for attempt in confirmed) / 100,
        "first_seen": customer.created_at.isoformat(),
    }
    if include_transactions:
        view["transactions"] = [{"date": attempt.created_at.isoformat(), "amount_ngn": attempt.amount_kobo / 100, "reference": attempt.reference, "status": attempt.status} for attempt in confirmed]
    return view


@mcp.tool()
def customer_history(customer_name: str | None = None, sender_account_number: str | None = None, *, ctx: Context) -> dict:
    """Look up people who have already paid this shop. If this sender account is already a customer, use the stored name and do not ask for their name again. Do not ask the customer for a chat ID."""
    merchant_id = merchant_id_from_context(ctx)
    try:
        sender = normalize_sender_account(sender_account_number) if sender_account_number and str(sender_account_number).strip() else None
        stated_name = normalize_name(customer_name) if customer_name and str(customer_name).strip() else None
    except ValueError as exc:
        return {"status": "review", "message": str(exc)}
    with SessionLocal() as db:
        customers = db.scalars(select(Customer).where(Customer.merchant_id == merchant_id).order_by(Customer.created_at)).all()
        if sender:
            customers = [customer for customer in customers if customer.sender_account_number == sender]
        if stated_name:
            customers = [customer for customer in customers if customer.display_name.casefold() == stated_name.casefold()]
        if sender or stated_name:
            if len(customers) == 1:
                view = _customer_view(customers[0], include_transactions=True)
                view["message"] = "This sender account already belongs to a customer. Do not ask for their name again."
                return view
            return {"customers": [_customer_view(customer, include_transactions=True) for customer in customers], "message": "No customer matched. If this is their first confirmed payment, ask for their name once." if not customers else "More than one customer matched."}
        return {"customers": [_customer_view(customer, include_transactions=False) for customer in customers]}


@mcp.tool()
def get_account_health(ctx: Context) -> dict:
    """Return the rolling-hour status of this merchant's primary receiving account and a proven healthy backup, if available."""
    merchant_id = merchant_id_from_context(ctx)
    with SessionLocal() as db:
        accounts = db.scalars(select(Account).where(Account.merchant_id == merchant_id, Account.is_active.is_(True))).all()
        primary = next((account for account in accounts if not account.is_backup), None)
        backup = next((account for account in accounts if account.is_backup), None)
        if not primary:
            return {"status": "setup_required", "message": "Primary receiving account is not configured"}
        health = account_health(db, primary.id)
        result = {"account": f"{primary.bank_name} ••••{primary.account_number[-4:]}", **health}
        if health["unhealthy"] and backup:
            backup_status = account_health(db, backup.id)
            if backup_status["attempts"] > 20 and not backup_status["unhealthy"]:
                result["backup_recommendation"] = {"bank": backup.bank_name, "account_number": backup.account_number, "account_name": backup.account_name}
            else:
                result["message"] = "Backup configured, but its recent health is not proven. Do not recommend it automatically."
        return result


@mcp.tool()
def report_summary(period: str, ctx: Context) -> dict:
    """Summarize who paid this shop today, this week, or this month. It includes how much each person paid and how many customers were new, so you can answer who paid this week and how much, and how many new customers. period is today, week, or month."""
    merchant_id = merchant_id_from_context(ctx)
    with SessionLocal() as db:
        merchant = db.get(Merchant, merchant_id)
        return build_report(db, merchant, period)


mcp_app = MerchantBearerGate(mcp.streamable_http_app())
