import datetime
import sqlite3
from unittest.mock import patch

import pytest

from src.db import models


def test_get_or_create_legislator_is_idempotent(conn):
    id1 = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    id2 = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    assert id1 == id2


def test_get_or_create_legislator_strips_whitespace(conn):
    id1 = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    id2 = models.get_or_create_legislator(conn, "  Alan  ", "  Armstrong  ", "senate", "member")
    assert id1 == id2


def test_different_chamber_is_a_different_legislator(conn):
    senate_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    house_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "house", "member")
    assert senate_id != house_id


def test_filing_round_trip(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    assert models.get_filing_by_external_id(conn, "senate", "abc-123") is None

    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-123",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )

    fetched = models.get_filing_by_external_id(conn, "senate", "abc-123")
    assert fetched is not None
    assert fetched.id == filing_id
    assert fetched.is_amendment is False
    assert fetched.parse_status == "pending"


def _insert_test_filing(conn, external_filing_id="abc-123"):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    return models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id=external_filing_id,
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )


def test_set_reconciliation_note_appends_rather_than_overwrites(conn):
    filing_id = _insert_test_filing(conn)
    models.set_reconciliation_note(conn, filing_id, "first note")
    models.set_reconciliation_note(conn, filing_id, "second note")
    note = models.get_filing_by_external_id(conn, "senate", "abc-123").reconciliation_note
    assert "first note" in note
    assert "second note" in note


def test_set_reconciliation_note_does_not_duplicate_same_note(conn):
    filing_id = _insert_test_filing(conn)
    models.set_reconciliation_note(conn, filing_id, "same note")
    models.set_reconciliation_note(conn, filing_id, "same note")
    note = models.get_filing_by_external_id(conn, "senate", "abc-123").reconciliation_note
    assert note.count("same note") == 1


def test_insert_filing_raises_on_duplicate(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    kwargs = dict(
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-123",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    models.insert_filing(conn, **kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        models.insert_filing(conn, **kwargs)


def test_update_filing_parse_status(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-123",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    models.update_filing_parse_status(conn, filing_id, "parsed", parsed_at="2026-09-05T00:01:00")
    fetched = models.get_filing_by_external_id(conn, "senate", "abc-123")
    assert fetched.parse_status == "parsed"
    assert fetched.parsed_at == "2026-09-05T00:01:00"


def test_insert_trade_from_real_armstrong_filing(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="fda235b3-bad7-4637-8fa1-053f354d929c",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/fda235b3-bad7-4637-8fa1-053f354d929c/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )

    trade_id = models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=703,
        ticker="UHS",
        asset_name="Universal Health Services, Inc. Common Stock",
        asset_type="Stock",
        transaction_type="purchase",
        transaction_date="2026-03-27",
        notification_date="2026-07-21",
        amount_low=1001,
        amount_high=15000,
        owner="self",
    )
    assert trade_id is not None

    # A ticker-less ADR from the same filing must not collide with the row above.
    adr_trade_id = models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=700,
        ticker=None,
        asset_name="Recruit Holdings Co Ltd Unsponsored ADR (RCRUY)",
        asset_type="Stock",
        transaction_type="purchase",
        transaction_date="2026-03-30",
        notification_date="2026-07-21",
        amount_low=1001,
        amount_high=15000,
        owner="self",
    )
    assert adr_trade_id is not None
    assert adr_trade_id != trade_id

    trades = models.get_trades_for_filing(conn, filing_id)
    assert len(trades) == 2
    assert {t.ticker for t in trades} == {"UHS", None}


def test_insert_trade_is_idempotent_on_reparse(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-123",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    trade_kwargs = dict(
        filing_id=filing_id,
        source_row_number=1,
        ticker="UHS",
        asset_name="Universal Health Services, Inc. Common Stock",
        asset_type="Stock",
        transaction_type="purchase",
        transaction_date="2026-03-27",
        notification_date="2026-07-21",
        amount_low=1001,
        amount_high=15000,
        owner="self",
    )
    first_id = models.insert_trade(conn, **trade_kwargs)
    second_result = models.insert_trade(conn, **trade_kwargs)
    assert first_id is not None
    assert second_result is None
    assert len(models.get_trades_for_filing(conn, filing_id)) == 1


def test_insert_trade_keeps_distinct_rows_identical_on_every_business_field(conn):
    """Two distinct transactions can be identical on every business field (e.g. two
    dependent children buying the same stock the same day for the same amount). Dedup
    must key on source_row_number, or the second is silently dropped as a "duplicate"."""
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-456",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2016-08-01",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-456/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    identical_fields = dict(
        filing_id=filing_id,
        ticker="WM",
        asset_name="Waste Management, Inc.",
        asset_type="Stock",
        transaction_type="purchase",
        transaction_date="2016-07-07",
        notification_date="2016-08-01",
        amount_low=1001,
        amount_high=15000,
        owner="dependent_child",
        comment=None,
    )
    first_id = models.insert_trade(conn, source_row_number=10, **identical_fields)
    second_id = models.insert_trade(conn, source_row_number=11, **identical_fields)
    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id
    assert len(models.get_trades_for_filing(conn, filing_id)) == 2


def test_insert_trade_from_real_pelosi_filing_point_value_amount(conn):
    leg_id = models.get_or_create_legislator(conn, "Nancy", "Pelosi", "house", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="house",
        external_filing_id="20022320",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2023-01-25",
        source_url="https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2023/20022320.pdf",
        document_format="pdf",
        fetched_at="2026-09-05T00:00:00",
    )

    trade_id = models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=1,
        ticker="RBLX",
        asset_name="Roblox Corporation Class A (RBLX) [OP]",
        asset_type="Stock Option",
        transaction_type="sale_full",
        transaction_date="2023-01-20",
        notification_date="2023-01-20",
        amount_low=1,
        amount_high=1,
        owner="spouse",
        comment="100 call options expired with no value for a total loss of $303,001.",
        raw_row_text="Filing Status: New | Cap. Gains > $200?: unchecked",
    )
    assert trade_id is not None
    trade = models.get_trades_for_filing(conn, filing_id)[0]
    assert trade.amount_low == trade.amount_high == 1
    assert trade.owner == "spouse"


