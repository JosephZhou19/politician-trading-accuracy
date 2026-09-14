"""Tests for SPY-benchmarked alpha in politician_yearly_activity (src/db/schema.sql)."""
from src.db import models

_leg_counter = {"n": 0}


def _insert_trade(conn, legislator, ticker, *, transaction_type="purchase",
                   amount_low=1000, amount_high=1000, price_at_transaction=100.0,
                   price_365d=None, transaction_date="2020-01-05"):
    first, last = legislator
    leg_id = models.get_or_create_legislator(conn, first, last, "senate", "member")
    _leg_counter["n"] += 1
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber="senate",
        external_filing_id=f"filing-{_leg_counter['n']}",
        filing_type="ptr", is_amendment=False, filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/x/",
        document_format="html", fetched_at="2026-09-05T00:00:00",
    )
    trade_id = models.insert_trade(
        conn, filing_id=filing_id, source_row_number=1,
        ticker=ticker, asset_name=f"{ticker} Inc.", asset_type="Stock",
        transaction_type=transaction_type, transaction_date=transaction_date,
        notification_date=transaction_date, amount_low=amount_low, amount_high=amount_high,
        owner="self",
    )
    prices = {}
    if price_at_transaction is not None:
        prices["price_at_transaction"] = price_at_transaction
    if price_365d is not None:
        prices["price_365d"] = price_365d
    if prices:
        models.set_trade_prices(conn, trade_id, prices)
    return leg_id, trade_id


def _year_row(conn, legislator_id, year):
    return conn.execute(
        "SELECT * FROM politician_yearly_activity WHERE legislator_id = ? AND year = ?",
        (legislator_id, year),
    ).fetchone()


def test_alpha_positive_when_beating_spy(conn):
    # Trade up 50% over the year; SPY only up 10% over the same window -> alpha = +40%
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-02",
                              price_at_transaction=100.0, price_365d=150.0)
    models.set_benchmark_prices(conn, [("2020-01-02", 100.0), ("2021-01-02", 110.0)])

    row = _year_row(conn, leg_id, 2020)
    assert row["trades_with_alpha_data"] == 1
    assert abs(row["avg_1yr_alpha_pct"] - 40.0) < 1e-9
    assert row["alpha_win_rate_1yr_pct"] == 100.0


def test_alpha_negative_when_underperforming_spy_despite_positive_return(conn):
    # Trade up 12% while SPY was up 20% over the same window -> alpha = -8%, a real
    # underperformance a raw-return-only view would miss entirely.
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-02",
                              price_at_transaction=100.0, price_365d=112.0)
    models.set_benchmark_prices(conn, [("2020-01-02", 100.0), ("2021-01-02", 120.0)])

    row = _year_row(conn, leg_id, 2020)
    assert abs(row["avg_1yr_alpha_pct"] - (-8.0)) < 1e-9
    assert row["alpha_win_rate_1yr_pct"] == 0.0


def test_alpha_weighted_by_dollar_amount(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-02",
                              amount_low=1000, amount_high=1000,
                              price_at_transaction=100.0, price_365d=150.0)  # +50%, alpha +40%
    _insert_trade(conn, ("Alan", "Armstrong"), "MSFT", transaction_date="2020-06-01",
                  amount_low=3000, amount_high=3000,
                  price_at_transaction=100.0, price_365d=95.0)  # -5%, alpha -15% (spy +10%)
    models.set_benchmark_prices(conn, [
        ("2020-01-02", 100.0), ("2021-01-02", 110.0),
        ("2020-06-01", 100.0), ("2021-06-01", 110.0),
    ])

    row = _year_row(conn, leg_id, 2020)
    assert row["trades_with_alpha_data"] == 2
    # weighted: (1000*40% + 3000*-15%) / 4000 = (400 - 450) / 4000 = -1.25%
    assert abs(row["avg_1yr_alpha_pct"] - (-1.25)) < 1e-9


def test_alpha_null_when_no_benchmark_data(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-02",
                              price_at_transaction=100.0, price_365d=150.0)
    # No benchmark_prices rows at all - the raw return still computes, alpha can't.

    row = _year_row(conn, leg_id, 2020)
    assert row["avg_1yr_return_pct"] is not None
    assert row["trades_with_alpha_data"] == 0
    assert row["avg_1yr_alpha_pct"] is None
    assert row["alpha_win_rate_1yr_pct"] is None


def test_alpha_rolls_forward_over_weekend_gap(conn):
    """Confirms the SQL lookup mirrors TickerHistory.price_on_or_after's roll-forward
    behavior - a trade dated on a day with no SPY row should use the next available one."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-04",
                              price_at_transaction=100.0, price_365d=150.0)  # Sat, no SPY row
    # SPY rows only on the following Monday and its +365d Monday equivalent.
    models.set_benchmark_prices(conn, [("2020-01-06", 100.0), ("2021-01-04", 110.0)])

    row = _year_row(conn, leg_id, 2020)
    assert row["trades_with_alpha_data"] == 1
    assert abs(row["avg_1yr_alpha_pct"] - 40.0) < 1e-9
