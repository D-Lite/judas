import pytest

from alertcheck.money import normalize_sender_account, parse_amount_to_kobo


@pytest.mark.parametrize(
    ("raw", "kobo"),
    [
        (3000, 300000),
        (3200, 320000),
        ("3200", 320000),
        ("3200.00", 320000),
        ("3200.50", 320050),
        ("3,000", 300000),
        ("3,200.50", 320050),
        ("3 000", 300000),
        ("3 200", 320000),
        ("₦3,200", 320000),
        ("NGN 3200", 320000),
        ("N3,200", 320000),
        ("3200 naira", 320000),
        ("naira 3,200", 320000),
        ("3k", 300000),
        ("3K", 300000),
        ("3 k", 300000),
        ("3.2k", 320000),
        ("3.25k", 325000),
        ("2m", 200_000_000),
        ("1.5m", 150_000_000),
        ("3 thousand", 300000),
        ("3.2 thousand", 320000),
        ("2 million", 200_000_000),
        ("3.000", 300),
    ],
)
def test_amount_parser_accepts_spoken_naira(raw, kobo):
    assert parse_amount_to_kobo(raw) == kobo


@pytest.mark.parametrize("raw", ["", "   ", "k", "naira", "3-4k", "3k and 2k", -5, 0, "0", "3200.505", "3k kobo"])
def test_amount_parser_rejects_unclear_amounts(raw):
    with pytest.raises(ValueError):
        parse_amount_to_kobo(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0123456789", "0123456789"),
        ("012 345 6789", "0123456789"),
        ("012-345-6789", "0123456789"),
        ("acct 0123456789", "0123456789"),
        ("account: 0123456789", "0123456789"),
        ("a/c 0123456789", "0123456789"),
    ],
)
def test_sender_account_keeps_leading_zeros(raw, expected):
    assert normalize_sender_account(raw) == expected


@pytest.mark.parametrize("raw", ["123456789", "01234567890", "0123456789 9876543210", "acct 123456789", "no digits"])
def test_sender_account_rejects_anything_other_than_one_nuban(raw):
    with pytest.raises(ValueError):
        normalize_sender_account(raw)
