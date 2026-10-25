import os
import re
import secrets
import unicodedata
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import bcrypt
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from .chord_config import chord_api_key, chord_base_url, chord_key_source, save_chord_api_key
from .db import Account, Attempt, Customer, IntegrationCredential, Merchant, ProviderTransaction, SessionLocal, init_db, utcnow
from .metrics import account_health
from .reports import attempts_csv, build_report, customers_csv, format_claim_time, incoming_csv, report_csv


ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-development-only-change-me")
if os.getenv("RAILWAY_ENVIRONMENT") and SESSION_SECRET == "local-development-only-change-me":
    raise RuntimeError("SESSION_SECRET must be configured for Railway")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if mcp is not None:
        async with mcp.session_manager.run():
            yield
    else:
        yield


app = FastAPI(title="AlertCheck", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=bool(os.getenv("RAILWAY_ENVIRONMENT")), same_site="lax")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

try:
    from .mcp_tools import mcp, mcp_app
except ModuleNotFoundError as exc:
    if exc.name != "mcp" or os.getenv("RAILWAY_ENVIRONMENT"):
        raise
    mcp = None
else:
    app.mount("/mcp", mcp_app)


def csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, token: str):
    if not secrets.compare_digest(request.session.get("csrf", ""), token):
        raise HTTPException(status_code=403, detail="Invalid form token")


async def read_form(request: Request) -> dict[str, str]:
    """Read the portal's small URL-encoded forms without multipart machinery."""
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected an HTML form")
    body = await request.body()
    if len(body) > 8192:
        raise HTTPException(413, "Form too large")
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: entries[-1] for key, entries in values.items()}


def render(request: Request, template: str, **kwargs):
    return templates.TemplateResponse(request, template, {"csrf": csrf(request), **kwargs})


def current_merchant(request: Request, db):
    merchant_id = request.session.get("merchant_id")
    return db.get(Merchant, merchant_id) if isinstance(merchant_id, int) else None


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:64] or "shop"


def normalized_phone(value: str) -> str:
    value = re.sub(r"[\s()-]", "", value)
    if not re.fullmatch(r"(?:\+234|0)[789]\d{9}", value):
        raise ValueError("Enter a Nigerian WhatsApp number, e.g. 08012345678")
    return value


def platform_password() -> str | None:
    value = os.getenv("PLATFORM_ADMIN_PASSWORD", "")
    return value if len(value) >= 16 else None


def platform_page(request: Request, **kwargs):
    if not platform_password():
        raise HTTPException(503, "Set PLATFORM_ADMIN_PASSWORD (at least 16 characters) to enable platform settings")
    if not request.session.get("platform_admin"):
        return render(request, "platform_login.html", **kwargs)
    with SessionLocal() as db:
        source = chord_key_source(db)
    response = render(request, "platform_chord.html", key_source=source, base_url=chord_base_url(), **kwargs)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/platform/chord", response_class=HTMLResponse)
def platform_settings(request: Request):
    return platform_page(request)


