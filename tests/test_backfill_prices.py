import datetime
from unittest.mock import patch

import pandas as pd

from src.db import models
from src.market.prices import TickerHistory
from scripts.backfill_prices import backfill


def _insert_trade(conn, ticker, transaction_date):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber="senate", external_filing_id=f"filing-{ticker}-{transaction_date}",
        filing_type="ptr", is_amendment=False, filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/x/",
        document_format="html", fetched_at="2026-09-05T00:00:00",
    )
    return models.insert_trade(
        conn, filing_id=filing_id, source_row_number=1, ticker=ticker, asset_name=f"{ticker} Inc.",
        asset_type="Stock", transaction_type="purchase", transaction_date=transaction_date,
        notification_date=transaction_date, amount_low=1000, owner="self",
    )


def _history(dates_and_prices):
    dates = [datetime.date(*d) for d, _ in dates_and_prices]
    prices = [p for _, p in dates_and_prices]
    return TickerHistory("TEST", pd.Series(prices, index=pd.Index(dates)))


def test_backfill_rewrites_already_set_prices_when_a_split_moves_the_basis(conn):
    """Regression: yfinance retroactively rewrites a ticker's entire history when a stock
    splits. If price_at_transaction was stored before a split and price_365d is fetched
    after, the two columns end up on different bases - a fake multi-x gain with no real
    market cause. The fix must detect the basis mismatch and rewrite every already-set
    column from the fresh history, not just fill in the missing one."""
    trade_id = _insert_trade(conn, "SPLITCO", "2024-01-02")
    # Pre-split basis: $100 stored a year ago.
    models.set_trade_prices(conn, trade_id, {"price_at_transaction": 100.0, "price_90d": 98.0})

    # Fresh history is post-split (2-for-1): the same 2024-01-02 date now reads $50.
    fresh = _history([
        ((2024, 1, 2), 50.0),
        ((2024, 4, 1), 49.0),  # price_90d's date, post-split
        ((2025, 1, 1), 55.0),
        ((2025, 1, 2), 55.0),
    ])

    with patch("scripts.backfill_prices.fetch_ticker_history", return_value=fresh):
        summary = backfill(conn)

    row = conn.execute("SELECT price_at_transaction, price_90d FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["price_at_transaction"] == 50.0
    assert row["price_90d"] == 49.0
    assert summary["trades_rebased"] == 1


def test_backfill_does_not_rebase_when_prices_still_agree(conn):
    trade_id = _insert_trade(conn, "STABLECO", "2024-01-02")
    models.set_trade_prices(conn, trade_id, {"price_at_transaction": 100.0})

    fresh = _history([((2024, 1, 2), 101.0), ((2024, 1, 3), 102.0)])

    with patch("scripts.backfill_prices.fetch_ticker_history", return_value=fresh):
        summary = backfill(conn)

    row = conn.execute("SELECT price_at_transaction FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["price_at_transaction"] == 100.0  # untouched - already-set and still agrees
    assert summary["trades_rebased"] == 0
