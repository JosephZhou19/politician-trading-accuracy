import sqlite3

import pytest

from src.db import models


@pytest.fixture
def conn(tmp_path):
    c = models.connect(tmp_path / "test.db")
    yield c
    c.close()


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
            asset_name="X Corp",
            transaction_type="gift",
            transaction_date="2026-01-01",
            notification_date="2026-01-01",
            amount_low=1,
            owner="self",
        )
