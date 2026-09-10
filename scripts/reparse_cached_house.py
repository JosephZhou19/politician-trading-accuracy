"""Re-parse already-ingested House PTRs from their cached raw PDFs, without hitting
the network. Needed whenever house_ptr_parser.py's logic changes, since ingest_ptrs()
skips anything already marked 'parsed'/'needs_ocr' - a normal ingestion re-run won't
pick up a parser fix on its own. Also retries every 'needs_ocr' filing, since some of
those may now be recoverable (the per-document column-derivation fix reclaims real
filings that were previously misrouted to the OCR queue).
"""
import sqlite3
import sys
from datetime import datetime, timezone

from src.db import models
from src.parse import house_ptr_parser as hp


def _reparse_one(conn, filing_id, raw_file_path):
    try:
        parsed = hp.parse_filing(raw_file_path)
    except hp.UnparseableFormError:
        return "needs_ocr", 0
    models.delete_trades_for_filing(conn, filing_id)
    for trade in parsed["trades"]:
        models.insert_trade(conn, filing_id=filing_id, **trade)
    models.update_filing_parse_status(
        conn, filing_id, "parsed", parsed_at=datetime.now(timezone.utc).isoformat()
    )
    # A filing reclaimed from needs_ocr never had its filing_date set (it genuinely
    # wasn't recoverable before) - now that it parses, fill it in.
    if parsed["filing_date"]:
        conn.execute(
            "UPDATE filings SET filing_date = ? WHERE id = ? AND filing_date IS NULL",
            (parsed["filing_date"], filing_id),
        )
        conn.commit()
    return "parsed", len(parsed["trades"])


def main(db_path="data/sample.db"):
    conn = models.connect(db_path)
    conn.row_factory = sqlite3.Row

    filings = conn.execute(
        """SELECT id, raw_file_path, parse_status FROM filings
           WHERE chamber = 'house' AND document_format IN ('pdf', 'image')
             AND parse_status IN ('parsed', 'needs_ocr', 'pending', 'failed')
             AND raw_file_path IS NOT NULL"""
    ).fetchall()

    reclaimed = 0
    count_changed = 0
    still_needs_ocr = 0
    for f in filings:
        before = conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE filing_id = ?", (f["id"],)
        ).fetchone()[0]

        new_status, new_count = _reparse_one(conn, f["id"], f["raw_file_path"])

        if f["parse_status"] == "needs_ocr" and new_status == "parsed":
            reclaimed += 1
            print(f"filing {f['id']}: needs_ocr -> parsed, {new_count} trades")
        elif new_status == "needs_ocr":
            still_needs_ocr += 1
        elif new_count != before:
            count_changed += 1
            print(f"filing {f['id']}: {before} -> {new_count} trades")

    print(f"\nRe-parsed {len(filings)} filings.")
    print(f"Reclaimed from needs_ocr: {reclaimed}")
    print(f"Trade count changed (already-parsed filings): {count_changed}")
    print(f"Still needs_ocr (genuine scans): {still_needs_ocr}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/sample.db")