def test_invalid_transaction_type_rejected(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-123",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        models.insert_trade(
            conn,
            filing_id=filing_id,
            source_row_number=1,
            asset_name="X Corp",
            transaction_type="gift",
            transaction_date="2026-01-01",
            notification_date="2026-01-01",
            amount_low=1,
            owner="self",
        )


def _insert_filing_with_trade(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="abc-123",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/abc-123/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=1,
        asset_name="X Corp",
        transaction_type="purchase",
        transaction_date="2026-01-01",
        notification_date="2026-01-01",
        amount_low=1,
        owner="self",
    )
    return filing_id


def test_delete_trades_for_filing_clears_only_that_filing(conn):
    filing_id = _insert_filing_with_trade(conn)
    assert len(models.get_trades_for_filing(conn, filing_id)) == 1

    models.delete_trades_for_filing(conn, filing_id)
    assert len(models.get_trades_for_filing(conn, filing_id)) == 0


def test_delete_trades_for_filing_does_not_touch_other_filings(conn):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_kwargs = dict(
        legislator_id=leg_id,
        chamber="senate",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-07-21",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    filing_a = models.insert_filing(
        conn, external_filing_id="abc-1", source_url="https://x/abc-1/", **filing_kwargs
    )
    filing_b = models.insert_filing(
        conn, external_filing_id="abc-2", source_url="https://x/abc-2/", **filing_kwargs
    )
    trade_kwargs = dict(
        asset_name="X Corp",
        transaction_type="purchase",
        transaction_date="2026-01-01",
        notification_date="2026-01-01",
        amount_low=1,
        owner="self",
    )
    models.insert_trade(conn, filing_id=filing_a, source_row_number=1, **trade_kwargs)
    models.insert_trade(conn, filing_id=filing_b, source_row_number=1, **trade_kwargs)

    models.delete_trades_for_filing(conn, filing_a)
    assert len(models.get_trades_for_filing(conn, filing_a)) == 0
    assert len(models.get_trades_for_filing(conn, filing_b)) == 1


def test_ingestion_run_round_trip(conn):
    run_id = models.start_ingestion_run(conn, "house", "2026-09-05T00:00:00")
    row = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "running"
    assert row["finished_at"] is None

    models.finish_ingestion_run(
        conn,
        run_id,
        finished_at="2026-09-05T01:00:00",
        filings_found=10,
        filings_new=8,
        filings_failed=1,
        status="completed",
    )
    row = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "completed"
    assert row["filings_found"] == 10
    assert row["filings_new"] == 8
    assert row["filings_failed"] == 1
    assert row["finished_at"] == "2026-09-05T01:00:00"


