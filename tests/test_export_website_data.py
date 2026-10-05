import pytest

from src.db import models
from scripts.export_website_data import (
    build_exports, clean_comment, export_issuers, export_legislators, fetch_trade_rows,
    _price_change_30d,
)

_filing_counter = [0]


def _insert_trade(
    conn, *, ticker="AAPL", transaction_date, notification_date=None, amount_low=100_001,
    amount_high=250_000, owner="self", asset_type="Stock", transaction_type="purchase",
    legislator=("Nancy", "Pelosi", "house"), asset_name=None, comment=None,
):
    _filing_counter[0] += 1
    leg_id = models.get_or_create_legislator(conn, *legislator, "member")
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber=legislator[2],
        external_filing_id=f"filing-{_filing_counter[0]}",
        filing_type="ptr", is_amendment=False, filing_date=transaction_date,
        source_url="https://example.com", document_format="html", fetched_at="2026-09-05T00:00:00",
    )
    trade_id = models.insert_trade(
        conn, filing_id=filing_id, source_row_number=1, ticker=ticker,
        asset_name=asset_name or f"{ticker} Inc.",
        asset_type=asset_type, transaction_type=transaction_type, transaction_date=transaction_date,
        notification_date=notification_date or transaction_date, amount_low=amount_low,
        amount_high=amount_high, owner=owner, comment=comment,
    )
    return leg_id, trade_id


def test_fetch_trade_rows_excludes_superseded_trades(conn):
    _, old_trade_id = _insert_trade(conn, transaction_date="2024-01-05")
    _, new_trade_id = _insert_trade(conn, transaction_date="2024-01-05")
    models.set_trade_superseded(conn, old_trade_id, new_trade_id)

    rows = fetch_trade_rows(conn)

    assert [r["id"] for r in rows] == [new_trade_id]


def test_build_exports_groups_trades_by_legislator_and_ticker(conn):
    pelosi_id, _ = _insert_trade(conn, ticker="NVDA", transaction_date="2024-01-05")
    _insert_trade(conn, ticker="NVDA", transaction_date="2024-02-05", legislator=("Josh", "Gottheimer", "house"))
    _insert_trade(conn, ticker="MSFT", transaction_date="2024-01-05", legislator=("Nancy", "Pelosi", "house"))

    rows = fetch_trade_rows(conn)
    trades_by_legislator, trades_by_ticker, recent_trades, tickers, latest_prices, price_series = build_exports(
        conn, rows, {pelosi_id: "Nancy Pelosi"},
    )

    assert len(trades_by_legislator[pelosi_id]) == 2  # her NVDA + her MSFT
    assert len(trades_by_ticker["NVDA"]) == 2  # Pelosi's + Gottheimer's
    assert tickers == {"NVDA": "NVDA Inc.", "MSFT": "MSFT Inc."}
    # Gottheimer's row isn't in the name map passed in - falls back to empty, not a crash.
    gottheimer_trade = [t for t in trades_by_ticker["NVDA"] if t["legislator_name"] == ""]
    assert len(gottheimer_trade) == 1


def test_build_exports_computes_price_at_notification_when_history_exists(conn):
    leg_id, _ = _insert_trade(conn, ticker="AAPL", transaction_date="2024-01-05", notification_date="2024-01-10")
    models.set_ticker_daily_prices(conn, "AAPL", [("2024-01-11", 150.0), ("2024-06-01", 200.0)])

    rows = fetch_trade_rows(conn)
    trades_by_legislator, _, _, _, latest_prices, price_series = build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    [trade] = trades_by_legislator[leg_id]
    assert trade["price_at_notification"] == 150.0
    # notification_date + 1 day (2024-01-11) is itself a trading day here, so price_date
    # matches it exactly - the rolled-forward-over-a-gap case is covered separately below.
    assert trade["price_date"] == "2024-01-11"
    assert latest_prices["AAPL"] == {"price": 200.0, "date": "2024-06-01"}
    assert price_series["AAPL"] == [["2024-01-11", 150.0], ["2024-06-01", 200.0]]


def test_build_exports_price_date_reflects_the_actual_rolled_forward_trading_day(conn):
    # notification_date + 1 day is 2024-01-06, a Saturday - the real first price available
    # is the following Monday. price_date must reflect that real day, not the Saturday.
    leg_id, _ = _insert_trade(conn, ticker="AAPL", transaction_date="2024-01-01", notification_date="2024-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2024-01-08", 110.0)])

    rows = fetch_trade_rows(conn)
    trades_by_legislator, _, _, _, _, _ = build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    [trade] = trades_by_legislator[leg_id]
    assert trade["price_date"] == "2024-01-08"
    assert trade["price_at_notification"] == 110.0


