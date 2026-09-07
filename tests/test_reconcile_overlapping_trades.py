from src.db import models
from src.ingest.reconcile_overlapping_trades import reconcile_overlapping_trades


def _insert_filing(conn, legislator_id, ext_id, filing_date):
    return models.insert_filing(
        conn,
        legislator_id=legislator_id,
        chamber="senate",
        external_filing_id=ext_id,
        filing_type="ptr",
        is_amendment=False,
        filing_date=filing_date,
        source_url=f"https://efdsearch.senate.gov/search/view/ptr/{ext_id}/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )


def _insert_trade(conn, filing_id, row_num, *, ticker="GILD", asset_name="Gilead Sciences",
                   transaction_type="sale_partial", transaction_date="2016-09-06",
                   amount_low=1001, amount_high=15000, owner="self"):
    return models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=row_num,
        ticker=ticker,
        asset_name=asset_name,
        transaction_type=transaction_type,
        transaction_date=transaction_date,
        notification_date=transaction_date,
        amount_low=amount_low,
        amount_high=amount_high,
        owner=owner,
    )


def test_matching_trade_across_two_filings_is_superseded_by_the_later_one(conn):
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_early = _insert_filing(conn, leg_id, "f-early", "2016-09-09")
    filing_late = _insert_filing(conn, leg_id, "f-late", "2016-10-07")
    early_trade = _insert_trade(conn, filing_early, 1)
    late_trade = _insert_trade(conn, filing_late, 1)

    summary = reconcile_overlapping_trades(conn)
    assert summary == {"groups_checked": 1, "trades_superseded": 1, "groups_tied": 0}

    trades = models.get_trades_for_filing(conn, filing_early)
    assert trades[0].superseded_by_trade_id == late_trade
    assert models.get_trades_for_filing(conn, filing_late)[0].superseded_by_trade_id is None


def test_same_filing_matching_trades_are_never_touched(conn):
    """This is exactly the two-dependent-children case source_row_number already handles -
    reconciliation must never even consider trades within the same filing."""
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_id = _insert_filing(conn, leg_id, "f-1", "2016-07-07")
    t1 = _insert_trade(conn, filing_id, 1, owner="dependent_child")
    t2 = _insert_trade(conn, filing_id, 2, owner="dependent_child")

    summary = reconcile_overlapping_trades(conn)
    assert summary == {"groups_checked": 0, "trades_superseded": 0, "groups_tied": 0}
    assert models.get_trades_for_filing(conn, filing_id)[0].superseded_by_trade_id is None
    assert models.get_trades_for_filing(conn, filing_id)[1].superseded_by_trade_id is None


def test_different_owner_is_not_a_match(conn):
    """Mirrored household trading (self and spouse both trading the same stock, amount,
    day) must never be collapsed - this is the exact false-positive risk that ruled out
    dropping owner from the matching key."""
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_id = _insert_filing(conn, leg_id, "f-1", "2016-09-09")
    _insert_trade(conn, filing_id, 1, owner="self")
    _insert_trade(conn, filing_id, 2, owner="spouse")

    summary = reconcile_overlapping_trades(conn)
    assert summary["trades_superseded"] == 0


def test_tied_filing_dates_are_left_unresolved(conn):
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_a = _insert_filing(conn, leg_id, "f-a", "2016-09-09")
    filing_b = _insert_filing(conn, leg_id, "f-b", "2016-09-09")
    _insert_trade(conn, filing_a, 1)
    _insert_trade(conn, filing_b, 1)

    summary = reconcile_overlapping_trades(conn)
    assert summary == {"groups_checked": 1, "trades_superseded": 0, "groups_tied": 1}
    assert models.get_trades_for_filing(conn, filing_a)[0].superseded_by_trade_id is None
    assert models.get_trades_for_filing(conn, filing_b)[0].superseded_by_trade_id is None


def test_matches_regardless_of_how_far_apart_the_filings_are(conn):
    """Deliberate design decision, not an oversight: no date-proximity gate. A real
    filing's transaction/filing dates can lag by years, so "filed close together" can't
    reliably distinguish a genuine late re-filing from a coincidence - and transaction-date
    range overlap is a no-op (the matched trade's own date is in both filings' ranges by
    construction, so it never actually rejects anything). The match key itself (owner
    pinned to one person, plus ticker/date/type/amount all coinciding) is the validated
    signal - see the module docstring for the real-data evidence."""
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_early = _insert_filing(conn, leg_id, "f-early", "2014-01-01")
    filing_late = _insert_filing(conn, leg_id, "f-late", "2020-12-31")
    _insert_trade(conn, filing_early, 1, transaction_date="2013-12-15")
    late_trade = _insert_trade(conn, filing_late, 1, transaction_date="2013-12-15")

    summary = reconcile_overlapping_trades(conn)
    assert summary["trades_superseded"] == 1
    assert models.get_trades_for_filing(conn, filing_early)[0].superseded_by_trade_id == late_trade


def test_ticker_less_trades_match_on_asset_name(conn):
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_early = _insert_filing(conn, leg_id, "f-early", "2016-09-09")
    filing_late = _insert_filing(conn, leg_id, "f-late", "2016-10-07")
    _insert_trade(conn, filing_early, 1, ticker=None, asset_name="Some Mutual Fund")
    late_trade = _insert_trade(conn, filing_late, 1, ticker=None, asset_name="Some Mutual Fund")

    summary = reconcile_overlapping_trades(conn)
    assert summary["trades_superseded"] == 1
    assert models.get_trades_for_filing(conn, filing_early)[0].superseded_by_trade_id == late_trade


def test_rerun_is_idempotent(conn):
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    filing_early = _insert_filing(conn, leg_id, "f-early", "2016-09-09")
    filing_late = _insert_filing(conn, leg_id, "f-late", "2016-10-07")
    _insert_trade(conn, filing_early, 1)
    _insert_trade(conn, filing_late, 1)

    first = reconcile_overlapping_trades(conn)
    assert first["trades_superseded"] == 1
    second = reconcile_overlapping_trades(conn)
    assert second["trades_superseded"] == 0