def _make_turso_connection():
    """A _TursoConnection with a fake underlying libsql connection, so the reconnect
    behavior can be tested without a real Turso database."""
    with patch.object(models._TursoConnection, "_new_conn", return_value="fresh-conn"):
        return models._TursoConnection("libsql://fake", "fake-token")


def test_turso_reconnects_once_on_stale_stream():
    conn = _make_turso_connection()
    call_count = {"n": 0}

    def flaky():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("stream not found")
        return "ok"

    with patch.object(conn, "_new_conn", return_value="reconnected-conn") as mock_new_conn, \
            patch.object(models, "logger") as mock_logger:
        result = conn._with_reconnect(flaky)

    assert result == "ok"
    assert call_count["n"] == 2
    mock_new_conn.assert_called_once()
    assert conn._conn == "reconnected-conn"
    mock_logger.warning.assert_called_once()
    assert "reconnecting" in mock_logger.warning.call_args[0][0]


def test_turso_reraises_non_stream_errors():
    conn = _make_turso_connection()

    def always_fails():
        raise ValueError("some other database error")

    with pytest.raises(ValueError, match="some other database error"):
        conn._with_reconnect(always_fails)


def test_turso_logs_slow_call():
    conn = _make_turso_connection()
    with patch("src.db.models.time.monotonic", side_effect=[0.0, 0.6]), \
            patch.object(models, "logger") as mock_logger:
        result = conn._with_reconnect(lambda: "ok")

    assert result == "ok"
    mock_logger.warning.assert_called_once()
    assert "Slow Turso call" in mock_logger.warning.call_args[0][0]


def test_turso_does_not_log_fast_call():
    conn = _make_turso_connection()
    with patch("src.db.models.time.monotonic", side_effect=[0.0, 0.1]), \
            patch.object(models, "logger") as mock_logger:
        result = conn._with_reconnect(lambda: "ok")

    assert result == "ok"
    mock_logger.warning.assert_not_called()


def _insert_priced_trade(conn, ticker, *, source_row_number=1, price_at_transaction=100.0):
    """A trade with real historical price data - the kind that puts its ticker in the
    price-trickle job's work queue."""
    filing_id = _insert_test_filing(conn, external_filing_id=f"filing-{ticker}-{source_row_number}")
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
    if price_at_transaction is not None:
        models.set_trade_prices(conn, trade_id, {"price_at_transaction": price_at_transaction})
    return trade_id


def test_ticker_with_no_price_data_is_excluded_from_price_check_universe(conn):
    _insert_priced_trade(conn, "AAPL")
    _insert_priced_trade(conn, "DEADCO", price_at_transaction=None)  # never priced - known-dead

    due = models.get_tickers_due_for_price_check(conn)

    assert "AAPL" in due
    assert "DEADCO" not in due


def test_new_ticker_is_due_for_a_check(conn):
    _insert_priced_trade(conn, "AAPL")
    assert "AAPL" in models.get_tickers_due_for_price_check(conn)


def test_record_real_price_then_zero_response_does_not_erase_it(conn):
    _insert_priced_trade(conn, "AAPL")
    models.record_real_price(conn, "AAPL", 200.50, "2026-01-01T00:00:00Z")
    models.record_zero_response(conn, "AAPL", "2026-01-02T00:00:00Z")

    tp = models.get_ticker_price(conn, "AAPL")
    assert tp.current_price == 200.50  # frozen, not overwritten by the zero
    assert tp.price_status == "active"
    assert tp.zero_streak == 1


def test_zero_streak_flips_to_delisted_at_threshold(conn):
    _insert_priced_trade(conn, "AAPL")
    for _ in range(models.ZERO_STREAK_DELIST_THRESHOLD - 1):
        models.record_zero_response(conn, "AAPL", "2026-01-01T00:00:00Z")
    assert models.get_ticker_price(conn, "AAPL").price_status == "active"

    models.record_zero_response(conn, "AAPL", "2026-01-15T00:00:00Z")

    tp = models.get_ticker_price(conn, "AAPL")
    assert tp.price_status == "delisted"
    assert tp.zero_streak == models.ZERO_STREAK_DELIST_THRESHOLD


def test_real_price_resets_zero_streak(conn):
    _insert_priced_trade(conn, "AAPL")
    models.record_zero_response(conn, "AAPL", "2026-01-01T00:00:00Z")
    models.record_zero_response(conn, "AAPL", "2026-01-02T00:00:00Z")
    models.record_real_price(conn, "AAPL", 150.0, "2026-01-03T00:00:00Z")

    tp = models.get_ticker_price(conn, "AAPL")
    assert tp.zero_streak == 0
    assert tp.price_status == "active"
    assert tp.current_price == 150.0


