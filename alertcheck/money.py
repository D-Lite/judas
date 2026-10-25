"""Parse the amounts and account numbers a customer can actually say."""
import re
from decimal import Decimal


_NUMBER = re.compile(
    r"(?P<whole>\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.(?P<frac>\d+))?\s*(?P<suffix>[kKmM])?"
)
_ACCOUNT_LABEL = re.compile(r"^(?:a/c|acct|account)\b[:\s-]*", re.IGNORECASE)


def normalize_name(value: str) -> str:
    collapsed = " ".join(str(value).split())
    if not collapsed:
        raise ValueError("Customer name is required")
    return collapsed[:120]


def names_match(stored: str, claimed: str) -> bool:
    return normalize_name(stored).casefold() == normalize_name(claimed).casefold()


def mask_account(number: str) -> str:
    digits = number or ""
    return f"••••{digits[-4:]}" if digits else ""


def normalize_sender_account(value: str) -> str:
    text = str(value).strip()
    text = _ACCOUNT_LABEL.sub("", text).strip()
    if not text or not re.fullmatch(r"[\d\s-]+", text):
        raise ValueError("Sender account must be one 10-digit number")
    digits = re.sub(r"[\s-]", "", text)
    if len(digits) != 10:
        raise ValueError("Sender account must be one 10-digit number")
    return digits


def parse_amount_to_kobo(raw: object) -> int:
    """Return integer kobo. Whole numbers are naira; a decimal part is kobo."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError("Amount is empty")
    if isinstance(raw, int):
        if raw <= 0:
            raise ValueError("Amount must be greater than zero")
        return raw * 100
    if isinstance(raw, float):
        if raw != raw or raw <= 0:
            raise ValueError("Amount must be greater than zero")
        text = format(Decimal(str(raw)), "f")
    else:
        text = str(raw).strip()
    if not text:
        raise ValueError("Amount is empty")
    return _parse_amount_text(text)


def _parse_amount_text(text: str) -> int:
    if re.search(r"kobo", text, re.IGNORECASE):
        raise ValueError("Say the amount in naira, not kobo")
    if re.search(r"[-–—]", text):
        raise ValueError("Amount looks like a range or a negative number")
    cleaned = text.replace("₦", " ").replace("\u00a0", " ")
    cleaned = re.sub(r"(?<![A-Za-z])(?:naira|ngn)(?![A-Za-z])", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?<![A-Za-z0-9])[Nn](?=\s*\d)", " ", cleaned)
    word_multiplier = 1
    if re.search(r"\bthousand\b", cleaned, re.IGNORECASE):
        word_multiplier = 1_000
        cleaned = re.sub(r"\bthousand\b", " ", cleaned, flags=re.IGNORECASE)
    if re.search(r"\bmillion\b", cleaned, re.IGNORECASE):
        if word_multiplier != 1:
            raise ValueError("Amount has more than one unit")
        word_multiplier = 1_000_000
        cleaned = re.sub(r"\bmillion\b", " ", cleaned, flags=re.IGNORECASE)
    matches = list(_NUMBER.finditer(cleaned))
    if len(matches) != 1:
        raise ValueError("Amount is unclear; say one amount such as 3200 or 3.2k")
    match = matches[0]
    outside = cleaned[: match.start()] + cleaned[match.end() :]
    if re.search(r"[A-Za-z0-9]", outside):
        raise ValueError("Amount is unclear; say one amount such as 3200 or 3.2k")
    suffix = (match.group("suffix") or "").lower()
    if suffix and word_multiplier != 1:
        raise ValueError("Amount has more than one unit")
    frac = match.group("frac") or ""
    if frac and len(frac) > 2 and any(character != "0" for character in frac[2:]):
        raise ValueError("Amount has more than 2 decimal places")
    whole = re.sub(r"[ ,]", "", match.group("whole"))
    number = Decimal(f"{whole}.{frac}" if frac else whole)
    multiplier = {"k": 1_000, "m": 1_000_000}.get(suffix, 1) * word_multiplier
    naira = number * Decimal(multiplier)
    quantized = naira.quantize(Decimal("0.01"))
    if quantized != naira:
        raise ValueError("Amount has more than 2 decimal places")
    if quantized <= 0:
        raise ValueError("Amount must be greater than zero")
    return int(quantized * 100)


def format_naira(amount_kobo: int) -> str:
    naira, kobo = divmod(int(amount_kobo), 100)
    if kobo:
        return f"₦{naira:,}.{kobo:02d}"
    return f"₦{naira:,}"
