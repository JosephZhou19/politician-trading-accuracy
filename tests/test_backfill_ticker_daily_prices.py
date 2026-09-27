import datetime
from unittest.mock import patch

import pytest

from src.db import models
from src.market.prices import TickerHistory
from scripts.backfill_ticker_daily_prices import CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER, backfill_ticker_daily_prices


@pytest.fixture(autouse=True)
def _no_real_delay():
    """REQUEST_DELAY_SECONDS applies once per ticker - without this, the circuit-breaker
    test alone (processing CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER tickers) would really sleep
    for several seconds."""
    with patch("scripts.backfill_ticker_daily_prices.time.sleep"):
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
    h = TickerHistory("TEST", None, price_type="open")
    h.daily_prices = lambda: pairs
    return h


def test_backfill_loads_full_history_for_every_traded_ticker(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    _insert_trade(conn, "MSFT", "2021-03-10")

    with patch(
        "scripts.backfill_ticker_daily_prices.fetch_ticker_history",
        return_value=_fake_history([(datetime.date(2020, 1, 2), 100.0), (datetime.date(2020, 1, 3), 101.0)]),
    ) as mock_fetch:
        summary = backfill_ticker_daily_prices(conn)

    assert summary["tickers_checked"] == 2
    assert summary["days_written"] == 4
    called_tickers = {call.args[0] for call in mock_fetch.call_args_list}
    assert called_tickers == {"AAPL", "MSFT"}

    rows = conn.execute("SELECT ticker, date, price FROM ticker_daily_prices ORDER BY ticker, date").fetchall()
    assert len(rows) == 4
    assert rows[0]["ticker"] == "AAPL" and rows[0]["date"] == "2020-01-02" and rows[0]["price"] == 100.0


def test_backfill_uses_each_tickers_own_earliest_trade_date_as_start(conn):
    _insert_trade(conn, "AAPL", "2015-06-01")

    with patch(
        "scripts.backfill_ticker_daily_prices.fetch_ticker_history",
        return_value=_fake_history([(datetime.date(2015, 6, 1), 50.0)]),
    ) as mock_fetch:
        backfill_ticker_daily_prices(conn)

    called_start = mock_fetch.call_args[0][1]
    assert called_start == datetime.date(2015, 6, 1)


def test_backfill_skips_ticker_already_fully_loaded(conn):
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2020-01-02", 100.0)])

    with patch("scripts.backfill_ticker_daily_prices.fetch_ticker_history") as mock_fetch:
        summary = backfill_ticker_daily_prices(conn)

    mock_fetch.assert_not_called()
    assert summary["tickers_checked"] == 0


def test_backfill_counts_tickers_with_no_yfinance_data(conn):
    _insert_trade(conn, "DEADCO", "2020-01-05")

    with patch("scripts.backfill_ticker_daily_prices.fetch_ticker_history", return_value=None):
        summary = backfill_ticker_daily_prices(conn)

    assert summary["tickers_no_data"] == 1
    assert summary["days_written"] == 0


def test_backfill_resumes_from_cursor_after_time_budget(conn):
    """A run that hits its time budget bookmarks the last completed ticker, so the next run
    picks up right after it instead of restarting from the top."""
    _insert_trade(conn, "AAPL", "2020-01-05")
    _insert_trade(conn, "MSFT", "2020-01-05")

    call_count = 0

    def fake_fetch(ticker, start, end):
        nonlocal call_count
        call_count += 1
        return _fake_history([(datetime.date(2020, 1, 2), 1.0)])

    with patch("scripts.backfill_ticker_daily_prices.fetch_ticker_history", side_effect=fake_fetch):
        with patch("scripts.backfill_ticker_daily_prices.time.monotonic", side_effect=[0, 100, 100]):
            summary = backfill_ticker_daily_prices(conn, time_budget_minutes=1)

    assert summary["tickers_checked"] == 1
    assert models.get_daily_price_backfill_cursor(conn) == "AAPL"


def test_backfill_catches_a_fetch_exception_and_continues(conn):
    """A single ticker's fetch raising (network error, HTTP error, rate-limit response) must
    not crash the whole run - it's treated as no-data-this-attempt, and the next ticker
    still gets processed."""
    _insert_trade(conn, "AAPL", "2020-01-05")
    _insert_trade(conn, "MSFT", "2020-01-05")

    def fake_fetch(ticker, start, end):
        if ticker == "AAPL":
            raise ConnectionError("boom")
        return _fake_history([(datetime.date(2020, 1, 5), 100.0)])

    with patch("scripts.backfill_ticker_daily_prices.fetch_ticker_history", side_effect=fake_fetch):
        summary = backfill_ticker_daily_prices(conn)

    assert summary["tickers_checked"] == 2
    assert summary["tickers_no_data"] == 1
    rows = conn.execute("SELECT ticker FROM ticker_daily_prices").fetchall()
    assert [r["ticker"] for r in rows] == ["MSFT"]


def test_backfill_circuit_breaker_trips_on_a_long_run_of_no_data(conn):
    """CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER real, distinct tickers (each with an actual
    disclosed trade) all coming back empty in a row is the signature of a yfinance
    rate-limit block, not coincidence - the run must stop early rather than mislabeling the
    rest of the ticker universe as permanently dead."""
    tickers = [f"TICK{i:03d}" for i in range(CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER + 5)]
    for t in tickers:
        _insert_trade(conn, t, "2020-01-05")

    with patch("scripts.backfill_ticker_daily_prices.fetch_ticker_history", return_value=None):
        summary = backfill_ticker_daily_prices(conn)

    assert summary["circuit_breaker_tripped"] is True
    assert summary["tickers_checked"] == CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER
    assert summary["tickers_checked"] < len(tickers)


def test_backfill_circuit_breaker_resets_on_a_success(conn):
    """An occasional real success in between misses must reset the streak, so a run with a
    normal, scattered rate of genuinely-dead tickers never trips the breaker."""
    tickers = [f"TICK{i:03d}" for i in range(CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER * 2)]
    for t in tickers:
        _insert_trade(conn, t, "2020-01-05")

    def fake_fetch(ticker, start, end):
        # Every 5th ticker succeeds - the no-data streak never reaches the threshold.
        if int(ticker[4:]) % 5 == 0:
            return _fake_history([(datetime.date(2020, 1, 5), 100.0)])
        return None

    with patch("scripts.backfill_ticker_daily_prices.fetch_ticker_history", side_effect=fake_fetch):
        summary = backfill_ticker_daily_prices(conn)

    assert summary["circuit_breaker_tripped"] is False
    assert summary["tickers_checked"] == len(tickers)
