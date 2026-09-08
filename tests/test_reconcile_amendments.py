from src.db import models
from src.ingest.reconcile_amendments import reconcile_senate_amendments


def _insert_filing(conn, legislator_id, ext_id, *, is_amendment, filing_date, nominal_date,
                    filed_at=None, amendment_number=None):
    return models.insert_filing(
        conn,
        legislator_id=legislator_id,
        chamber="senate",
        external_filing_id=ext_id,
        filing_type="ptr",
        is_amendment=is_amendment,
        filing_date=filing_date,
        nominal_date=nominal_date,
        filed_at=filed_at,
        amendment_number=amendment_number,
        source_url=f"https://efdsearch.senate.gov/search/view/ptr/{ext_id}/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )


def _insert_trade(conn, filing_id, row_num, *, ticker, transaction_date="2025-11-20",
                   transaction_type="sale_partial", amount_low=1001, amount_high=15000,
                   owner="joint"):
    return models.insert_trade(
        conn,
        filing_id=filing_id,
        source_row_number=row_num,
        ticker=ticker,
        asset_name=f"{ticker} Corp",
        transaction_type=transaction_type,
        transaction_date=transaction_date,
        notification_date=transaction_date,
        amount_low=amount_low,
        amount_high=amount_high,
        owner=owner,
    )


def test_simple_original_with_one_amendment_supersedes_original(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(
        conn, leg_id, "orig-1", is_amendment=False,
        filing_date="2025-12-08", nominal_date="2025-12-08",
    )
    amendment = _insert_filing(
        conn, leg_id, "amend-1", is_amendment=True,
        filing_date="2026-08-24", nominal_date="2025-12-08",
    )

    summary = reconcile_senate_amendments(conn)
    assert summary == {
        "groups_with_amendments": 1, "filings_superseded": 1,
        "groups_ambiguous": 0, "groups_resolved_by_content": 0, "chains_tied": 0,
    }

    original_row = models.get_filing_by_external_id(conn, "senate", "orig-1")
    amendment_row = models.get_filing_by_external_id(conn, "senate", "amend-1")
    assert original_row.superseded_by_filing_id == amendment
    assert amendment_row.superseded_by_filing_id is None


def test_chain_of_two_amendments_only_latest_is_current(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(
        conn, leg_id, "orig-1", is_amendment=False,
        filing_date="2025-12-08", nominal_date="2025-12-08",
    )
    amend1 = _insert_filing(
        conn, leg_id, "amend-1", is_amendment=True,
        filing_date="2026-01-01", nominal_date="2025-12-08",
    )
    amend2 = _insert_filing(
        conn, leg_id, "amend-2", is_amendment=True,
        filing_date="2026-08-24", nominal_date="2025-12-08",
    )

    reconcile_senate_amendments(conn)

    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amend2
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id == amend2
    assert models.get_filing_by_external_id(conn, "senate", "amend-2").superseded_by_filing_id is None


def test_two_originals_same_date_no_amendment_is_left_alone(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    _insert_filing(conn, leg_id, "orig-a", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    _insert_filing(conn, leg_id, "orig-b", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")

    summary = reconcile_senate_amendments(conn)
    assert summary == {
        "groups_with_amendments": 0, "filings_superseded": 0,
        "groups_ambiguous": 0, "groups_resolved_by_content": 0, "chains_tied": 0,
    }
    assert models.get_filing_by_external_id(conn, "senate", "orig-a").superseded_by_filing_id is None
    assert models.get_filing_by_external_id(conn, "senate", "orig-b").superseded_by_filing_id is None


def test_ambiguous_group_is_flagged_not_guessed(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    _insert_filing(conn, leg_id, "orig-a", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    _insert_filing(conn, leg_id, "orig-b", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2026-08-24", nominal_date="2025-12-08")

    summary = reconcile_senate_amendments(conn)
    assert summary == {
        "groups_with_amendments": 1, "filings_superseded": 0,
        "groups_ambiguous": 1, "groups_resolved_by_content": 0, "chains_tied": 0,
    }

    for ext_id in ("orig-a", "orig-b", "amend-1"):
        assert models.get_filing_by_external_id(conn, "senate", ext_id).superseded_by_filing_id is None

    amendment_row = models.get_filing_by_external_id(conn, "senate", "amend-1")
    assert amendment_row.reconciliation_note is not None
    assert "ambiguous" in amendment_row.reconciliation_note


def test_rerun_is_idempotent_and_picks_up_new_amendment(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    amend1 = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2026-01-01", nominal_date="2025-12-08")

    first_summary = reconcile_senate_amendments(conn)
    assert first_summary["filings_superseded"] == 1
    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amend1

    # a re-run with nothing new should be a no-op
    second_summary = reconcile_senate_amendments(conn)
    assert second_summary["filings_superseded"] == 0

    # a later amendment arrives; the current "head" (amend-1) should now be superseded too
    amend2 = _insert_filing(conn, leg_id, "amend-2", is_amendment=True, filing_date="2026-08-24", nominal_date="2025-12-08")
    third_summary = reconcile_senate_amendments(conn)
    assert third_summary["filings_superseded"] == 1
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id == amend2
    # the original, already superseded, is untouched by this run (still points at amend-1,
    # not re-pointed at amend-2 - fine, since following the chain to its end still resolves
    # correctly and re-pointing every historical link on every new amendment isn't needed)
    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amend1


def test_ambiguous_group_resolved_by_trade_content(conn):
    """Real case, from Boozman's actual data: two originals filed the same date (one all
    sells, one all buys - a rebalance split across two submissions), amendment matches one
    almost entirely and the other not at all. Content evidence resolves what the date
    reference alone can't."""
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    sells = _insert_filing(conn, leg_id, "sells", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    buys = _insert_filing(conn, leg_id, "buys", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    amendment = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2026-08-24", nominal_date="2025-12-08")

    _insert_trade(conn, sells, 1, ticker="AAPL")
    _insert_trade(conn, sells, 2, ticker="RNWAX")  # gets ticker-corrected in the amendment
    _insert_trade(conn, buys, 1, ticker="MSFT", transaction_type="purchase")
    _insert_trade(conn, amendment, 1, ticker="AAPL")
    _insert_trade(conn, amendment, 2, ticker="RNWGX")  # the correction

    summary = reconcile_senate_amendments(conn)
    assert summary == {
        "groups_with_amendments": 1, "filings_superseded": 1,
        "groups_ambiguous": 0, "groups_resolved_by_content": 1, "chains_tied": 0,
    }
    assert models.get_filing_by_external_id(conn, "senate", "sells").superseded_by_filing_id == amendment
    assert models.get_filing_by_external_id(conn, "senate", "buys").superseded_by_filing_id is None
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id is None

    # the whole "sells" filing is superseded, so its RNWAX trade (which the amendment
    # corrected to RNWGX, so a trade-level content match could never catch it) is correctly
    # excluded from "current" via the filing-level join, not left as a residual duplicate
    current_tickers = {
        r["ticker"] for r in conn.execute("""
            SELECT t.ticker FROM trades t JOIN filings f ON t.filing_id = f.id
            WHERE f.legislator_id = ? AND f.superseded_by_filing_id IS NULL
        """, (leg_id,)).fetchall()
    }
    assert current_tickers == {"MSFT", "AAPL", "RNWGX"}


def test_still_ambiguous_when_content_matches_both_candidates(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    orig_a = _insert_filing(conn, leg_id, "orig-a", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    orig_b = _insert_filing(conn, leg_id, "orig-b", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    amendment = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2026-08-24", nominal_date="2025-12-08")

    _insert_trade(conn, orig_a, 1, ticker="AAPL")
    _insert_trade(conn, orig_b, 1, ticker="AAPL")
    _insert_trade(conn, amendment, 1, ticker="AAPL")  # matches both - genuinely ambiguous

    summary = reconcile_senate_amendments(conn)
    assert summary["groups_ambiguous"] == 1
    assert summary["groups_resolved_by_content"] == 0
    for ext_id in ("orig-a", "orig-b", "amend-1"):
        assert models.get_filing_by_external_id(conn, "senate", ext_id).superseded_by_filing_id is None


def test_amendment_beats_original_tied_on_same_filing_date(conn):
    """Real case, from Whitehouse's data: an original and the amendment correcting it were
    both recorded with the identical filing_date. Sorting by date alone makes this a coin
    flip; the amendment must deterministically win regardless of list-construction order."""
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2026-06-16", nominal_date="2026-06-16")
    amendment = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2026-06-16", nominal_date="2026-06-16")

    reconcile_senate_amendments(conn)

    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amendment
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id is None


def test_filed_at_breaks_same_day_amendment_chain(conn):
    """Real case, from Whitehouse's data: three separate amendments to the same original,
    all recorded with the identical filing_date (one calendar day), only distinguishable by
    the precise "Filed ... @ H:MM AM/PM" timestamp (9:41 AM, 3:42 PM, 4:15 PM). Without
    filed_at, these would tie; with it, they resolve into a correctly ordered chain."""
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2014-03-26", nominal_date="2014-03-26")
    amend1 = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T09:41")
    amend2 = _insert_filing(conn, leg_id, "amend-2", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T15:42")
    amend3 = _insert_filing(conn, leg_id, "amend-3", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T16:15")

    summary = reconcile_senate_amendments(conn)
    assert summary["chains_tied"] == 0
    assert summary["filings_superseded"] == 3

    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amend3
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id == amend3
    assert models.get_filing_by_external_id(conn, "senate", "amend-2").superseded_by_filing_id == amend3
    assert models.get_filing_by_external_id(conn, "senate", "amend-3").superseded_by_filing_id is None


def test_genuine_tie_is_flagged_not_guessed(conn):
    """Two amendments recorded with the identical precise timestamp - a true tie with no
    remaining signal to break it. Must be flagged, not resolved by arbitrary order."""
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2014-03-26", nominal_date="2014-03-26")
    amend1 = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T09:41")
    amend2 = _insert_filing(conn, leg_id, "amend-2", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T09:41")

    summary = reconcile_senate_amendments(conn)
    assert summary["chains_tied"] == 1
    assert summary["filings_superseded"] == 0

    for ext_id in ("orig-1", "amend-1", "amend-2"):
        assert models.get_filing_by_external_id(conn, "senate", ext_id).superseded_by_filing_id is None
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").reconciliation_note is not None
    assert models.get_filing_by_external_id(conn, "senate", "amend-2").reconciliation_note is not None


def test_explicit_amendment_number_is_authoritative_over_time(conn):
    """Real case, from Whitehouse's data: Amendment 1/2/3 filed the same day at 9:41am/
    3:42pm/4:15pm - filed_at already gets this right, but the explicit number is the more
    direct, authoritative signal from the source itself, confirmed to run in ascending
    filing-time order. Deliberately give amend-2 a LATER filed_at than amend-3 here, to
    prove the number wins over time when both are present, not just agree with it by luck."""
    leg_id = models.get_or_create_legislator(conn, "Sheldon", "Whitehouse", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2014-03-26", nominal_date="2014-03-26")
    amend1 = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T09:41", amendment_number=1)
    amend2 = _insert_filing(conn, leg_id, "amend-2", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T23:59", amendment_number=2)
    amend3 = _insert_filing(conn, leg_id, "amend-3", is_amendment=True, filing_date="2015-08-13", nominal_date="2014-03-26", filed_at="2015-08-13T16:15", amendment_number=3)

    summary = reconcile_senate_amendments(conn)
    assert summary["chains_tied"] == 0
    assert summary["filings_superseded"] == 3

    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amend3
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id == amend3
    assert models.get_filing_by_external_id(conn, "senate", "amend-2").superseded_by_filing_id == amend3
    assert models.get_filing_by_external_id(conn, "senate", "amend-3").superseded_by_filing_id is None


def test_falls_back_to_time_when_any_amendment_lacks_a_number(conn):
    """A mix of numbered and unnumbered amendments (older filings just say "(Amendment)"
    with no number) can't be ranked purely by number, so the whole chain falls back to
    effective-time ordering instead of guessing where the unnumbered one fits."""
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2019-01-01", nominal_date="2019-01-01")
    amend1 = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2019-02-01", nominal_date="2019-01-01", amendment_number=1)
    amend2 = _insert_filing(conn, leg_id, "amend-2", is_amendment=True, filing_date="2019-03-01", nominal_date="2019-01-01", amendment_number=None)

    summary = reconcile_senate_amendments(conn)
    assert summary["filings_superseded"] == 2
    assert models.get_filing_by_external_id(conn, "senate", "amend-2").superseded_by_filing_id is None
    assert models.get_filing_by_external_id(conn, "senate", "orig-1").superseded_by_filing_id == amend2
    assert models.get_filing_by_external_id(conn, "senate", "amend-1").superseded_by_filing_id == amend2


def test_two_amendments_with_the_same_number_is_flagged(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    original = _insert_filing(conn, leg_id, "orig-1", is_amendment=False, filing_date="2019-01-01", nominal_date="2019-01-01")
    amend1 = _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2019-02-01", nominal_date="2019-01-01", amendment_number=1)
    amend2 = _insert_filing(conn, leg_id, "amend-2", is_amendment=True, filing_date="2019-02-02", nominal_date="2019-01-01", amendment_number=1)

    summary = reconcile_senate_amendments(conn)
    assert summary["chains_tied"] == 1
    assert summary["filings_superseded"] == 0
    for ext_id in ("orig-1", "amend-1", "amend-2"):
        assert models.get_filing_by_external_id(conn, "senate", ext_id).superseded_by_filing_id is None
