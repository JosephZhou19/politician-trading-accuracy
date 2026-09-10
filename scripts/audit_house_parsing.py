"""Read-only audit: classify every House filing currently marked needs_ocr (genuine
scan vs recoverable now that house_ptr_parser.py derives columns per-document), and
re-parse every already-'parsed' House filing to catch any trade-count regression
from the current parser, across the real dataset rather than a handful of samples.

Makes no changes to the DB or any files.
"""
import sqlite3

import pdfplumber

from src.parse import house_ptr_parser as hp

VALID_OWNERS = {"self", "spouse", "joint", "dependent_child"}
VALID_TYPES = {"purchase", "sale_full", "sale_partial", "exchange"}


def classify_needs_ocr(conn):
    rows = conn.execute("""
        SELECT f.id, f.raw_file_path, l.last_name
        FROM filings f JOIN legislators l ON f.legislator_id = l.id
        WHERE f.parse_status = 'needs_ocr' AND f.chamber = 'house'
          AND f.document_format = 'image'
    """).fetchall()

    print(f"=== {len(rows)} House filings marked needs_ocr ===")
    genuine_scan, mislabeled_clean, mislabeled_dirty, unclear = [], [], [], []

    for r in rows:
        try:
            with pdfplumber.open(r["raw_file_path"]) as pdf:
                full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                columns = hp._derive_columns(pdf)
                raw_rows = hp._extract_raw_rows(pdf, columns) if columns else []
        except Exception as e:
            unclear.append((r, f"open error: {e!r}"))
            continue

        if len(full_text) < 50:
            genuine_scan.append(r)
            continue
        if columns is None:
            unclear.append((r, "has text but no column header could be located"))
            continue

        if not raw_rows:
            mislabeled_dirty.append((r, "zero rows extracted"))
            continue

        bad = []
        for row in raw_rows:
            owner = hp._canonicalize_owner(row["owner_raw"])
            ttype = hp._canonicalize_transaction_type(row["type_raw"])
            if owner not in VALID_OWNERS:
                bad.append(f"owner={row['owner_raw']!r}")
            if ttype not in VALID_TYPES:
                bad.append(f"type={row['type_raw']!r}")
        if bad:
            mislabeled_dirty.append((r, f"{len(raw_rows)} rows, bad values: {bad[:5]}"))
        else:
            mislabeled_clean.append((r, len(raw_rows)))

    print(f"genuine scans (near-zero text, real OCR needed): {len(genuine_scan)}")
    by_name = {}
    for r in genuine_scan:
        by_name[r["last_name"]] = by_name.get(r["last_name"], 0) + 1
    print(f"  by rep: {by_name}")

    print(f"\nmislabeled but would parse CLEANLY if detection were fixed: {len(mislabeled_clean)}")
    for r, n in mislabeled_clean:
        print(f"  filing {r['id']} ({r['last_name']}): {n} rows, all canonicalize fine")

    print(f"\nmislabeled AND would still fail/misparse (needs real parser work): {len(mislabeled_dirty)}")
    for r, reason in mislabeled_dirty:
        print(f"  filing {r['id']} ({r['last_name']}): {reason}")

    if unclear:
        print(f"\nunclear/errored: {len(unclear)}")
        for r, reason in unclear:
            print(f"  filing {r['id']} ({r['last_name']}): {reason}")


def audit_parsed_filings(conn):
    """Re-parse every already-'parsed' House filing with the current parser and
    compare against the trade count already stored in the DB, to catch any
    regression (or further silent under-count) across the real dataset, not just
    the handful of filings manually spot-checked during development."""
    rows = conn.execute("""
        SELECT f.id, f.raw_file_path, l.last_name, COUNT(t.id) trade_count
        FROM filings f
        JOIN legislators l ON f.legislator_id = l.id
        LEFT JOIN trades t ON t.filing_id = f.id
        WHERE f.chamber = 'house' AND f.parse_status = 'parsed'
        GROUP BY f.id
    """).fetchall()

    print(f"\n=== {len(rows)} House filings marked 'parsed' - comparing against a fresh re-parse ===")
    changed = []
    errored = []
    for r in rows:
        try:
            reparsed = hp.parse_filing(r["raw_file_path"])
        except Exception as e:
            errored.append((r, repr(e)))
            continue
        new_count = len(reparsed["trades"])
        if new_count != r["trade_count"]:
            changed.append((r, r["trade_count"], new_count))

    print(f"filings whose trade count changed on re-parse: {len(changed)}")
    for r, old, new in changed:
        print(f"  filing {r['id']} ({r['last_name']}): {old} -> {new}")
    if errored:
        print(f"filings that now raise instead of parsing: {len(errored)}")
        for r, err in errored:
            print(f"  filing {r['id']} ({r['last_name']}): {err}")


if __name__ == "__main__":
    conn = sqlite3.connect("data/sample.db")
    conn.row_factory = sqlite3.Row
    classify_needs_ocr(conn)
    audit_parsed_filings(conn)
