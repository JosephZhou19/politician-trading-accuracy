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


def test_a_stale_batchmate_does_not_inflate_another_tickers_written_rows(conn):
    """Regression for a real, confirmed-live bug: batch_start is the MIN watermark across the
    whole batch, so a stale ticker (AAPL, watermark from years back) makes the shared fetch
    window span years - but MSFT (watermark just a few days old) must still only get ITS OWN
    overlap window written, not AAPL's entire multi-year gap. Measured live: this fix cut a
    real run's days_written from 1,178,033 to a small fraction of that."""
    _insert_trade(conn, "AAPL", "2020-01-05")
    _insert_trade(conn, "MSFT", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2019-01-01", 1.0)])   # very stale
    models.set_ticker_daily_prices(conn, "MSFT", [("2026-09-08", 2.0)])  # nearly current

    # The shared batch fetch spans from AAPL's stale watermark all the way to today, for BOTH
    # tickers - that's the pre-existing, unavoidable fetch-side behavior this test isn't
    # about; what matters is what gets WRITTEN afterward.
    wide_range = [("2019-01-01", 1.0), ("2022-06-15", 1.5), ("2026-09-08", 2.0), ("2026-09-10", 2.1)]
    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch",
        return_value={"AAPL": _fake_history(wide_range), "MSFT": _fake_history(wide_range)},
    ):
        update_ticker_daily_prices(conn)

    msft_rows = conn.execute(
        "SELECT date FROM ticker_daily_prices WHERE ticker = 'MSFT' ORDER BY date"
    ).fetchall()
    # MSFT's own watermark is 2026-09-08, OVERLAP_DAYS=2 back is 2026-09-06 - only dates on or
    # after that should be written, never AAPL's 2019/2022 history.
    assert [r["date"] for r in msft_rows] == ["2026-09-08", "2026-09-10"]


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


def test_a_confirmed_dead_ticker_is_fast_forwarded_and_not_reprocessed(conn):
    """A ticker already tracked 'active' in ticker_status but with zero stored rows
    (independently confirmed dead by the one-time backfill) must be fast-forwarded to
    'delisted' up front, so it's filtered out of this run's new-tickers pass instead of
    burning another fetch+write on a ticker already known to be gone."""
    _insert_trade(conn, "DEADCO", "2020-01-05")
    models.record_ticker_seen(conn, "DEADCO", "2026-01-01T00:00:00+00:00")  # active, zero price rows

    with patch("scripts.update_ticker_daily_prices.fetch_ticker_history") as mock_fetch:
        update_ticker_daily_prices(conn)

    mock_fetch.assert_not_called()
    ts = models.get_ticker_status(conn, "DEADCO")
    assert ts.status == "delisted"


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


def test_a_history_with_zero_valid_daily_prices_counts_as_a_miss_not_a_refresh(conn):
    """A non-None history whose window happened to filter down to zero valid days (e.g. every
    day NaN/implausible, without the whole ticker failing the batch fetch's own all-NaN check)
    must still be treated as a miss - not counted as due_tickers_refreshed, and must not reset
    the ticker's zero_streak to 0."""
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2026-09-10", 200.0)])
    models.record_ticker_missed(conn, "AAPL", "2026-09-09T00:00:00+00:00")  # zero_streak=1 already

    empty_history = TickerHistory("AAPL", pd.Series([], dtype=float, index=pd.DatetimeIndex([]).date))
    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch", return_value={"AAPL": empty_history}
    ):
        summary = update_ticker_daily_prices(conn)

    assert summary["due_tickers_refreshed"] == 0
    assert summary["tickers_no_data"] == 1
    ts = models.get_ticker_status(conn, "AAPL")
    assert ts.zero_streak == 2  # incremented, not reset to 0


def test_new_tickers_batch_uses_a_bounded_number_of_round_trips_not_one_per_ticker(conn):
    """Same fix extended to the new-tickers loop: it previously did one execute() per price
    row plus a separate status upsert and commit() per ticker. Writes across a group of new
    tickers must now be batched, not scale with ticker count."""
    n = 20
    tickers = [f"N{i}" for i in range(n)]
    for t in tickers:
        _insert_trade(conn, t, "2020-01-05")

    def _fetch_one(ticker, start, end):
        return _fake_history([("2020-01-05", 100.0), ("2020-01-06", 101.0)])

    statement_count = 0

    def _count(sql):
        nonlocal statement_count
        statement_count += 1

    conn.set_trace_callback(_count)
    try:
        with patch("scripts.update_ticker_daily_prices.fetch_ticker_history", side_effect=_fetch_one):
            summary = update_ticker_daily_prices(conn)
    finally:
        conn.set_trace_callback(None)

    assert summary["new_tickers_backfilled"] == n
    assert statement_count < 20


