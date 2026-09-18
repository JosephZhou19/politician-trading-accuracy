"""Tests for scripts/sync_local_mirror.py."""
import pytest

from scripts.sync_local_mirror import (
    _connect_turso,
    sync_append_only,
    sync_filings,
    sync_small_table,
    sync_trades,
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


def _insert_trade(conn, filing_id, *, ticker="AAPL", price_at_transaction=None,
                   transaction_date="2026-01-05"):
    trade_id = models.insert_trade(
        conn, filing_id=filing_id, source_row_number=1, ticker=ticker,
        asset_name=f"{ticker} Inc.", asset_type="Stock", transaction_type="purchase",
        transaction_date=transaction_date, notification_date=transaction_date,
        amount_low=1000, amount_high=1000, owner="self",
    )
    if price_at_transaction is not None:
        models.set_trade_prices(conn, trade_id, {"price_at_transaction": price_at_transaction})
    return trade_id


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


def test_sync_trades_rechecks_pending_prices_and_picks_up_the_backfill(source, conn):
    _, filing_id = _insert_filing(source)
    trade_id = _insert_trade(source, filing_id, price_at_transaction=None)

    sync_append_only(source, conn, "legislators", "id")
    sync_filings(source, conn)
    new1, rechecked1 = sync_trades(source, conn)
    assert new1 == 1
    local_price = conn.execute(
        "SELECT price_at_transaction FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()[0]
    assert local_price is None

    # The real pipeline later backfills the price via yfinance.
    models.set_trade_prices(source, trade_id, {"price_at_transaction": 123.45})
    new2, rechecked2 = sync_trades(source, conn)
    assert new2 == 0
    assert rechecked2 == 1
    local_price = conn.execute(
        "SELECT price_at_transaction FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()[0]
    assert local_price == 123.45


def test_sync_trades_does_not_recheck_a_fully_priced_trade(source, conn):
    _, filing_id = _insert_filing(source)
    # An old enough transaction_date that every horizon (30/90/180/365d) is already due -
    # only genuinely filling in all six price columns makes this "not pending".
    trade_id = _insert_trade(source, filing_id, price_at_transaction=100.0,
                              transaction_date="2020-01-05")
    models.set_trade_prices(source, trade_id, {
        "price_at_notification": 101.0, "price_30d": 102.0, "price_90d": 103.0,
        "price_180d": 104.0, "price_365d": 105.0,
    })
    sync_append_only(source, conn, "legislators", "id")
    sync_filings(source, conn)
    sync_trades(source, conn)

    _, rechecked = sync_trades(source, conn)
    assert rechecked == 0


def test_sync_small_table_mirrors_source_exactly_including_removals(source, conn):
    checked_at = "2026-09-05T00:00:00"
    models.record_real_price(source, "AAPL", 200.0, checked_at)
    models.record_real_price(source, "MSFT", 400.0, checked_at)
    sync_small_table(source, conn, "ticker_prices")
    assert conn.execute("SELECT COUNT(*) FROM ticker_prices").fetchone()[0] == 2

    # A ticker no longer present upstream (however that happened) must disappear locally
    # too - something an id-based incremental sync could never do on its own.
    source.execute("DELETE FROM ticker_prices WHERE ticker = 'MSFT'")
    source.commit()
    sync_small_table(source, conn, "ticker_prices")

    remaining = [r["ticker"] for r in conn.execute("SELECT ticker FROM ticker_prices").fetchall()]
    assert remaining == ["AAPL"]


def test_connect_turso_refuses_without_credentials(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="refusing to sync"):
        _connect_turso()
