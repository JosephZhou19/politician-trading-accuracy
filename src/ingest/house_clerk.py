"""Scraper for House Clerk financial disclosure filings (disclosures-clerk.house.gov)."""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from src.db import models
from src.parse import house_ptr_parser, sanity_checks

logger = logging.getLogger(__name__)

# A filing at one of these statuses is done - re-running the scraper skips it. 'needs_ocr'
# counts as done for now since there's no OCR/legacy-form step to retry into yet;
# 'pending'/'failed' mean a previous run started but never finished, so those are retried.
DONE_STATUSES = {"parsed", "needs_ocr"}

BASE_URL = "https://disclosures-clerk.house.gov/"
SEARCH_URL = BASE_URL + "FinancialDisclosure/ViewMemberSearchResult"
USER_AGENT = "Mozilla/5.0 (research; contact josephzhou1234@gmail.com)"
REQUEST_DELAY_SECONDS = 1

FILING_ID_RE = re.compile(r"(\d+)\.pdf$")


def search_filings(filing_year, last_name="", state="", district=""):
    """Query the House Clerk search API. A blank last_name returns every filing for
    the year - the site has no roster we can search by instead."""
    resp = requests.post(
        SEARCH_URL,
        data={
            "LastName": last_name,
            "FilingYear": str(filing_year),
            "State": state,
            "District": district,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return _parse_search_results(resp.text)


def _parse_search_results(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("tbody tr"):
        cells = tr.select("td")
        link = cells[0].find("a")
        if not link:
            continue
        rows.append({
            "name_raw": cells[0].get_text(strip=True),
            "office": cells[1].get_text(strip=True),
            "filing_year": cells[2].get_text(strip=True),
            "filing_type_raw": cells[3].get_text(strip=True),
            "pdf_path": link["href"],
        })
    return rows


def is_ptr(filing_type_raw):
    return "PTR" in filing_type_raw


def is_amendment(filing_type_raw):
    return "amendment" in filing_type_raw.lower()


def external_filing_id(pdf_path):
    return FILING_ID_RE.search(pdf_path).group(1)


def parse_house_name(name_raw):
    """Split the search result's 'Last, Hon.. First Middle' name into (first, last).
    The comma unambiguously separates last name from first+middle, unlike the PDF's
    own 'Hon. First Middle Last' field where the last-name boundary is ambiguous.
    Title is usually "Hon.." but at least one real filing used "Mrs.." instead -
    without stripping it too, "Mrs.." gets parsed as the first name, creating a
    duplicate legislator row for the same person."""
    last_name, _, rest = name_raw.partition(",")
    rest = re.sub(r"^\s*(?:Hon|Mrs|Mr|Ms|Dr)\.+\s*", "", rest.strip())
    first_name = rest.split()[0] if rest else ""
    return first_name, last_name.strip()


def download_pdf(pdf_path):
    resp = requests.get(BASE_URL + pdf_path, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.content


def _insert_new_filing(conn, legislator_id, ext_id, row, pdf_file, pdf_bytes, now, *,
                        document_format, filing_date=None):
    return models.insert_filing(
        conn,
        legislator_id=legislator_id,
        chamber="house",
        external_filing_id=ext_id,
        filing_type="ptr",
        is_amendment=is_amendment(row["filing_type_raw"]),
        filing_date=filing_date,
        source_url=BASE_URL + row["pdf_path"],
        document_format=document_format,
        fetched_at=now,
        raw_file_path=str(pdf_file),
        raw_doc_hash=hashlib.sha256(pdf_bytes).hexdigest(),
    )


def _process_filing(conn, pdf_dir, row, existing_filing_id):
    """Download and store one PTR. Returns 'parsed' or 'needs_ocr' (for a legacy
    hand-filled/scanned form this parser doesn't handle - see UnparseableFormError).
    Raises on any *unexpected* failure - the caller marks the filing 'failed' and moves
    on, so one bad filing can't take down an hours-long bulk run."""
    ext_id = external_filing_id(row["pdf_path"])
    pdf_bytes = download_pdf(row["pdf_path"])
    pdf_file = pdf_dir / f"{ext_id}.pdf"
    pdf_file.write_bytes(pdf_bytes)
    first_name, last_name_parsed = parse_house_name(row["name_raw"])
    now = datetime.now(timezone.utc).isoformat()

    try:
        parsed = house_ptr_parser.parse_filing(pdf_file)
    except house_ptr_parser.UnparseableFormError:
        # filer_status/filing_date aren't recoverable without OCR support; filer_status
        # defaults to 'member' (PTRs are overwhelmingly filed by sitting members) rather
        # than being left unset, and filing_date stays NULL (a guess would be worse).
        legislator_id = models.get_or_create_legislator(
            conn, first_name, last_name_parsed, "house", "member"
        )
        filing_id = existing_filing_id or _insert_new_filing(
            conn, legislator_id, ext_id, row, pdf_file, pdf_bytes, now,
            document_format="image",
        )
        models.update_filing_parse_status(conn, filing_id, "needs_ocr")
        return "needs_ocr"

    filer_status = house_ptr_parser.canonicalize_filer_status(parsed["filer_status_raw"])
    legislator_id = models.get_or_create_legislator(
        conn, first_name, last_name_parsed, "house", filer_status
    )

    if existing_filing_id is None:
        filing_id = _insert_new_filing(
            conn, legislator_id, ext_id, row, pdf_file, pdf_bytes, now,
            document_format="pdf", filing_date=parsed["filing_date"],
        )
    else:
        filing_id = existing_filing_id
        models.delete_trades_for_filing(conn, filing_id)

    for trade in parsed["trades"]:
        models.insert_trade(conn, filing_id=filing_id, **trade)

    models.update_filing_parse_status(
        conn, filing_id, "parsed", parsed_at=datetime.now(timezone.utc).isoformat()
    )
    issues = sanity_checks.validate_trades(parsed["trades"])
    if issues:
        models.set_reconciliation_note(conn, filing_id, "; ".join(issues))
    return "parsed"


def ingest_ptrs(conn, data_dir, filing_year, last_name=""):
    """Search a filing year (optionally scoped to one last name), download and parse
    every new or previously-failed/pending PTR, and write legislators/filings/trades.
    One bad filing is logged and skipped rather than stopping the whole run - check
    `failures` in the returned summary, or `SELECT * FROM filings WHERE parse_status =
    'failed'`, to see what needs attention."""
    pdf_dir = Path(data_dir) / "raw" / "house"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "ptrs_found": 0, "ptrs_new": 0, "ptrs_skipped": 0,
        "ptrs_needs_ocr": 0, "ptrs_failed": 0, "failures": [],
    }
    for row in search_filings(filing_year, last_name=last_name):
        if not is_ptr(row["filing_type_raw"]):
            continue
        summary["ptrs_found"] += 1

        ext_id = external_filing_id(row["pdf_path"])
        existing = models.get_filing_by_external_id(conn, "house", ext_id)
        if existing and existing.parse_status in DONE_STATUSES:
            summary["ptrs_skipped"] += 1
            continue

        try:
            status = _process_filing(conn, pdf_dir, row, existing.id if existing else None)
            summary["ptrs_needs_ocr" if status == "needs_ocr" else "ptrs_new"] += 1
        except Exception as e:
            logger.warning("Failed to process House PTR %s: %r", ext_id, e)
            if existing:
                models.update_filing_parse_status(conn, existing.id, "failed")
            summary["ptrs_failed"] += 1
            summary["failures"].append((ext_id, repr(e)))
        time.sleep(REQUEST_DELAY_SECONDS)

    return summary