def test_build_exports_leaves_price_at_notification_none_without_a_real_ticker(conn):
    leg_id, _ = _insert_trade(conn, ticker=None, asset_type="OT", transaction_date="2024-01-05")

    rows = fetch_trade_rows(conn)
    trades_by_legislator, trades_by_ticker, _, tickers, _, _ = build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    [trade] = trades_by_legislator[leg_id]
    assert trade["price_at_notification"] is None
    assert trades_by_ticker == {}  # no real ticker, nothing to index by ticker
    assert tickers == {}


def test_recent_trades_is_newest_first_and_capped(conn, monkeypatch):
    import scripts.export_website_data as export_module
    monkeypatch.setattr(export_module, "RECENT_TRADES_COUNT", 2)

    leg_id, _ = _insert_trade(conn, ticker="AAPL", transaction_date="2024-01-01", notification_date="2024-01-01")
    _insert_trade(conn, ticker="MSFT", transaction_date="2024-02-01", notification_date="2024-02-01", legislator=("Nancy", "Pelosi", "house"))
    _insert_trade(conn, ticker="NVDA", transaction_date="2024-03-01", notification_date="2024-03-01", legislator=("Nancy", "Pelosi", "house"))

    rows = fetch_trade_rows(conn)
    _, _, recent_trades, _, _, _ = export_module.build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    assert [t["ticker"] for t in recent_trades] == ["NVDA", "MSFT"]


def test_export_legislators_includes_zero_trade_members(conn):
    leg_id = models.get_or_create_legislator(conn, "Ralph", "Abraham", "house", "member")

    legislators = export_legislators(conn, trade_counts={}, metadata={})

    [entry] = [l for l in legislators if l["id"] == leg_id]
    assert entry["trade_count"] == 0


def test_export_legislators_defaults_to_most_trades_first(conn):
    quiet_id = models.get_or_create_legislator(conn, "Ralph", "Abraham", "house", "member")
    active_id = models.get_or_create_legislator(conn, "Nancy", "Pelosi", "house", "member")

    legislators = export_legislators(conn, trade_counts={quiet_id: 0, active_id: 973}, metadata={})

    assert [l["id"] for l in legislators[:1]] == [active_id]
    assert legislators[-1]["id"] == quiet_id


def test_export_legislators_includes_party_and_state_when_matched(conn):
    matched_id = models.get_or_create_legislator(conn, "Nancy", "Pelosi", "house", "member")
    unmatched_id = models.get_or_create_legislator(conn, "Someone", "Unknown", "house", "member")

    legislators = export_legislators(
        conn, trade_counts={}, metadata={matched_id: {"party": "Democrat", "state": "CA"}},
    )

    [matched] = [l for l in legislators if l["id"] == matched_id]
    [unmatched] = [l for l in legislators if l["id"] == unmatched_id]
    assert matched["party"] == "Democrat"
    assert matched["state"] == "CA"
    assert unmatched["party"] is None
    assert unmatched["state"] is None


def test_build_exports_orders_every_list_newest_trade_first(conn):
    leg_id, _ = _insert_trade(conn, ticker="AAPL", transaction_date="2024-01-01", notification_date="2024-01-01")
    _insert_trade(conn, ticker="AAPL", transaction_date="2024-06-01", notification_date="2024-06-01", legislator=("Nancy", "Pelosi", "house"))

    rows = fetch_trade_rows(conn)
    trades_by_legislator, trades_by_ticker, _, _, _, _ = build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    assert [t["notification_date"] for t in trades_by_ticker["AAPL"]] == ["2024-06-01", "2024-01-01"]


def test_clean_comment_strips_the_signature_boilerplate_artifact():
    garbled = (
        "Sale of 230 shares of Apple, Inc. offeringS Signature the statements I have made "
        "on the attached and belief. Mr. Lou Barletta , 10/3/2014"
    )
    assert clean_comment(garbled) == "Sale of 230 shares of Apple, Inc."


def test_clean_comment_leaves_a_clean_comment_untouched():
    assert clean_comment("Sale due to corporate transaction") == "Sale due to corporate transaction"


def test_clean_comment_handles_none_and_empty():
    assert clean_comment(None) is None
    assert clean_comment("") is None


