import datetime
from unittest.mock import patch

import pandas as pd
import pytest

from src.db import models
from src.market.prices import TickerHistory
from scripts.update_ticker_daily_prices import OVERLAP_DAYS, update_ticker_daily_prices


@pytest.fixture(autouse=True)
def _no_real_delay():
    """Every due-ticker test exercises the batch-delay sleep between yfinance calls - skip
    the real wait so this test file doesn't cost a full extra second per test."""
    with patch("scripts.update_ticker_daily_prices.time.sleep"):
        yield


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


def _fake_history(pairs):
    """pairs: list of (date, price), date as a datetime.date or an ISO string. Builds a
    real backing Series (not just a stubbed daily_prices()) so price_on_or_after also
    works correctly - the split-detection check in update_ticker_daily_prices.py calls it."""
    dates = [d if isinstance(d, datetime.date) else datetime.date.fromisoformat(d) for d, _ in pairs]
    prices = [p for _, p in pairs]
    return TickerHistory("TEST", pd.Series(prices, index=pd.Index(dates)))


def test_new_ticker_gets_a_full_single_ticker_fetch(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_history",
        return_value=_fake_history([(datetime.date(2020, 1, 5), 100.0)]),
    ) as mock_fetch, patch("scripts.update_ticker_daily_prices.fetch_ticker_histories_batch") as mock_batch:
        summary = update_ticker_daily_prices(conn)

    mock_batch.assert_not_called()
    mock_fetch.assert_called_once_with("AAPL", datetime.date(2020, 1, 5), datetime.date.today())
    assert summary["new_tickers_backfilled"] == 1
    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'AAPL'").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2020-01-05", 100.0)]


def test_due_ticker_gets_a_cheap_batched_overlap_window_refresh(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2026-09-10", 200.0)])

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch",
        return_value={"AAPL": _fake_history([("2026-09-10", 200.0), ("2026-09-15", 205.0)])},
    ) as mock_batch, patch("scripts.update_ticker_daily_prices.fetch_ticker_history") as mock_fetch:
        summary = update_ticker_daily_prices(conn)

    mock_fetch.assert_not_called()
    mock_batch.assert_called_once()
    called_tickers = mock_batch.call_args[0][0]
    assert called_tickers == ["AAPL"]
    assert summary["due_tickers_refreshed"] == 1
    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'AAPL' ORDER BY date").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2026-09-10", 200.0), ("2026-09-15", 205.0)]


def test_due_ticker_batch_start_date_uses_the_earliest_overlap_window_in_the_batch(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    _insert_trade(conn, "MSFT", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2026-09-01", 1.0)])
    models.set_ticker_daily_prices(conn, "MSFT", [("2026-09-10", 2.0)])

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch",
        return_value={
            "AAPL": _fake_history([("2026-09-01", 1.0)]),
            "MSFT": _fake_history([("2026-09-10", 2.0)]),
        },
    ) as mock_batch:
        update_ticker_daily_prices(conn)

    called_start = mock_batch.call_args[0][1]
    # OVERLAP_DAYS back from the EARLIEST of the two tickers' last-stored dates (2026-09-01)
    assert called_start == datetime.date(2026, 9, 1) - datetime.timedelta(days=OVERLAP_DAYS)


def test_split_detected_triggers_full_refetch_and_replace(conn):
    """The batch window's fetched price at the ticker's own last-stored date disagrees with
    what's on record there by more than the rebase threshold - a split must have moved the
    whole series onto a new basis, so the entire history is replaced, not just topped up."""
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2020-01-05", 100.0), ("2026-09-10", 400.0)])

    # Fresh fetch shows a 4-for-1 split: the same 2026-09-10 date now reads 100.0, not 400.0.
    fresh = _fake_history([("2026-09-01", 98.0), ("2026-09-10", 100.0)])
    full_post_split = _fake_history([("2020-01-05", 25.0), ("2026-09-10", 100.0)])

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch", return_value={"AAPL": fresh}
    ), patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_history", return_value=full_post_split
    ) as mock_full_fetch:
        summary = update_ticker_daily_prices(conn)

    mock_full_fetch.assert_called_once_with("AAPL", datetime.date(2020, 1, 5), datetime.date.today())
    assert summary["tickers_rebased"] == 1
    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'AAPL' ORDER BY date").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2020-01-05", 25.0), ("2026-09-10", 100.0)]


def test_no_rebase_when_prices_still_agree(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2026-09-10", 200.0)])

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch",
        return_value={"AAPL": _fake_history([("2026-09-10", 201.0), ("2026-09-15", 205.0)])},
    ), patch("scripts.update_ticker_daily_prices.fetch_ticker_history") as mock_full_fetch:
        summary = update_ticker_daily_prices(conn)

    mock_full_fetch.assert_not_called()
    assert summary["tickers_rebased"] == 0
    assert summary["due_tickers_refreshed"] == 1


def test_missing_history_from_batch_counts_as_no_data(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2026-09-10", 200.0)])

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch", return_value={"AAPL": None}
    ):
        summary = update_ticker_daily_prices(conn)

    assert summary["tickers_no_data"] == 1


def test_replaces_the_old_finnhub_trickle_by_syncing_ticker_status(conn):
    """The whole point of retiring update_current_prices.py: this job must derive
    ticker_status from the same fetch it uses for ticker_daily_prices, via the same
    record_ticker_seen/record_ticker_missed functions the old Finnhub job used
    (record_real_price/record_zero_response, since renamed)."""
    _insert_trade(conn, "AAPL", "2020-01-05")

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_history",
        return_value=_fake_history([("2020-01-05", 100.0), ("2020-01-06", 102.0)]),
    ):
        update_ticker_daily_prices(conn)

    ts = models.get_ticker_status(conn, "AAPL")
    assert ts.status == "active"
    assert ts.zero_streak == 0


def test_no_data_for_a_new_ticker_increments_zero_streak(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")

    with patch("scripts.update_ticker_daily_prices.fetch_ticker_history", return_value=None):
        update_ticker_daily_prices(conn)

    ts = models.get_ticker_status(conn, "AAPL")
    assert ts.status == "active"  # one miss isn't enough to flip it
    assert ts.zero_streak == 1


def test_no_data_for_a_due_ticker_increments_zero_streak(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2026-09-10", 200.0)])
    models.record_ticker_seen(conn, "AAPL", "2026-09-10T00:00:00+00:00")

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch", return_value={"AAPL": None}
    ):
        update_ticker_daily_prices(conn)

    ts = models.get_ticker_status(conn, "AAPL")
    assert ts.zero_streak == 1
