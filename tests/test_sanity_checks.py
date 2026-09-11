from src.parse.sanity_checks import validate_trades

TRADE = {
    "ticker": "AAPL", "asset_name": "Apple Inc. Common Stock", "asset_type": None,
    "transaction_type": "purchase", "transaction_date": "2022-01-15",
    "notification_date": "2022-02-01", "amount_low": 1001, "amount_high": 15000,
    "owner": "self", "comment": None, "filing_status": "new",
}


def test_clean_trade_has_no_issues():
    assert validate_trades([dict(TRADE)]) == []


def test_amount_low_greater_than_amount_high_flagged():
    trade = dict(TRADE, amount_low=50000, amount_high=15000)
    issues = validate_trades([trade])
    assert any("amount_low" in i and "amount_high" in i for i in issues)


def test_negative_amount_flagged():
    trade = dict(TRADE, amount_low=-100)
    issues = validate_trades([trade])
    assert any("negative amount_low" in i for i in issues)


def test_zero_amount_not_flagged():
    """A bare-cents holding legitimately parses to (0, 0) - not itself suspicious."""
    trade = dict(TRADE, amount_low=0, amount_high=0)
    assert validate_trades([trade]) == []


def test_suspicious_asset_name_flagged():
    for bad_name in ("", "1", "12345"):
        trade = dict(TRADE, asset_name=bad_name)
        issues = validate_trades([trade])
        assert any("asset_name" in i for i in issues), bad_name


def test_notification_predates_transaction_flagged():
    trade = dict(TRADE, transaction_date="2022-06-01", notification_date="2022-01-01")
    issues = validate_trades([trade])
    assert any("predates" in i for i in issues)


def test_large_notification_lag_flagged():
    trade = dict(TRADE, transaction_date="2020-01-01", notification_date="2023-01-01")
    issues = validate_trades([trade])
    assert any("days after transaction_date" in i for i in issues)


def test_small_notification_lag_not_flagged():
    trade = dict(TRADE, transaction_date="2022-01-01", notification_date="2022-02-01")
    assert validate_trades([trade]) == []


def test_date_out_of_plausible_range_flagged():
    trade = dict(TRADE, transaction_date="1975-01-01")
    issues = validate_trades([trade])
    assert any("outside plausible range" in i for i in issues)


def test_missing_dates_not_flagged():
    """Absent/unparseable dates are a separate concern (needs_ocr, NOT NULL at the DB layer) -
    this module only judges dates it can actually parse."""
    trade = dict(TRADE, transaction_date=None, notification_date=None)
    assert validate_trades([trade]) == []


def test_couple_of_identical_rows_not_flagged():
    """Two or three identical rows are a normal, legitimate pattern (e.g. the same stock
    bought the same day for two dependent children) - not itself suspicious."""
    issues = validate_trades([dict(TRADE), dict(TRADE), dict(TRADE)])
    assert not any("identical rows" in i for i in issues)


def test_excessive_identical_rows_flagged():
    issues = validate_trades([dict(TRADE) for _ in range(6)])
    assert any("identical rows" in i for i in issues)


def test_distinct_rows_not_flagged_as_duplicate():
    other = dict(TRADE, asset_name="Microsoft Corporation", transaction_date="2022-03-01")
    issues = validate_trades([dict(TRADE), other])
    assert not any("identical rows" in i for i in issues)


def test_empty_trade_list_has_no_issues():
    assert validate_trades([]) == []
