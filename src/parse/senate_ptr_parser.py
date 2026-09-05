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
        _, date_cell, owner_cell, ticker_cell, asset_cell, type_cell, txn_cell, amount_cell, comment_cell = cells

        ticker_link = ticker_cell.find("a")
        ticker = ticker_link.get_text(strip=True) if ticker_link else None

        comment_text = comment_cell.get_text(strip=True)
        amount_low, amount_high = _parse_amount(amount_cell.get_text(strip=True))
        txn_raw = txn_cell.get_text(strip=True)
        owner_raw = owner_cell.get_text(strip=True)

        trades.append({
            "ticker": ticker,
            "asset_name": asset_cell.get_text(strip=True),
            "asset_type": type_cell.get_text(strip=True) or None,
            "transaction_type": TRANSACTION_TYPE_MAP.get(txn_raw, txn_raw),
            "transaction_date": _to_iso_date(date_cell.get_text(strip=True)),
            "amount_low": amount_low,
            "amount_high": amount_high,
            "owner": OWNER_MAP.get(owner_raw, owner_raw),
            "comment": None if comment_text == "--" else comment_text,
            "raw_row_text": None,
        })
    return trades
