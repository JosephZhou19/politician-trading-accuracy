"""Scraper for Senate eFD financial disclosure filings (efdsearch.senate.gov)."""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.db import models
from src.parse import sanity_checks, senate_ptr_parser

logger = logging.getLogger(__name__)

# A filing at one of these statuses is done - re-running the scraper skips it.
# 'pending'/'failed' mean a previous run started but never finished, so those get retried.
DONE_STATUSES = {"parsed", "needs_ocr"}

BASE_URL = "https://efdsearch.senate.gov"
HOME_URL = BASE_URL + "/search/home/"
DATA_URL = BASE_URL + "/search/report/data/"
USER_AGENT = "Mozilla/5.0 (research; contact josephzhou1234@gmail.com)"
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 1

# Verified against the live form's <label> text - visual layout order gets this wrong
# (14 is Blind Trust, not Periodic Transactions).
REPORT_TYPE_PTR = 11

# Verified against the live form's <label> text.
FILER_TYPE_SENATOR = 1
FILER_TYPE_CANDIDATE = 4
FILER_TYPE_FORMER_SENATOR = 5
FILER_TYPE_TO_STATUS = {
    FILER_TYPE_SENATOR: "member",
    FILER_TYPE_CANDIDATE: "candidate",
    FILER_TYPE_FORMER_SENATOR: "former_member",
}

FILING_ID_RE = re.compile(r"/search/view/(?:ptr|paper)/([0-9a-fA-F-]+)/")
AMENDMENT_RE = re.compile(r"\(Amendment", re.IGNORECASE)
# Modern amendments are explicitly numbered - "(Amendment 1)", "(Amendment 2)", etc. - in
# ascending order of actual filing time, a more authoritative ordering signal than inferring
# it from timestamps (see reconcile_amendments.py). Older amendments just say "(Amendment)"
# with no number; those fall back to filed_at/filing_date ordering.
AMENDMENT_NUMBER_RE = re.compile(r"\(Amendment\s*(\d+)\)", re.IGNORECASE)
# Every report title is "...for MM/DD/YYYY[ (Amendment N)]" - the filing's own date for a
# normal filing, or the ORIGINAL's date for an amendment (amendments carry no other
# reference to what they amend). This is the nominal_date used for reconciliation.
REPORT_DATE_RE = re.compile(r"for\s+(\d{2}/\d{2}/\d{4})")


def new_session():
    """Create a session that has accepted the site's required usage agreement -
    searching is refused without it."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    resp = session.get(HOME_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    token = soup.find("input", {"name": "csrfmiddlewaretoken"})["value"]
    session.post(
        HOME_URL,
        data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
        headers={"Referer": HOME_URL},
    )
    return session


def _fetch_page(session, filer_type, start, last_name="", length=PAGE_SIZE):
    data = {
        "report_types": f"[{REPORT_TYPE_PTR}]",
        "filer_types": f"[{filer_type}]",
        "submitted_start_date": "01/01/2012 00:00:00",
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": last_name,
        "draw": str(start // length + 1),
        "start": str(start),
        "length": str(length),
    }
    resp = None
    for attempt in range(5):
        resp = session.post(
            DATA_URL,
            data=data,
            headers={
                "Referer": BASE_URL + "/search/",
                "X-CSRFToken": session.cookies.get("csrftoken"),
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if resp.status_code == 200:
            return resp.json()
        time.sleep(2 * (attempt + 1))
    resp.raise_for_status()


def search_ptrs(session, filer_type, last_name=""):
    """Yield every PTR search-result row for one filer type. A blank last_name returns
    everyone - there's no member roster to search by instead, same as the House site."""
    start = 0
    total = None
    while total is None or start < total:
        result = _fetch_page(session, filer_type, start, last_name=last_name)
        total = result["recordsTotal"]
        yield from result["data"]
        start += PAGE_SIZE
        time.sleep(REQUEST_DELAY_SECONDS)


def _to_iso_date(mmddyyyy):
    month, day, year = mmddyyyy.split("/")
    return f"{year}-{month}-{day}"


def parse_row(row):
    first_name_raw, last_name_raw, _office_raw, link_html, date_str = row
    a = BeautifulSoup(link_html, "html.parser").find("a")
    href = a["href"]
    link_text = a.get_text()
    date_match = REPORT_DATE_RE.search(link_text)
    number_match = AMENDMENT_NUMBER_RE.search(link_text)
    return {
        "first_name": first_name_raw.strip().split()[0] if first_name_raw.strip() else "",
        "last_name": last_name_raw.strip(),
        "href": href,
        "external_filing_id": FILING_ID_RE.search(href).group(1).lower(),
        "is_paper": "/search/view/paper/" in href,
        "is_amendment": bool(AMENDMENT_RE.search(link_text)),
        "filing_date": _to_iso_date(date_str.strip()),
        "nominal_date": _to_iso_date(date_match.group(1)) if date_match else None,
        "amendment_number": int(number_match.group(1)) if number_match else None,
    }


