"""Parses an electronic Senate eFD Periodic Transaction Report page into trade records.

Only covers electronically-filed reports (/search/view/ptr/...), which are plain HTML
tables. Paper-filed reports (/search/view/paper/...) are scanned images and are handled
separately in src/ingest/senate_efd.py - see PLAN.md for why those aren't skipped outright.
"""

import re

from bs4 import BeautifulSoup

TRANSACTION_TYPE_MAP = {
    "Purchase": "purchase",
    "Sale (Full)": "sale_full",
    "Sale (Partial)": "sale_partial",
    "Exchange": "exchange",
}

OWNER_MAP = {
    "Self": "self",
    "Spouse": "spouse",
    "Joint": "joint",
    "Dependent Child": "dependent_child",
    "Child": "dependent_child",  # older (~2014-2017) filings use this label instead
}

# The source's ticker-link cell isn't reliable for these - a bond can be linked to its
# issuer's common-stock ticker, an unrelated OTC symbol, or a foreign listing, never a
# ticker for the bond itself. Treating that as the equity would corrupt later price
# matching, so ticker is only trusted for asset types outside this set.
NON_EQUITY_ASSET_TYPES = {"Corporate Bond", "Municipal Security"}

# The ticker-link cell is occasionally empty ("--") for what's still a real equity trade,
# with the ticker glued onto the front of the asset name instead - e.g.
# "STT-State Street Corporation" or "BRK-B - Berkshire Hathaway Inc Class B".
GLUED_TICKER_RE = re.compile(r"^([A-Z]{1,6}(?:-[A-Z])?)\s*-\s*(.+)$")


def _parse_amount(amount_raw):
    parts = re.findall(r"\$[\d,]+(?:\.\d+)?", amount_raw)
    if not parts:
        return None, None
    low = int(float(parts[0].replace("$", "").replace(",", "")))
    high = int(float(parts[1].replace("$", "").replace(",", ""))) if len(parts) > 1 else low
    return low, high


def _to_iso_date(mmddyyyy):
    month, day, year = mmddyyyy.split("/")
    return f"{year}-{month}-{day}"


FILED_AT_RE = re.compile(r"Filed\s+(\d{2}/\d{2}/\d{4})\s*@\s*(\d{1,2}):(\d{2})\s*(AM|PM)")


def parse_filed_at(html):
    """Extract the precise "Filed MM/DD/YYYY @ H:MM AM/PM" timestamp from the report page,
    as a sortable "YYYY-MM-DDTHH:MM" string (24-hour). More precise than the date-only
    value in search results, needed to order same-day amendment chains. Returns None if
    not found."""
    m = FILED_AT_RE.search(html)
    if not m:
        return None
    date_str, hour_str, minute, meridiem = m.groups()
    hour = int(hour_str) % 12
    if meridiem == "PM":
        hour += 12
    return f"{_to_iso_date(date_str)}T{hour:02d}:{minute}"


def parse_report_html(html):
    """Extract trade records from an electronic PTR page's transaction table.

    notification_date isn't in this table at all - Senate reports one filing date for
    the whole report, not a per-transaction notification date like House does - so the
    caller fills it in from the filing's own filing_date after this returns.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None or table.find("tbody") is None:
        return []

    trades = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != 9:
            continue
        id_cell, date_cell, owner_cell, ticker_cell, asset_cell, type_cell, txn_cell, amount_cell, comment_cell = cells

        ticker_link = ticker_cell.find("a")
        ticker_text = ticker_link.get_text(strip=True) if ticker_link else None
        asset_type = type_cell.get_text(strip=True) or None

        # Bond/note detail (Rate/Coupon, Matures) lives in a nested <div class="text-muted">
        # inside the asset-name cell with no separator - extract it first so get_text()
        # on the cell doesn't glue it onto the name.
        bond_detail_div = asset_cell.find("div", class_="text-muted")
        bond_detail = bond_detail_div.get_text(" ", strip=True) if bond_detail_div else None
        if bond_detail_div is not None:
            bond_detail_div.extract()
        asset_name = asset_cell.get_text(strip=True)

        ticker = ticker_text if asset_type not in NON_EQUITY_ASSET_TYPES else None
        if ticker is None and asset_type not in NON_EQUITY_ASSET_TYPES:
            glued_match = GLUED_TICKER_RE.match(asset_name)
            if glued_match:
                ticker, asset_name = glued_match.group(1), glued_match.group(2)

        raw_row_text = None
        if asset_type in NON_EQUITY_ASSET_TYPES:
            parts = [p for p in (bond_detail, f"source ticker link: {ticker_text}" if ticker_text else None) if p]
            raw_row_text = " | ".join(parts) or None

        comment_text = comment_cell.get_text(strip=True)
        amount_low, amount_high = _parse_amount(amount_cell.get_text(strip=True))
        txn_raw = txn_cell.get_text(strip=True)
        owner_raw = owner_cell.get_text(strip=True)

        trades.append({
            "source_row_number": int(id_cell.get_text(strip=True)),
            "ticker": ticker,
            "asset_name": asset_name,
            "asset_type": asset_type,
            "transaction_type": TRANSACTION_TYPE_MAP.get(txn_raw, txn_raw),
            "transaction_date": _to_iso_date(date_cell.get_text(strip=True)),
            "amount_low": amount_low,
            "amount_high": amount_high,
            "owner": OWNER_MAP.get(owner_raw, owner_raw),
            "comment": None if comment_text == "--" else comment_text,
            "raw_row_text": raw_row_text,
        })
    return trades