@app.post("/platform/login")
async def platform_login(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    expected = platform_password()
    if not expected:
        raise HTTPException(503, "Platform settings are disabled")
    if not secrets.compare_digest(form.get("password", ""), expected):
        return render(request, "platform_login.html", error="Password is incorrect.")
    request.session.clear()
    request.session["platform_admin"] = True
    return RedirectResponse("/platform/chord", status_code=303)


@app.post("/platform/logout")
async def platform_logout(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/platform/chord/key")
async def platform_save_key(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    if not platform_password() or not request.session.get("platform_admin"):
        raise HTTPException(403, "Platform administrator sign-in required")
    try:
        with SessionLocal.begin() as db:
            save_chord_api_key(db, form.get("api_key", ""))
    except ValueError as exc:
        return platform_page(request, error=str(exc))
    return RedirectResponse("/platform/chord?saved=1", status_code=303)


@app.post("/platform/chord/check")
async def platform_check_chord(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    if not platform_password() or not request.session.get("platform_admin"):
        raise HTTPException(403, "Platform administrator sign-in required")
    key = chord_api_key()
    if not key:
        return platform_page(request, error="Add the platform Chord API key first.")
    try:
        response = httpx.get(f"{chord_base_url()}/v1/teammates", headers={"Authorization": f"Bearer {key}"}, timeout=5)
    except httpx.HTTPError:
        return platform_page(request, error="Chord could not be reached. Check the host and try again.")
    if response.status_code == 200:
        return platform_page(request, success="Chord accepted the API key. Teammate access is available; WhatsApp and MCP still need their own checks.")
    if response.status_code == 401:
        return platform_page(request, error="Chord rejected the API key (401). Check or rotate it.")
    if response.status_code == 403:
        return platform_page(request, error="The key lacks teammate:read permission (403).")
    return platform_page(request, error=f"Chord returned HTTP {response.status_code}; no connection was confirmed.")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if request.session.get("merchant_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "home.html")


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return render(request, "signup.html")


@app.post("/signup")
async def signup(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    shop_name, email, password, whatsapp_number = (form.get(key, "") for key in ("shop_name", "email", "password", "whatsapp_number"))
    try:
        phone = normalized_phone(whatsapp_number)
    except ValueError as exc:
        return render(request, "signup.html", error=str(exc), values={"shop_name": shop_name, "email": email, "whatsapp_number": whatsapp_number})
    if len(shop_name.strip()) < 2 or len(password) < 12 or "@" not in email:
        return render(request, "signup.html", error="Use a shop name, valid email and password of at least 12 characters.", values={"shop_name": shop_name, "email": email, "whatsapp_number": whatsapp_number})
    with SessionLocal() as db:
        base_slug = slugify(shop_name)
        slug = base_slug
        while db.scalar(select(Merchant.id).where(Merchant.slug == slug)):
            slug = f"{base_slug}-{secrets.token_hex(2)}"
        merchant = Merchant(name=shop_name.strip(), slug=slug, email=email.strip().lower(), password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(), whatsapp_number=phone)
        db.add(merchant)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return render(request, "signup.html", error="That email already has an account.", values={"shop_name": shop_name, "email": email, "whatsapp_number": whatsapp_number})
        request.session.clear()
        request.session["merchant_id"] = merchant.id
    return RedirectResponse("/setup", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    email, password = form.get("email", ""), form.get("password", "")
    with SessionLocal() as db:
        merchant = db.scalar(select(Merchant).where(Merchant.email == email.strip().lower()))
        if not merchant or not bcrypt.checkpw(password.encode(), merchant.password_hash.encode()):
            return render(request, "login.html", error="Email or password is incorrect.")
        request.session.clear()
        request.session["merchant_id"] = merchant.id
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        accounts = db.scalars(select(Account).where(Account.merchant_id == merchant.id).order_by(Account.is_backup, Account.id)).all()
        has_connection_key = db.scalar(select(IntegrationCredential.id).where(IntegrationCredential.merchant_id == merchant.id)) is not None
        return render(request, "setup.html", merchant=merchant, accounts=accounts, has_connection_key=has_connection_key)


@app.post("/setup/connection-key")
async def issue_connection_key(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    with SessionLocal.begin() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        accounts = db.scalars(select(Account).where(Account.merchant_id == merchant.id).order_by(Account.is_backup, Account.id)).all()
        if not any(not account.is_backup for account in accounts):
            return render(request, "setup.html", merchant=merchant, accounts=accounts, error="Add a primary account before creating a connection key.", has_connection_key=False)
        token = "ac_" + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        credential = db.scalar(select(IntegrationCredential).where(IntegrationCredential.merchant_id == merchant.id))
        if credential:
            credential.token_hash = token_hash
            credential.created_at = utcnow()
        else:
            db.add(IntegrationCredential(merchant_id=merchant.id, token_hash=token_hash))
    return render(request, "setup.html", merchant=merchant, accounts=accounts, has_connection_key=True, connection_key=token)


@app.post("/setup/accounts")
async def add_account(request: Request):
    form = await read_form(request)
    verify_csrf(request, form.get("csrf", ""))
    bank_name, account_name, account_number, role = (form.get(key, "") for key in ("bank_name", "account_name", "account_number", "role"))
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        accounts = db.scalars(select(Account).where(Account.merchant_id == merchant.id)).all()
        if not re.fullmatch(r"\d{10}", account_number) or not bank_name.strip() or not account_name.strip():
            return render(request, "setup.html", merchant=merchant, accounts=accounts, error="Enter a bank, account name and 10-digit account number.")
        if role not in {"primary", "backup"}:
            raise HTTPException(400, "Invalid account role")
        if role == "backup" and not any(not a.is_backup for a in accounts):
            return render(request, "setup.html", merchant=merchant, accounts=accounts, error="Add a primary account before a backup account.")
        if any(a.is_backup == (role == "backup") for a in accounts):
            return render(request, "setup.html", merchant=merchant, accounts=accounts, error=f"This shop already has a {role} account.")
        db.add(Account(merchant_id=merchant.id, bank_name=bank_name.strip(), account_name=account_name.strip(), account_number=account_number, is_backup=role == "backup"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return render(request, "setup.html", merchant=merchant, accounts=accounts, error="That account is already configured.")
    return RedirectResponse("/setup", status_code=303)


def _dashboard_href(tab="activity", status="all", period=None):
    parts = []
    if tab != "activity":
        parts.append(f"tab={tab}")
    if status != "all":
        parts.append(f"status={status}")
    if period:
        parts.append(f"period={period}")
    return "/dashboard" if not parts else "/dashboard?" + "&".join(parts)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        accounts = db.scalars(select(Account).where(Account.merchant_id == merchant.id).order_by(Account.is_backup, Account.id)).all()
        selected_status = request.query_params.get("status", "all")
        if selected_status not in {"all", "confirmed", "pending", "failed", "review"}:
            raise HTTPException(400, "Unknown activity status")
        tab = request.query_params.get("tab", "activity")
        if tab not in {"activity", "customers", "downloads"}:
            raise HTTPException(400, "Unknown dashboard tab")
        period = request.query_params.get("period", "week")
        if period not in {"today", "week", "month"}:
            raise HTTPException(400, "Unknown report period")
        activity_query = select(Attempt).where(Attempt.merchant_id == merchant.id)
        if selected_status != "all":
            activity_query = activity_query.where(Attempt.status == selected_status)
        attempts = db.scalars(activity_query.order_by(Attempt.created_at.desc(), Attempt.id.desc()).limit(40)).all()
        for attempt in attempts:
            attempt.claimed_display = format_claim_time(attempt.claimed_at, merchant.timezone)
        counts = dict(db.execute(select(Attempt.status, func.count()).where(Attempt.merchant_id == merchant.id).group_by(Attempt.status)).all())
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_paid = db.scalar(select(func.coalesce(func.sum(Attempt.amount_kobo), 0)).where(Attempt.merchant_id == merchant.id, Attempt.status == "confirmed", Attempt.created_at >= today)) or 0
        primary = next((a for a in accounts if not a.is_backup), None)
        backup = next((a for a in accounts if a.is_backup), None)
        incoming_rows = db.scalars(select(ProviderTransaction).where(ProviderTransaction.merchant_id == merchant.id).order_by(ProviderTransaction.occurred_at.desc(), ProviderTransaction.id.desc()).limit(8)).all()
        incoming_refs = [row.reference for row in incoming_rows]
        attempt_status = {}
        if incoming_refs:
            matched = db.scalars(select(Attempt).where(Attempt.merchant_id == merchant.id, Attempt.reference.in_(incoming_refs))).all()
            attempt_status = {row.reference: row.status for row in matched}
        incoming = [{"sender_name": row.sender_name, "sender_account_number": row.sender_account_number, "reference": row.reference, "amount_kobo": row.amount_kobo, "occurred_at": row.occurred_at, "attempt_status": attempt_status.get(row.reference)} for row in incoming_rows]
        customers = db.scalars(select(Customer).where(Customer.merchant_id == merchant.id).order_by(Customer.created_at.desc())).all()
        customer_rows = []
        for customer in customers:
            confirmed = [item for item in customer.attempts if item.status == "confirmed"]
            customer_rows.append({"name": customer.display_name, "account_ending": customer.sender_account_number[-4:], "confirmed_count": len(confirmed), "confirmed_amount_kobo": sum(item.amount_kobo for item in confirmed), "first_seen": customer.created_at})
        week = build_report(db, merchant, "week")
        report = week if period == "week" else build_report(db, merchant, period)
        periods = [{"id": name, "label": name.title(), "href": _dashboard_href("downloads", selected_status, name)} for name in ("today", "week", "month")]
        return render(
            request,
            "dashboard.html",
            merchant=merchant,
            accounts=accounts,
            attempts=attempts,
            counts=counts,
            selected_status=selected_status,
            today_paid=today_paid,
            primary=primary,
            backup=backup,
            account_health=account_health(db, primary.id) if primary else None,
            backup_health=account_health(db, backup.id) if backup else None,
            incoming=incoming,
            customers=customer_rows,
            week=week,
            tab=tab,
            period=period,
            report=report,
            periods=periods,
            activity_url=_dashboard_href(status=selected_status),
            customers_url=_dashboard_href("customers", selected_status),
            downloads_url=_dashboard_href("downloads", selected_status, period),
        )


def _csv(filename: str, payload: bytes):
    return Response(content=payload, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/dashboard/attempts.csv")
def download_attempts(request: Request):
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        rows = db.scalars(select(Attempt).where(Attempt.merchant_id == merchant.id).order_by(Attempt.created_at.desc(), Attempt.id.desc())).all()
        payload = attempts_csv(rows)
    return _csv("judas-transactions.csv", payload)


@app.get("/dashboard/customers.csv")
def download_customers(request: Request):
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        rows = db.scalars(select(Customer).where(Customer.merchant_id == merchant.id).order_by(Customer.created_at.desc())).all()
        payload = customers_csv(rows)
    return _csv("judas-customers.csv", payload)


@app.get("/dashboard/report.csv")
def download_report(request: Request):
    period = request.query_params.get("period", "week")
    if period not in {"today", "week", "month"}:
        raise HTTPException(400, "Unknown report period")
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        payload = report_csv(build_report(db, merchant, period))
    return _csv(f"judas-{period}.csv", payload)


@app.get("/dashboard/incoming.csv")
def download_incoming(request: Request):
    with SessionLocal() as db:
        merchant = current_merchant(request, db)
        if not merchant:
            return RedirectResponse("/login", status_code=303)
        rows = db.scalars(select(ProviderTransaction).where(ProviderTransaction.merchant_id == merchant.id).order_by(ProviderTransaction.occurred_at.desc(), ProviderTransaction.id.desc())).all()
        refs = [row.reference for row in rows]
        attempt_status = {}
        if refs:
            matched = db.scalars(select(Attempt).where(Attempt.merchant_id == merchant.id, Attempt.reference.in_(refs))).all()
            attempt_status = {row.reference: row.status for row in matched}
        incoming = [{"sender_name": row.sender_name, "sender_account_number": row.sender_account_number, "reference": row.reference, "amount_kobo": row.amount_kobo, "occurred_at": row.occurred_at, "attempt_status": attempt_status.get(row.reference)} for row in rows]
        payload = incoming_csv(incoming)
    return _csv("judas-incoming.csv", payload)