def test_new_tickers_flush_in_groups_not_only_at_the_very_end(conn):
    """A run that hits its time budget partway through the new-tickers pass must not lose
    the writes it already fetched but hadn't flushed yet."""
    tickers = [f"N{i}" for i in range(3)]
    for t in tickers:
        _insert_trade(conn, t, "2020-01-05")

    call_count = 0

    def _fetch_one(ticker, start, end):
        nonlocal call_count
        call_count += 1
        return _fake_history([("2020-01-05", float(call_count))])

    with patch("scripts.update_ticker_daily_prices.fetch_ticker_history", side_effect=_fetch_one), \
         patch("scripts.update_ticker_daily_prices.time.monotonic", side_effect=[0] + [1000] * 10):
        summary = update_ticker_daily_prices(conn, time_budget_minutes=1)

    assert summary["new_tickers_backfilled"] == 1
    rows = conn.execute("SELECT ticker FROM ticker_daily_prices").fetchall()
    assert [r["ticker"] for r in rows] == ["N0"]


def test_rebase_replace_uses_the_bulk_insert_path_not_one_execute_per_row(conn):
    """replace_ticker_daily_prices (the rebase path) must route through the same bulk insert
    as everything else, not set_ticker_daily_prices's per-row loop - a rebase can be years of
    history, not a handful of rows."""
    _insert_trade(conn, "AAPL", "2020-01-05")
    models.set_ticker_daily_prices(conn, "AAPL", [("2020-01-05", 100.0), ("2026-09-10", 400.0)])

    fresh = _fake_history([("2026-09-01", 98.0), ("2026-09-10", 100.0)])
    full_post_split = _fake_history([("2020-01-05", 25.0), ("2026-09-10", 100.0)])

    with patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch", return_value={"AAPL": fresh}
    ), patch(
        "scripts.update_ticker_daily_prices.fetch_ticker_history", return_value=full_post_split
    ), patch(
        "src.db.models.bulk_insert_ticker_daily_prices", wraps=models.bulk_insert_ticker_daily_prices
    ) as mock_bulk, patch(
        "src.db.models.set_ticker_daily_prices", wraps=models.set_ticker_daily_prices
    ) as mock_single:
        update_ticker_daily_prices(conn)

    mock_bulk.assert_called_once()
    mock_single.assert_not_called()


def test_due_tickers_batch_uses_a_bounded_number_of_round_trips_not_one_per_ticker(conn):
    """The actual regression this fix is for: confirmed live that the old per-ticker version
    took ~5-6 Turso round-trips PER due ticker (one rebase-check read, one status upsert, one
    execute() per price row), limiting a 30-minute run to ~94 of ~2,770 due tickers. A batch
    of 20 due tickers must now cost a small, roughly-constant number of conn.execute() calls
    - not one that scales with the ticker count."""
    n = 20
    tickers = [f"T{i}" for i in range(n)]
    for t in tickers:
        _insert_trade(conn, t, "2020-01-05")
        models.set_ticker_daily_prices(conn, t, [("2026-09-10", 100.0)])
        models.record_ticker_seen(conn, t, "2026-09-10T00:00:00+00:00")

    histories = {t: _fake_history([("2026-09-10", 100.0), ("2026-09-15", 105.0)]) for t in tickers}

    statement_count = 0

    def _count(sql):
        nonlocal statement_count
        statement_count += 1

    conn.set_trace_callback(_count)
    try:
        with patch(
            "scripts.update_ticker_daily_prices.fetch_ticker_histories_batch", return_value=histories
        ):
            summary = update_ticker_daily_prices(conn)
    finally:
        conn.set_trace_callback(None)

    assert summary["due_tickers_refreshed"] == n
    # A handful of statements for the whole 20-ticker batch (rebase-check read, bulk price
    # insert, status upsert, plus the run's own setup queries and transaction control) -
    # nowhere near 20x anything, and specifically far below what one-round-trip-per-ticker
    # would require (20+ statements for just the price writes alone, before even counting
    # the rebase/status round trips).
    assert statement_count < 20
