"""Re-parse already-ingested Senate PTRs from their cached raw HTML, without hitting
the network. Needed whenever senate_ptr_parser.py's logic changes, since ingest_ptrs()
skips anything already marked 'parsed' - a normal ingestion re-run won't pick up a
parser fix on its own.
"""
import sqlite3
import sys

from src.db import models
from src.parse import senate_ptr_parser


def main(db_path="data/sample.db"):
    conn = models.connect(db_path)
    conn.row_factory = sqlite3.Row

    filings = conn.execute(
        """SELECT id, raw_file_path FROM filings
           WHERE chamber = 'senate' AND document_format = 'html'
             AND parse_status = 'parsed' AND raw_file_path IS NOT NULL"""
    ).fetchall()

    changed = 0
    for f in filings:
        with open(f["raw_file_path"], encoding="utf-8") as fh:
            html = fh.read()
        before = conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE filing_id = ?", (f["id"],)
        ).fetchone()[0]

        models.delete_trades_for_filing(conn, f["id"])
        trades = senate_ptr_parser.parse_report_html(html)
        for trade in trades:
            trade["notification_date"] = conn.execute(
                "SELECT filing_date FROM filings WHERE id = ?", (f["id"],)
            ).fetchone()[0]
            models.insert_trade(conn, filing_id=f["id"], **trade)

        if len(trades) != before:
            changed += 1
            print(f"filing {f['id']}: {before} -> {len(trades)} trades")

    print(f"Re-parsed {len(filings)} filings, {changed} had a trade-count change.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/sample.db")