def test_build_exports_surfaces_asset_name_only_for_ticker_less_trades(conn):
    leg_id, _ = _insert_trade(
        conn, ticker=None, asset_type="OT", transaction_date="2024-01-05",
        asset_name="Electronic Arts Inc. (EA)", comment="Sale due to corporate transaction",
    )
    _insert_trade(
        conn, ticker="AAPL", transaction_date="2024-01-06", legislator=("Josh", "Gottheimer", "house"),
    )

    rows = fetch_trade_rows(conn)
    trades_by_legislator, _, _, _, _, _ = build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    [no_ticker_trade] = trades_by_legislator[leg_id]
    assert no_ticker_trade["asset_name"] == "Electronic Arts Inc. (EA)"
    assert no_ticker_trade["comment"] == "Sale due to corporate transaction"


def test_recent_trades_keeps_the_asset_name_for_a_ticker_less_trade(conn):
    """Regression: recent_trades used to unconditionally overwrite asset_name with a
    tickers-dict lookup keyed by trade['ticker'] - for a ticker-less trade that's None, so
    tickers.get(None, "") silently wiped out the asset_name a visitor actually needs to
    make sense of the row (e.g. a corporate-transaction cash-out with no ticker at all)."""
    leg_id, _ = _insert_trade(
        conn, ticker=None, asset_type="OT", transaction_date="2024-01-05",
        asset_name="Electronic Arts Inc. (EA)", comment="Sale due to corporate transaction",
    )

    rows = fetch_trade_rows(conn)
    _, _, recent_trades, _, _, _ = build_exports(conn, rows, {leg_id: "Nancy Pelosi"})

    [trade] = recent_trades
    assert trade["asset_name"] == "Electronic Arts Inc. (EA)"


def test_price_change_30d_compares_against_30_calendar_days_back():
    series = [["2024-01-01", 100.0], ["2024-01-15", 110.0], ["2024-01-31", 120.0]]
    assert _price_change_30d(series) == pytest.approx((120.0 - 100.0) / 100.0)


def test_price_change_30d_none_when_history_is_too_short():
    assert _price_change_30d([["2024-01-31", 120.0]]) is None
    assert _price_change_30d([]) is None


def test_price_change_30d_none_when_all_history_is_within_the_30_day_window():
    """Regression: confirmed live against real exported data - a ticker with only a few
    days of real history (DOMO, TBPH, TOELY all had 2-8 day spans) was silently showing
    that short-window swing mislabeled as a 30-day change, since the lookup for "the price
    on or after the cutoff" happily matched the series' own first point when nothing before
    the cutoff existed at all. A ticker priced for under a month has no real 30-day number
    to show - this must be None, not a disguised 2-day number."""
    series = [["2026-10-01", 100.0], ["2026-10-02", 95.0], ["2026-10-03", 92.0]]
    assert _price_change_30d(series) is None


def test_export_issuers_aggregates_per_ticker(conn):
    leg_a, _ = _insert_trade(
        conn, ticker="NVDA", transaction_date="2024-01-05", notification_date="2024-01-10",
        amount_low=15_001, amount_high=50_000,
    )
    _insert_trade(
        conn, ticker="NVDA", transaction_date="2024-02-05", notification_date="2024-02-10",
        amount_low=50_001, amount_high=100_000, legislator=("Josh", "Gottheimer", "house"),
    )
    models.set_ticker_daily_prices(conn, "NVDA", [("2024-02-10", 500.0)])

    rows = fetch_trade_rows(conn)
    _, trades_by_ticker, _, tickers, latest_prices, price_series = build_exports(
        conn, rows, {leg_a: "Nancy Pelosi"},
    )

    issuers = export_issuers(trades_by_ticker, tickers, latest_prices, price_series)

    [nvda] = [i for i in issuers if i["ticker"] == "NVDA"]
    assert nvda["trade_count"] == 2
    assert nvda["distinct_politicians"] == 2
    assert nvda["last_traded"] == "2024-02-10"
    assert nvda["volume"] == pytest.approx(32_500.5 + 75_000.5)
    assert nvda["current_price"] == 500.0


def test_export_issuers_defaults_to_most_traded_first(conn):
    leg_id, _ = _insert_trade(conn, ticker="NVDA", transaction_date="2024-01-05")
    for i in range(3):
        _insert_trade(conn, ticker="AAPL", transaction_date=f"2024-01-0{i+1}", legislator=("Josh", "Gottheimer", "house"))

    rows = fetch_trade_rows(conn)
    _, trades_by_ticker, _, tickers, latest_prices, price_series = build_exports(
        conn, rows, {leg_id: "Nancy Pelosi"},
    )

    issuers = export_issuers(trades_by_ticker, tickers, latest_prices, price_series)

    assert issuers[0]["ticker"] == "AAPL"  # 3 trades beats NVDA's 1
