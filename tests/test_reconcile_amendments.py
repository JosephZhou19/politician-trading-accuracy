from src.db import models
from src.ingest.reconcile_amendments import reconcile_senate_amendments


def _insert_filing(conn, legislator_id, ext_id, *, is_amendment, filing_date, nominal_date):
    return models.insert_filing(
        conn,
        legislator_id=legislator_id,
        chamber="senate",
        external_filing_id=ext_id,
        filing_type="ptr",
        is_amendment=is_amendment,
        filing_date=filing_date,
        nominal_date=nominal_date,
        source_url=f"https://efdsearch.senate.gov/search/view/ptr/{ext_id}/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
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
    assert summary == {"groups_with_amendments": 1, "filings_superseded": 1, "groups_ambiguous": 0}

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
    assert summary == {"groups_with_amendments": 0, "filings_superseded": 0, "groups_ambiguous": 0}
    assert models.get_filing_by_external_id(conn, "senate", "orig-a").superseded_by_filing_id is None
    assert models.get_filing_by_external_id(conn, "senate", "orig-b").superseded_by_filing_id is None


def test_ambiguous_group_is_flagged_not_guessed(conn):
    leg_id = models.get_or_create_legislator(conn, "John", "Boozman", "senate", "member")
    _insert_filing(conn, leg_id, "orig-a", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    _insert_filing(conn, leg_id, "orig-b", is_amendment=False, filing_date="2025-12-08", nominal_date="2025-12-08")
    _insert_filing(conn, leg_id, "amend-1", is_amendment=True, filing_date="2026-08-24", nominal_date="2025-12-08")

    summary = reconcile_senate_amendments(conn)
    assert summary == {"groups_with_amendments": 1, "filings_superseded": 0, "groups_ambiguous": 1}

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
