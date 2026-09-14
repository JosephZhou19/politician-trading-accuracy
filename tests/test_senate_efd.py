from unittest.mock import patch

from src.db import models
from src.ingest import senate_efd
from src.ingest.senate_efd import FILER_TYPE_SENATOR, ingest_ptrs


def _search_row(href="/search/view/ptr/aaa-111/", link_text="PTR for 08/27/2026",
                 first="Thomas", last="Carper", date_str="09/02/2026"):
    link_html = f'<a href="{href}">{link_text}</a>'
    return [first, last, "DE", link_html, date_str]


def test_ingest_ptrs_skips_already_parsed_filing_via_dict_with_no_db_lookup(conn, tmp_path):
    """Regression for the ingest N+1 latency bug (see PLAN.md): the skip-check for an
    already-ingested filing must come from the in-memory existing_by_ext_id dict, not a
    per-row get_filing_by_external_id round trip."""
    leg_id = models.get_or_create_legislator(conn, "Thomas", "Carper", "senate", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="senate",
        external_filing_id="aaa-111",
        filing_type="ptr",
        is_amendment=False,
        filing_date="2026-08-27",
        source_url="https://efdsearch.senate.gov/search/view/ptr/aaa-111/",
        document_format="html",
        fetched_at="2026-09-05T00:00:00",
    )
    models.update_filing_parse_status(conn, filing_id, "parsed")

    with patch.object(senate_efd, "new_session", return_value=object()), \
         patch.object(senate_efd, "search_ptrs", return_value=[_search_row()]), \
         patch.object(models, "get_filing_by_external_id") as mock_lookup:
        summary = ingest_ptrs(conn, tmp_path, filer_types=(FILER_TYPE_SENATOR,))

    mock_lookup.assert_not_called()
    assert summary["found"] == 1
    assert summary["skipped"] == 1
    assert summary["new"] == 0


def test_ingest_ptrs_processes_new_filing_and_updates_the_shared_dict(conn, tmp_path):
    trades = [{
        "source_row_number": 1,
        "ticker": "GOOGL",
        "asset_name": "Alphabet Inc. - Class A Common Stock (GOOGL) [ST]",
        "asset_type": "ST",
        "transaction_type": "sale_partial",
        "transaction_date": "2026-08-27",
        "amount_low": 1001,
        "amount_high": 15000,
        "owner": "self",
        "comment": None,
        "raw_row_text": None,
        "filing_status": "New",
    }]
    existing_by_ext_id = {}

    with patch.object(senate_efd, "new_session", return_value=object()), \
         patch.object(senate_efd, "search_ptrs", return_value=[_search_row(href="/search/view/ptr/bbb-222/")]), \
         patch.object(senate_efd, "fetch_report_html", return_value="<html></html>"), \
         patch.object(senate_efd.senate_ptr_parser, "parse_report_html", return_value=trades), \
         patch.object(senate_efd.senate_ptr_parser, "parse_filed_at", return_value="2026-09-02T10:00"):
        summary = ingest_ptrs(
            conn, tmp_path, filer_types=(FILER_TYPE_SENATOR,), existing_by_ext_id=existing_by_ext_id
        )

    assert summary["new"] == 1
    assert summary["skipped"] == 0

    filing_id, status = existing_by_ext_id["bbb-222"]
    assert status == "parsed"
    fetched = models.get_filing_by_external_id(conn, "senate", "bbb-222")
    assert fetched.id == filing_id
    assert fetched.parse_status == "parsed"
    stored_trades = conn.execute(
        "SELECT ticker FROM trades WHERE filing_id = ?", (filing_id,)
    ).fetchall()
    assert [t["ticker"] for t in stored_trades] == ["GOOGL"]


def test_ingest_ptrs_records_paper_filing_and_updates_the_shared_dict(conn, tmp_path):
    """Paper filings get a needs_ocr row with no trades - the shared dict must reflect
    that status too, not just the 'parsed' electronic-filing path."""
    existing_by_ext_id = {}

    with patch.object(senate_efd, "new_session", return_value=object()), \
         patch.object(senate_efd, "search_ptrs",
                      return_value=[_search_row(href="/search/view/paper/ccc-333/")]):
        summary = ingest_ptrs(
            conn, tmp_path, filer_types=(FILER_TYPE_SENATOR,), existing_by_ext_id=existing_by_ext_id
        )

    assert summary["paper"] == 1
    filing_id, status = existing_by_ext_id["ccc-333"]
    assert status == "needs_ocr"
    fetched = models.get_filing_by_external_id(conn, "senate", "ccc-333")
    assert fetched.parse_status == "needs_ocr"


def test_ingest_ptrs_builds_its_own_dict_when_none_passed(conn, tmp_path):
    with patch.object(senate_efd, "new_session", return_value=object()), \
         patch.object(senate_efd, "search_ptrs", return_value=[]):
        summary = ingest_ptrs(conn, tmp_path, filer_types=(FILER_TYPE_SENATOR,))
    assert summary["found"] == 0
