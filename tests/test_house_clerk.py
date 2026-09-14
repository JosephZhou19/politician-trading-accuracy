from unittest.mock import patch

from src.db import models
from src.ingest import house_clerk
from src.ingest.house_clerk import ingest_ptrs, parse_house_name


def test_parse_house_name_strips_hon_title():
    assert parse_house_name("Gottheimer, Hon.. Josh") == ("Josh", "Gottheimer")


def test_parse_house_name_strips_non_hon_titles():
    """Regression: a real filing used "Mrs.." instead of "Hon..", which - unstripped -
    got parsed as the first name, creating a duplicate legislator row for the same
    person (Marjorie Greene / "Mrs.. Greene")."""
    assert parse_house_name("Greene, Mrs.. Marjorie Taylor") == ("Marjorie", "Greene")


def test_parse_house_name_middle_name_dropped():
    assert parse_house_name("Gottheimer, Hon.. Josh Middle") == ("Josh", "Gottheimer")


def _search_result_row(ext_id, name_raw="Kean, Hon.. Thomas H. Jr."):
    return {
        "name_raw": name_raw,
        "office": "NJ07",
        "filing_year": "2026",
        "filing_type_raw": "PTR Original",
        "pdf_path": f"public_disc/ptr-pdfs/2026/{ext_id}.pdf",
    }


def test_ingest_ptrs_skips_already_parsed_filing_via_dict_with_no_db_lookup(conn, tmp_path):
    """Regression for the ingest N+1 latency bug (see PLAN.md): the skip-check for an
    already-ingested filing must come from the in-memory existing_by_ext_id dict, not a
    per-row get_filing_by_external_id round trip - that round trip, multiplied by every
    filing already on file, was the dominant cost of a full run over a high-latency
    connection."""
    leg_id = models.get_or_create_legislator(conn, "Thomas", "Kean", "house", "member")
    filing_id = models.insert_filing(
        conn,
        legislator_id=leg_id,
        chamber="house",
        external_filing_id="111",
        filing_type="ptr",
        is_amendment=False,
        source_url="https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/111.pdf",
        document_format="pdf",
        fetched_at="2026-09-05T00:00:00",
    )
    models.update_filing_parse_status(conn, filing_id, "parsed")

    with patch.object(house_clerk, "search_filings", return_value=[_search_result_row("111")]), \
         patch.object(models, "get_filing_by_external_id") as mock_lookup:
        summary = ingest_ptrs(conn, tmp_path, 2026)

    mock_lookup.assert_not_called()
    assert summary["ptrs_found"] == 1
    assert summary["ptrs_skipped"] == 1
    assert summary["ptrs_new"] == 0


def test_ingest_ptrs_processes_new_filing_and_updates_the_shared_dict(conn, tmp_path):
    parsed_pdf = {
        "filer_status_raw": "member",
        "filing_date": "2026-08-27",
        "trades": [{
            "source_row_number": 1,
            "ticker": "GOOGL",
            "asset_name": "Alphabet Inc. - Class A Common Stock (GOOGL) [ST]",
            "asset_type": "ST",
            "transaction_type": "sale_partial",
            "transaction_date": "2026-08-27",
            "notification_date": "2026-09-02",
            "amount_low": 1001,
            "amount_high": 15000,
            "owner": "self",
            "comment": None,
            "raw_row_text": None,
            "filing_status": "new",
        }],
    }
    existing_by_ext_id = {}

    with patch.object(house_clerk, "search_filings", return_value=[_search_result_row("222")]), \
         patch.object(house_clerk, "download_pdf", return_value=b"%PDF-fake"), \
         patch.object(house_clerk.house_ptr_parser, "parse_filing", return_value=parsed_pdf):
        summary = ingest_ptrs(conn, tmp_path, 2026, existing_by_ext_id=existing_by_ext_id)

    assert summary["ptrs_new"] == 1
    assert summary["ptrs_skipped"] == 0

    filing_id, status = existing_by_ext_id["222"]
    assert status == "parsed"
    fetched = models.get_filing_by_external_id(conn, "house", "222")
    assert fetched.id == filing_id
    assert fetched.parse_status == "parsed"
    trades = conn.execute(
        "SELECT ticker FROM trades WHERE filing_id = ?", (filing_id,)
    ).fetchall()
    assert [t["ticker"] for t in trades] == ["GOOGL"]


def test_ingest_ptrs_builds_its_own_dict_when_none_passed(conn, tmp_path):
    """Standalone calls (scripts/ingest_house_sample.py, or a test that doesn't care about
    cross-year dedup) shouldn't have to build the dict themselves."""
    with patch.object(house_clerk, "search_filings", return_value=[]):
        summary = ingest_ptrs(conn, tmp_path, 2026)
    assert summary["ptrs_found"] == 0