def fetch_report_html(session, href):
    resp = session.get(urljoin(BASE_URL, href))
    resp.raise_for_status()
    return resp.text


def _process_paper_filing(conn, parsed, legislator_id, source_url, now, existing_filing_id):
    if existing_filing_id is None:
        filing_id = models.insert_filing(
            conn,
            legislator_id=legislator_id,
            chamber="senate",
            external_filing_id=parsed["external_filing_id"],
            filing_type="ptr",
            is_amendment=parsed["is_amendment"],
            filing_date=parsed["filing_date"],
            source_url=source_url,
            document_format="image",
            fetched_at=now,
            nominal_date=parsed["nominal_date"],
            amendment_number=parsed["amendment_number"],
        )
    else:
        filing_id = existing_filing_id
    models.update_filing_parse_status(conn, filing_id, "needs_ocr")
    return filing_id


def _process_electronic_filing(session, conn, html_dir, parsed, legislator_id, source_url, now, existing_filing_id):
    """Download, parse, and store one electronic PTR. Raises on any failure - the caller
    marks the filing 'failed' and moves on, so one bad filing can't take down an
    hours-long bulk run."""
    html = fetch_report_html(session, parsed["href"])
    html_file = html_dir / f"{parsed['external_filing_id']}.html"
    html_file.write_text(html, encoding="utf-8")

    if existing_filing_id is None:
        filing_id = models.insert_filing(
            conn,
            legislator_id=legislator_id,
            chamber="senate",
            external_filing_id=parsed["external_filing_id"],
            filing_type="ptr",
            is_amendment=parsed["is_amendment"],
            filing_date=parsed["filing_date"],
            source_url=source_url,
            document_format="html",
            fetched_at=now,
            raw_file_path=str(html_file),
            raw_doc_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            nominal_date=parsed["nominal_date"],
            filed_at=senate_ptr_parser.parse_filed_at(html),
            amendment_number=parsed["amendment_number"],
        )
    else:
        filing_id = existing_filing_id
        models.delete_trades_for_filing(conn, filing_id)

    trades = senate_ptr_parser.parse_report_html(html)
    for trade in trades:
        trade["notification_date"] = parsed["filing_date"]
        models.insert_trade(conn, filing_id=filing_id, **trade)

    models.update_filing_parse_status(
        conn, filing_id, "parsed", parsed_at=datetime.now(timezone.utc).isoformat()
    )
    issues = sanity_checks.validate_trades(trades)
    if issues:
        models.set_reconciliation_note(conn, filing_id, "; ".join(issues))
    return filing_id


def ingest_ptrs(conn, data_dir, filer_types=(FILER_TYPE_SENATOR,), last_name=""):
    """Search PTRs for the given filer types and write legislators/filings/trades.

    Paper-filed reports (scanned images) are recorded as a filings row with
    parse_status='needs_ocr' and no trades, rather than skipped outright - skipping
    them would silently make ~28% of Senate PTRs (and 27 senators who file exclusively
    on paper) disappear from any comparison. Actually OCR-ing them is future work.

    One bad filing is logged and skipped rather than stopping the whole run - check
    `failures` in the returned summary, or `SELECT * FROM filings WHERE parse_status =
    'failed'`, to see what needs attention.
    """
    html_dir = Path(data_dir) / "raw" / "senate"
    html_dir.mkdir(parents=True, exist_ok=True)
    session = new_session()
    summary = {"found": 0, "new": 0, "skipped": 0, "paper": 0, "failed": 0, "failures": []}

    for filer_type in filer_types:
        filer_status = FILER_TYPE_TO_STATUS[filer_type]
        for row in search_ptrs(session, filer_type, last_name=last_name):
            parsed = parse_row(row)
            summary["found"] += 1

            existing = models.get_filing_by_external_id(conn, "senate", parsed["external_filing_id"])
            if existing and existing.parse_status in DONE_STATUSES:
                summary["skipped"] += 1
                continue

            legislator_id = models.get_or_create_legislator(
                conn, parsed["first_name"], parsed["last_name"], "senate", filer_status
            )
            now = datetime.now(timezone.utc).isoformat()
            source_url = urljoin(BASE_URL, parsed["href"])
            existing_id = existing.id if existing else None

            try:
                if parsed["is_paper"]:
                    _process_paper_filing(conn, parsed, legislator_id, source_url, now, existing_id)
                    summary["paper"] += 1
                else:
                    _process_electronic_filing(
                        session, conn, html_dir, parsed, legislator_id, source_url, now, existing_id
                    )
                    summary["new"] += 1
            except Exception as e:
                logger.warning(
                    "Failed to process Senate PTR %s: %r", parsed["external_filing_id"], e
                )
                if existing_id:
                    models.update_filing_parse_status(conn, existing_id, "failed")
                summary["failed"] += 1
                summary["failures"].append((parsed["external_filing_id"], repr(e)))
            time.sleep(REQUEST_DELAY_SECONDS)

    return summary
