"""Tests for scripts/sync_local_mirror.py."""
import pytest

from scripts.sync_local_mirror import (
    _connect_turso,
    sync_append_only,
    sync_filings,
    sync_small_table,
    sync_ticker_daily_prices,
)
from src.db import models

_leg_counter = {"n": 0}


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Stands in for Turso - a real, independent local sqlite file. The sync functions
    only ever call .execute()/.fetchall() on it, so any real connection works."""
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    c = models.connect(tmp_path / "source.db")
    yield c
    c.close()


def _insert_filing(conn, *, chamber="senate", parse_status="pending"):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", chamber, "member")
    _leg_counter["n"] += 1
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber=chamber,
        external_filing_id=f"filing-{_leg_counter['n']}",
        filing_type="ptr", is_amendment=False, filing_date="2026-01-05",
        source_url="https://efdsearch.senate.gov/search/view/ptr/x/",
        document_format="html", fetched_at="2026-09-05T00:00:00",
    )
    if parse_status != "pending":
        models.update_filing_parse_status(conn, filing_id, parse_status)
    return leg_id, filing_id


def _insert_trade(conn, filing_id, *, ticker="AAPL", transaction_date="2026-01-05", source_row_number=1):
    return models.insert_trade(
        conn, filing_id=filing_id, source_row_number=source_row_number, ticker=ticker,
        asset_name=f"{ticker} Inc.", asset_type="Stock", transaction_type="purchase",
        transaction_date=transaction_date, notification_date=transaction_date,
        amount_low=1000, amount_high=1000, owner="self",
    )


def test_sync_append_only_pulls_new_rows_then_only_the_delta(source, conn):
    models.get_or_create_legislator(source, "Alan", "Armstrong", "senate", "member")
    models.get_or_create_legislator(source, "Ben", "Cardin", "senate", "member")

    first = sync_append_only(source, conn, "legislators", "id")
    assert first == 2
    assert conn.execute("SELECT COUNT(*) FROM legislators").fetchone()[0] == 2

    models.get_or_create_legislator(source, "Carl", "Davis", "senate", "member")
    second = sync_append_only(source, conn, "legislators", "id")
    assert second == 1
    assert conn.execute("SELECT COUNT(*) FROM legislators").fetchone()[0] == 3


def test_sync_filings_rechecks_pending_status_and_picks_up_the_change(source, conn):
    _, filing_id = _insert_filing(source, parse_status="pending")

    sync_append_only(source, conn, "legislators", "id")
    new1, rechecked1 = sync_filings(source, conn)
    assert new1 == 1
    local_status = conn.execute(
        "SELECT parse_status FROM filings WHERE id = ?", (filing_id,)
    ).fetchone()[0]
    assert local_status == "pending"

    # The real pipeline later finishes parsing it.
    models.update_filing_parse_status(source, filing_id, "parsed")
    new2, rechecked2 = sync_filings(source, conn)
    assert new2 == 0
    assert rechecked2 == 1
    local_status = conn.execute(
        "SELECT parse_status FROM filings WHERE id = ?", (filing_id,)
    ).fetchone()[0]
    assert local_status == "parsed"


def test_sync_filings_rechecks_needs_ocr_and_picks_up_the_review(source, conn):
    # needs_ocr isn't terminal - review_ocr_drafts.py can still flip it to parsed later,
    # via the human OCR-review pass rather than the automated parsers.
    _, filing_id = _insert_filing(source, parse_status="needs_ocr")

    sync_append_only(source, conn, "legislators", "id")
    new1, rechecked1 = sync_filings(source, conn)
    assert new1 == 1
    local_status = conn.execute(
        "SELECT parse_status FROM filings WHERE id = ?", (filing_id,)
    ).fetchone()[0]
    assert local_status == "needs_ocr"

    # review_ocr_drafts.py accepts the draft and marks the filing parsed.
    models.update_filing_parse_status(source, filing_id, "parsed")
    new2, rechecked2 = sync_filings(source, conn)
    assert new2 == 0
    assert rechecked2 == 1
    local_status = conn.execute(
        "SELECT parse_status FROM filings WHERE id = ?", (filing_id,)
    ).fetchone()[0]
    assert local_status == "parsed"


def test_sync_filings_does_not_recheck_once_done(source, conn):
    _insert_filing(source, parse_status="parsed")
    sync_append_only(source, conn, "legislators", "id")
    sync_filings(source, conn)

    _, rechecked = sync_filings(source, conn)
    assert rechecked == 0


def test_sync_trades_pulls_new_rows_then_only_the_delta(source, conn):
    """trades is now plain append-only (see sync_local_mirror.py's module docstring for why
    the old price-recheck logic went away along with the price columns it tracked)."""
    _, filing_id = _insert_filing(source)
    _insert_trade(source, filing_id)

    sync_append_only(source, conn, "legislators", "id")
    sync_filings(source, conn)
    first = sync_append_only(source, conn, "trades", "id")
    assert first == 1
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1

    _insert_trade(source, filing_id, ticker="MSFT", source_row_number=2)
    second = sync_append_only(source, conn, "trades", "id")
    assert second == 1
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


def test_sync_small_table_mirrors_source_exactly_including_removals(source, conn):
    checked_at = "2026-09-05T00:00:00"
    models.record_ticker_seen(source, "AAPL", checked_at)
    models.record_ticker_seen(source, "MSFT", checked_at)
    sync_small_table(source, conn, "ticker_status")
    assert conn.execute("SELECT COUNT(*) FROM ticker_status").fetchone()[0] == 2

    # A ticker no longer present upstream (however that happened) must disappear locally
    # too - something an id-based incremental sync could never do on its own.
    source.execute("DELETE FROM ticker_status WHERE ticker = 'MSFT'")
    source.commit()
    sync_small_table(source, conn, "ticker_status")

    remaining = [r["ticker"] for r in conn.execute("SELECT ticker FROM ticker_status").fetchall()]
    assert remaining == ["AAPL"]


def test_sync_ticker_daily_prices_first_sync_pulls_everything(source, conn):
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 100.0), ("2026-01-03", 101.0)])
    models.set_ticker_daily_prices(source, "MSFT", [("2026-01-02", 200.0)])

    summary = sync_ticker_daily_prices(source, conn)

    assert summary["new_tickers"] == 2
    assert summary["rebased_tickers"] == 0
    assert summary["full_pull_rows"] == 3
    assert summary["incremental_rows"] == 0
    rows = conn.execute("SELECT ticker, date, price FROM ticker_daily_prices ORDER BY ticker, date").fetchall()
    assert [(r["ticker"], r["date"], r["price"]) for r in rows] == [
        ("AAPL", "2026-01-02", 100.0), ("AAPL", "2026-01-03", 101.0), ("MSFT", "2026-01-02", 200.0),
    ]


def test_sync_ticker_daily_prices_only_pulls_the_delta_once_synced(source, conn):
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 100.0)])
    sync_ticker_daily_prices(source, conn)

    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-03", 101.0)])
    summary = sync_ticker_daily_prices(source, conn)

    assert summary["new_tickers"] == 0
    assert summary["rebased_tickers"] == 0
    assert summary["incremental_rows"] == 1
    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'AAPL' ORDER BY date").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2026-01-02", 100.0), ("2026-01-03", 101.0)]


def test_sync_ticker_daily_prices_no_op_when_nothing_changed(source, conn):
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 100.0)])
    sync_ticker_daily_prices(source, conn)

    summary = sync_ticker_daily_prices(source, conn)

    assert summary["new_tickers"] == 0
    assert summary["rebased_tickers"] == 0
    assert summary["incremental_rows"] == 0
    assert summary["full_pull_rows"] == 0


def test_sync_ticker_daily_prices_pulls_full_history_for_a_ticker_that_appears_later(source, conn):
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 100.0)])
    sync_ticker_daily_prices(source, conn)

    models.set_ticker_daily_prices(source, "NVDA", [("2026-01-02", 50.0), ("2026-01-03", 51.0)])
    summary = sync_ticker_daily_prices(source, conn)

    assert summary["new_tickers"] == 1
    assert summary["full_pull_rows"] == 2
    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'NVDA' ORDER BY date").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2026-01-02", 50.0), ("2026-01-03", 51.0)]


def test_sync_ticker_daily_prices_detects_a_split_rebase_and_replaces_the_whole_series(source, conn):
    """Regression: a plain `date > watermark` pull would never notice a split that
    retroactively rewrote an already-synced OLD date's price - the date didn't change, only
    the value stored there did. The rebase check must catch this via the watermark date's
    own price disagreeing, and trigger a full re-pull rather than just an incremental one."""
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 100.0), ("2026-01-03", 400.0)])
    sync_ticker_daily_prices(source, conn)

    # Simulate update_ticker_daily_prices.py's replace_ticker_daily_prices after a 4-for-1
    # split: the whole series gets rewritten, including the already-synced watermark date.
    source.execute("DELETE FROM ticker_daily_prices WHERE ticker = 'AAPL'")
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 25.0), ("2026-01-03", 100.0)])

    summary = sync_ticker_daily_prices(source, conn)

    assert summary["rebased_tickers"] == 1
    assert summary["incremental_rows"] == 0
    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'AAPL' ORDER BY date").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2026-01-02", 25.0), ("2026-01-03", 100.0)]


def test_sync_ticker_daily_prices_does_not_rebase_on_a_normal_small_move(source, conn):
    """A small in-tolerance correction to the watermark date is a DOCUMENTED, accepted gap
    (see sync_ticker_daily_prices's own docstring) - it's below the rebase threshold, and
    the incremental pull only looks strictly past the watermark date, never at it. This
    locks in that known, intentional behavior rather than leaving it silently unverified."""
    models.set_ticker_daily_prices(source, "AAPL", [("2026-01-02", 100.0)])
    sync_ticker_daily_prices(source, conn)

    source.execute(
        "UPDATE ticker_daily_prices SET price = 101.0 WHERE ticker = 'AAPL' AND date = '2026-01-02'"
    )
    summary = sync_ticker_daily_prices(source, conn)

    assert summary["rebased_tickers"] == 0
    local_price = conn.execute(
        "SELECT price FROM ticker_daily_prices WHERE ticker = 'AAPL' AND date = '2026-01-02'"
    ).fetchone()[0]
    assert local_price == 100.0  # NOT 101.0 - the correction does not propagate, by design


def test_connect_turso_refuses_without_credentials(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="refusing to sync"):
        _connect_turso()
