from unittest.mock import patch

from src.db import models
from scripts.update_current_prices import _rotate_to_resume_point, update_current_prices


def test_rotate_with_no_cursor_leaves_order_unchanged():
    assert _rotate_to_resume_point(["AAPL", "GOOG", "MSFT"], None) == ["AAPL", "GOOG", "MSFT"]


def test_rotate_resumes_after_cursor():
    assert _rotate_to_resume_point(["AAPL", "GOOG", "MSFT"], "AAPL") == ["GOOG", "MSFT", "AAPL"]


def test_rotate_wraps_when_cursor_is_the_last_ticker():
    assert _rotate_to_resume_point(["AAPL", "GOOG", "MSFT"], "MSFT") == ["AAPL", "GOOG", "MSFT"]


def test_rotate_handles_a_cursor_no_longer_in_the_list():
    """The bookmarked ticker can drop out of the due set between runs (e.g. it got flagged
    delisted) - resume from the first ticker alphabetically after it rather than crashing."""
    assert _rotate_to_resume_point(["AAPL", "MSFT"], "GOOG") == ["MSFT", "AAPL"]


def _insert_priced_trade(conn, ticker, *, source_row_number=1):
    filing_id = models.insert_filing(
        conn,
        legislator_id=models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member"),
        chamber="senate",
        external_filing_id=f"filing-{ticker}-{source_row_number}",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/x/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    trade_id = models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=source_row_number,
        ticker=ticker,
        asset_name=f"{ticker} Inc.",
        asset_type="Stock",
        transaction_type="purchase",
        transaction_date="2026-01-05",
        notification_date="2026-01-20",
        amount_low=1001,
        owner="self",
    )
    models.set_trade_prices(conn, trade_id, {"price_at_transaction": 100.0})


def _fake_prices(mapping):
    def fetch(tickers, api_key=None):
        for t in tickers:
            yield t, mapping.get(t)
    return fetch


def test_update_current_prices_saves_cursor_at_last_processed_ticker(conn):
    for t in ["AAPL", "GOOG", "MSFT"]:
        _insert_priced_trade(conn, t)

    with patch("scripts.update_current_prices.fetch_current_prices", _fake_prices(
        {"AAPL": 1.0, "GOOG": 2.0, "MSFT": 3.0}
    )):
        update_current_prices(conn, time_budget_minutes=None)

    assert models.get_trickle_cursor(conn) == "MSFT"
    assert models.get_ticker_price(conn, "AAPL").current_price == 1.0
    assert models.get_ticker_price(conn, "MSFT").current_price == 3.0


def test_update_current_prices_stops_at_time_budget_and_resumes_next_call(conn):
    for t in ["AAPL", "GOOG", "MSFT"]:
        _insert_priced_trade(conn, t)

    # time.monotonic() is called once up front (to set the deadline) and once per ticker
    # (to check it) - these values expire the budget right after the first ticker.
    with patch("scripts.update_current_prices.fetch_current_prices", _fake_prices(
        {"AAPL": 1.0, "GOOG": 2.0, "MSFT": 3.0}
    )), patch("scripts.update_current_prices.time.monotonic", side_effect=[0, 100]):
        summary = update_current_prices(conn, time_budget_minutes=1)

    assert summary["tickers_checked"] == 1
    assert models.get_trickle_cursor(conn) == "AAPL"
    assert models.get_ticker_price(conn, "GOOG") is None  # never reached this run

    # Next call resumes after AAPL instead of restarting from the top.
    with patch("scripts.update_current_prices.fetch_current_prices", _fake_prices(
        {"AAPL": 1.0, "GOOG": 2.0, "MSFT": 3.0}
    )):
        update_current_prices(conn, time_budget_minutes=None)

    assert models.get_ticker_price(conn, "GOOG").current_price == 2.0
    assert models.get_ticker_price(conn, "MSFT").current_price == 3.0


class _CommitCountingConn:
    """sqlite3.Connection.commit is a read-only C-level attribute - can't patch.object it
    directly - so this wraps a real connection and forwards everything except commit()."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.commit_calls = 0

    def commit(self):
        self.commit_calls += 1
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_update_current_prices_commits_in_batches_not_per_ticker(conn):
    tickers = [f"T{i:03d}" for i in range(5)]
    for t in tickers:
        _insert_priced_trade(conn, t)

    wrapped = _CommitCountingConn(conn)
    with patch("scripts.update_current_prices.COMMIT_BATCH_SIZE", 2), \
            patch("scripts.update_current_prices.fetch_current_prices", _fake_prices(
                {t: 1.0 for t in tickers}
            )):
        update_current_prices(wrapped, time_budget_minutes=None)

    # 5 tickers at a batch size of 2 -> commits after ticker 2, 4, and once more for the
    # trailing partial batch (ticker 5), plus one for set_trickle_cursor - 4 total, not 5.
    assert wrapped.commit_calls == 4
