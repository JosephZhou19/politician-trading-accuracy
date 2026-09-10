"""Lists House filings stuck at parse_status='needs_ocr' (scanned PDFs with no text layer)
with a link to the source PDF, and walks through them interactively so a human can read each
one and type in the trades by hand - deliberately not an auto-transcriber, since financial
figures shouldn't be silently guessed at.

Usage:
    python -m scripts.resolve_needs_ocr                       # walk every needs_ocr filing
    python -m scripts.resolve_needs_ocr --last-name Gottheimer # just one legislator
    python -m scripts.resolve_needs_ocr --list-only            # just print links, no prompts

Resumable: a filing is only marked 'parsed' once its entry loop finishes normally, so quitting
partway (Ctrl-C or 'q') leaves it at 'needs_ocr' with trades already typed in preserved.
"""
import argparse
import sqlite3

from src.db import models
from src.parse.house_ptr_parser import (
    _canonicalize_owner,
    _canonicalize_transaction_type,
    _extract_ticker_and_asset_type,
    _to_iso_date,
)

TYPE_HELP = "P=purchase, S=sale (full), \"S (Partial)\"=sale (partial), E=exchange"
OWNER_HELP = "blank=self, SP=spouse, JT=joint, DC=dependent child"


def get_needs_ocr_filings(conn, last_name=None):
    query = """
        SELECT f.id, f.external_filing_id, f.source_url, f.filing_date,
               l.first_name, l.last_name
        FROM filings f JOIN legislators l ON f.legislator_id = l.id
        WHERE f.chamber = 'house' AND f.parse_status = 'needs_ocr'
    """
    params = ()
    if last_name:
        query += " AND l.last_name = ?"
        params = (last_name,)
    query += " ORDER BY l.last_name, f.external_filing_id"
    return conn.execute(query, params).fetchall()


def print_filing_list(filings):
    if not filings:
        print("No needs_ocr filings found.")
        return
    print(f"{len(filings)} filing(s) stuck at needs_ocr:\n")
    for f in filings:
        print(f"  {f['first_name']} {f['last_name']} - filing {f['external_filing_id']}")
        print(f"    {f['source_url']}")


def _prompt(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _prompt_iso_date(label):
    while True:
        raw = input(f"{label} (MM/DD/YYYY, blank to skip): ").strip()
        if not raw:
            return None
        try:
            return _to_iso_date(raw)
        except ValueError:
            print(f"  Couldn't parse {raw!r} as a date - try again.")


def _prompt_amount():
    while True:
        raw = input("Amount (e.g. \"1,001 - 15,000\" or \"1001-15000\"): ").strip()
        digit_groups = ["".join(ch for ch in part if ch.isdigit()) for part in raw.split("-")]
        digit_groups = [g for g in digit_groups if g]
        if len(digit_groups) == 1:
            return int(digit_groups[0]), int(digit_groups[0])
        if len(digit_groups) == 2:
            return int(digit_groups[0]), int(digit_groups[1])
        print("  Couldn't read that as one or two numbers - try again.")


def _prompt_trade(next_row_number):
    print(f"\n--- Trade (row {next_row_number}; blank asset to finish this filing, "
          f"'q' to stop and leave it needs_ocr) ---")
    asset_raw = input('Asset (full text, e.g. "Apple Inc. (AAPL) [ST]"): ').strip()
    if not asset_raw:
        return "done"
    if asset_raw.lower() == "q":
        return "quit"

    ticker, asset_type = _extract_ticker_and_asset_type(asset_raw)
    type_raw = _prompt(f"Type ({TYPE_HELP})") or ""
    transaction_date = _prompt_iso_date("Transaction date")
    notification_date = _prompt_iso_date("Notification date")
    amount_low, amount_high = _prompt_amount()
    owner_raw = _prompt(f"Owner ({OWNER_HELP})", default="") or ""
    filing_status = (_prompt("Filing status (New/Amended)", default="New") or "new").lower()
    comment = _prompt("Comment/description (optional)", default="") or None

    return {
        "ticker": ticker,
        "asset_name": asset_raw,
        "asset_type": asset_type,
        "transaction_type": _canonicalize_transaction_type(type_raw),
        "transaction_date": transaction_date,
        "notification_date": notification_date or transaction_date,
        "amount_low": amount_low,
        "amount_high": amount_high,
        "owner": _canonicalize_owner(owner_raw),
        "comment": comment,
        "raw_row_text": "[manually transcribed from scanned PDF]",
        "filing_status": filing_status,
    }


def resolve_filing(conn, filing):
    print(f"\n=== Filing {filing['external_filing_id']} - {filing['first_name']} "
          f"{filing['last_name']} ===")
    print(f"Link: {filing['source_url']}")
    choice = input("[Enter] start entering trades   [z] reviewed, nothing to report   "
                    "[s] skip for now: ").strip().lower()
    if choice == "s":
        print("Skipped.")
        return "skipped"

    existing = models.get_trades_for_filing(conn, filing["id"])
    next_row_number = max((t.source_row_number for t in existing), default=0) + 1
    entered = 0

    if choice != "z":
        if next_row_number > 1:
            print(f"({next_row_number - 1} trade(s) already entered for this filing previously.)")
        filing_date = _prompt_iso_date("Filing date (date the filer signed/certified this report)")
        if filing_date:
            models.set_filing_date(conn, filing["id"], filing_date)

        while True:
            trade = _prompt_trade(next_row_number)
            if trade == "done":
                break
            if trade == "quit":
                print("Stopping here - filing left at needs_ocr, trades entered so far are saved.")
                return "partial"
            models.insert_trade(conn, filing_id=filing["id"], source_row_number=next_row_number, **trade)
            next_row_number += 1
            entered += 1

    models.update_filing_parse_status(conn, filing["id"], "parsed")
    print(f"Filing {filing['external_filing_id']} marked parsed ({entered} trade(s) entered this run).")
    return "resolved"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/sample.db")
    parser.add_argument("--last-name", help="Only process this legislator's needs_ocr filings")
    parser.add_argument("--list-only", action="store_true", help="Print links and exit, no prompts")
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    filings = get_needs_ocr_filings(conn, args.last_name)

    print_filing_list(filings)
    if args.list_only or not filings:
        return

    summary = {"resolved": 0, "partial": 0, "skipped": 0}
    for filing in filings:
        outcome = resolve_filing(conn, filing)
        summary[outcome] = summary.get(outcome, 0) + 1

    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
