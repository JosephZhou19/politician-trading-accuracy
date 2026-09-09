import sqlite3

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
