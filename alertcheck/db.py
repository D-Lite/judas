import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    whatsapp_number: Mapped[str] = mapped_column(String(24))
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Lagos")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accounts: Mapped[list["Account"]] = relationship(back_populates="merchant")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("merchant_id", "account_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    bank_name: Mapped[str] = mapped_column(String(100))
    account_name: Mapped[str] = mapped_column(String(120))
    account_number: Mapped[str] = mapped_column(String(10))
    is_backup: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    merchant: Mapped[Merchant] = relationship(back_populates="accounts")


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("merchant_id", "reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    customer_chat_id: Mapped[str] = mapped_column(String(120))
    customer_name: Mapped[str] = mapped_column(String(120))
    reference: Mapped[str] = mapped_column(String(80))
    amount_kobo: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(255), default="")
    sender_account_number: Mapped[str | None] = mapped_column(String(10), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    customer: Mapped["Customer | None"] = relationship(back_populates="attempts")


class Customer(Base):
    """A person who has had at least one confirmed payment into this shop."""
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("merchant_id", "sender_account_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    sender_account_number: Mapped[str] = mapped_column(String(10))
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[list[Attempt]] = relationship(back_populates="customer")


class ProviderTransaction(Base):
    """Invented transaction feed records served by the demo provider API."""
    __tablename__ = "provider_transactions"
    __table_args__ = (UniqueConstraint("merchant_id", "reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    account_number: Mapped[str] = mapped_column(String(10), index=True)
    reference: Mapped[str] = mapped_column(String(80), index=True)
    sender_name: Mapped[str] = mapped_column(String(120))
    sender_account_number: Mapped[str | None] = mapped_column(String(10), nullable=True)
    amount_kobo: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[str] = mapped_column(String(24))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformSecret(Base):
    __tablename__ = "platform_secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(String(2048))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def make_engine():
    url = os.getenv("DATABASE_URL", "sqlite:///./alertcheck.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgres://")
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def ensure_columns(bind=None) -> None:
    """Add columns that create_all will not alter onto an existing database."""
    bind = bind or engine
    Base.metadata.create_all(bind)
    claimed_ddl = "TIMESTAMP WITH TIME ZONE" if bind.dialect.name == "postgresql" else "DATETIME"
    additions = (
        ("provider_transactions", "sender_account_number", "VARCHAR(10)"),
        ("attempts", "sender_account_number", "VARCHAR(10)"),
        ("attempts", "customer_id", "INTEGER"),
        ("attempts", "claimed_at", claimed_ddl),
    )
    inspector = inspect(bind)
    with bind.begin() as connection:
        tables = set(inspector.get_table_names())
        for table, column, ddl in additions:
            if table not in tables:
                continue
            present = {item["name"] for item in inspector.get_columns(table)}
            if column not in present:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        if "attempts" in tables:
            connection.execute(text("UPDATE attempts SET claimed_at = COALESCE(updated_at, created_at) WHERE status = 'confirmed' AND claimed_at IS NULL"))


def init_db():
    ensure_columns()
