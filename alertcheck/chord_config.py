"""Platform-owned Chord credential: environment wins over encrypted DB storage."""
import base64
import hashlib
import os

from cryptography.fernet import Fernet

from .db import PlatformSecret, SessionLocal


def _cipher() -> Fernet:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32 or secret == "local-development-only-change-me":
        raise ValueError("A long SESSION_SECRET is required to save a Chord key in the portal")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def chord_base_url() -> str:
    url = os.getenv("CHORD_BASE_URL", "https://api.chordhq.xyz").rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("CHORD_BASE_URL must be HTTPS")
    return url


def chord_key_source(db=None) -> str:
    if os.getenv("CHORD_API_KEY", "").strip():
        return "environment"
    if db is not None:
        return "portal" if db.get(PlatformSecret, "chord_api_key") else "missing"
    with SessionLocal() as session:
        return "portal" if session.get(PlatformSecret, "chord_api_key") else "missing"


def chord_api_key(db=None) -> str | None:
    env_key = os.getenv("CHORD_API_KEY", "").strip()
    if env_key:
        return env_key
    if db is None:
        with SessionLocal() as session:
            return chord_api_key(session)
    row = db.get(PlatformSecret, "chord_api_key")
    return _cipher().decrypt(row.encrypted_value.encode()).decode() if row else None


def save_chord_api_key(db, key: str):
    key = key.strip()
    if len(key) < 16 or len(key) > 1500 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("Enter a valid Chord API key (16–1500 printable characters, no spaces)")
    if os.getenv("CHORD_API_KEY", "").strip():
        raise ValueError("CHORD_API_KEY is set in the environment; change it there instead")
    encrypted = _cipher().encrypt(key.encode()).decode()
    row = db.get(PlatformSecret, "chord_api_key")
    if row:
        row.encrypted_value = encrypted
    else:
        db.add(PlatformSecret(name="chord_api_key", encrypted_value=encrypted))
