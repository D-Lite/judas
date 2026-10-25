from alertcheck.receipts import parse_receipt_text


def test_access_style_receipt_fields():
    claim = parse_receipt_text("""Transaction Amount: N3,200.00
Sender: DANIEL TEST
Beneficiary: MAMA BOLA
Account Number: 0123456789
Transaction Reference: 512345678901
Transaction Status: Transfer Request Successful""")
    assert claim.reference == "512345678901"
    assert claim.amount_kobo == 320000
    assert claim.account_number == "0123456789"
    assert claim.sender_name == "DANIEL TEST"
    assert claim.missing == ()


def test_missing_fields_require_clarification():
    claim = parse_receipt_text("Reference Number: 712345678901\nAmount: NGN 5,000")
    assert claim.reference == "712345678901"
    assert claim.amount_kobo == 500000
    assert claim.missing == ("recipient_account",)
