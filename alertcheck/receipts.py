"""Best-effort extraction from Nigerian transfer receipt text and files.

Output is an untrusted claim. The provider-backed verifier decides status.
"""
import io
import re
from dataclasses import dataclass


REFERENCE_LABELS = r"(?:transaction\s+reference|reference\s+(?:number|code|id)|session\s+id|payment\s+reference|reference)"
AMOUNT_LABELS = r"(?:transaction\s+amount|amount\s+(?:transferred|paid|sent)|amount)"
SENDER_LABELS = r"(?:sender|from|payer|account\s+holder)"
RECIPIENT_LABELS = r"(?:beneficiary|receiver|recipient|to)"
ACCOUNT_LABELS = r"(?:credit\s+account|beneficiary\s+account|recipient\s+account|account\s+number)"


@dataclass(frozen=True)
class ReceiptClaim:
    reference: str | None
    amount_kobo: int | None
    sender_name: str | None
    recipient_name: str | None
    account_number: str | None
    raw_text: str
    missing: tuple[str, ...]


def _labeled_line(text: str, labels: str) -> str | None:
    match = re.search(rf"(?im)^\s*{labels}\s*[:#-]?\s*(.*?)\s*$", text)
    return match.group(1).strip() if match and match.group(1).strip() else None


def parse_receipt_text(text: str) -> ReceiptClaim:
    cleaned = text.replace("\u00a0", " ").replace("₦", "NGN ")
    reference_line = _labeled_line(cleaned, REFERENCE_LABELS)
    reference_match = re.search(r"\b\d{10,40}\b", reference_line or "")
    reference = reference_match.group() if reference_match else None
    amount_line = _labeled_line(cleaned, AMOUNT_LABELS)
    amount_match = re.search(r"(?:NGN|N)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:NGN)?\b", amount_line or "", re.I)
    amount_kobo = None
    if amount_match:
        from decimal import Decimal

        amount_kobo = int(Decimal(amount_match.group(1).replace(",", "")) * 100)
    sender = _labeled_line(cleaned, SENDER_LABELS)
    recipient = _labeled_line(cleaned, RECIPIENT_LABELS)
    account_line = _labeled_line(cleaned, ACCOUNT_LABELS)
    account_match = re.search(r"\b\d{10}\b", account_line or "")
    missing = tuple(key for key, value in (("reference", reference), ("amount", amount_kobo), ("recipient_account", account_match)) if not value)
    return ReceiptClaim(reference, amount_kobo, sender, recipient, account_match.group() if account_match else None, text, missing)


def extract_file(data: bytes, media_type: str) -> ReceiptClaim:
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Receipt exceeds the 8 MB limit")
    if media_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > 3:
            raise ValueError("Receipt PDF has too many pages")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return parse_receipt_text(text)
        # Scanned PDFs need rasterization. Do not pretend an empty text layer
        # means there was no transfer; request another image or reference.
        raise ValueError("Scanned PDF has no text layer; send a clear image or type the reference")
    if media_type in {"image/png", "image/jpeg", "image/webp"}:
        from PIL import Image
        import pytesseract

        image = Image.open(io.BytesIO(data))
        image.verify()
        image = Image.open(io.BytesIO(data)).convert("RGB")
        if image.width * image.height > 20_000_000:
            raise ValueError("Receipt image is too large")
        return parse_receipt_text(pytesseract.image_to_string(image))
    raise ValueError("Send a PDF, PNG, JPEG or WebP receipt")