def test_delisted_ticker_is_excluded_until_recheck_window(conn):
    just_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _insert_priced_trade(conn, "AAPL")
    for _ in range(models.ZERO_STREAK_DELIST_THRESHOLD):
        models.record_zero_response(conn, "AAPL", just_now)
    assert models.get_ticker_price(conn, "AAPL").price_status == "delisted"

    # Just flagged - not due again immediately.
    assert "AAPL" not in models.get_tickers_due_for_price_check(conn)

    # But a delisted ticker checked too long ago is due again (the monthly safety net).
    conn.execute(
        "UPDATE ticker_prices SET last_checked_at = ? WHERE ticker = ?",
        ("2020-01-01T00:00:00Z", "AAPL"),
    )
    conn.commit()
    assert "AAPL" in models.get_tickers_due_for_price_check(conn)


def test_delisted_ticker_that_trades_again_reactivates(conn):
    _insert_priced_trade(conn, "AAPL")
    for _ in range(models.ZERO_STREAK_DELIST_THRESHOLD):
        models.record_zero_response(conn, "AAPL", "2026-01-01T00:00:00Z")

    models.record_real_price(conn, "AAPL", 42.0, "2026-02-01T00:00:00Z")

    tp = models.get_ticker_price(conn, "AAPL")
    assert tp.price_status == "active"
    assert tp.zero_streak == 0
    assert "AAPL" in models.get_tickers_due_for_price_check(conn)


def test_backfill_delisted_status_labels_only_tickers_with_zero_price_history(conn):
    _insert_priced_trade(conn, "AAPL")
    _insert_priced_trade(conn, "DEADCO", price_at_transaction=None)

    count = models.backfill_delisted_status(conn)

    assert count == 1
    assert models.get_ticker_price(conn, "AAPL") is None
    dead = models.get_ticker_price(conn, "DEADCO")
    assert dead.price_status == "delisted"
    assert dead.current_price is None
    assert dead.last_checked_at is None
    assert dead.zero_streak == models.ZERO_STREAK_DELIST_THRESHOLD


def test_backfill_delisted_status_does_not_touch_or_double_count_existing_rows(conn):
    _insert_priced_trade(conn, "DEADCO", price_at_transaction=None)
    models.backfill_delisted_status(conn)

    # A second run must not re-count or overwrite the row it already labeled.
    second_count = models.backfill_delisted_status(conn)

    assert second_count == 0
    assert models.get_ticker_price(conn, "DEADCO").price_status == "delisted"


def test_backfill_delisted_status_skips_ticker_already_tracked_as_active(conn):
    """A ticker that has zero price history but is already in ticker_prices for some other
    reason (e.g. a manual entry) must not be silently relabeled delisted."""
    _insert_priced_trade(conn, "DEADCO", price_at_transaction=None)
    models.record_real_price(conn, "DEADCO", 5.0, "2026-01-01T00:00:00Z")

    models.backfill_delisted_status(conn)

    assert models.get_ticker_price(conn, "DEADCO").price_status == "active"


def test_trickle_cursor_round_trip(conn):
    assert models.get_trickle_cursor(conn) is None
    models.set_trickle_cursor(conn, "AAPL")
    assert models.get_trickle_cursor(conn) == "AAPL"
    models.set_trickle_cursor(conn, "MSFT")
    assert models.get_trickle_cursor(conn) == "MSFT"


def test_trickle_cursor_can_be_cleared(conn):
    models.set_trickle_cursor(conn, "AAPL")
    models.set_trickle_cursor(conn, None)
    assert models.get_trickle_cursor(conn) is None


def test_tickers_due_for_price_check_are_ordered(conn):
    _insert_priced_trade(conn, "MSFT")
    _insert_priced_trade(conn, "AAPL", source_row_number=2)
    _insert_priced_trade(conn, "GOOG", source_row_number=3)

    assert models.get_tickers_due_for_price_check(conn) == ["AAPL", "GOOG", "MSFT"]


def test_record_functions_can_defer_commit(conn):
    _insert_priced_trade(conn, "AAPL")
    models.record_real_price(conn, "AAPL", 100.0, "2026-01-01T00:00:00Z", commit=False)
    # Uncommitted writes are still visible on the same connection (no separate reader here),
    # so this mainly confirms the call succeeds without raising when commit=False.
    assert models.get_ticker_price(conn, "AAPL").current_price == 100.0
