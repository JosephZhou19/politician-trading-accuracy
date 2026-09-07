"""Reconciles Senate PTR amendments against the originals they correct.

Senate report titles say "for MM/DD/YYYY" - equal to filing_date for a normal filing, but
for an amendment it's the ORIGINAL's date (the only reference an amendment carries to what
it corrects - confirmed against a real amendment document, which just restates every
transaction with no pointer back to the original). Grouping filings by (legislator_id,
nominal_date) clusters an original with all its amendments, since every amendment in a
chain references the original's date, not the previous amendment's.

Re-runnable and incremental: already-superseded filings are excluded from re-consideration,
so a later run only needs to look at whatever's still "current" plus anything new. House
isn't handled here - no equivalent reference is exposed there, and no House PTR amendment
has been observed to exist at all (checked 2020-2026 live).
"""

import logging
from collections import defaultdict

from src.db import models

logger = logging.getLogger(__name__)


def reconcile_senate_amendments(conn):
    """Mark superseded Senate filings (superseded_by_filing_id). Groups where an
    amendment's nominal_date matches more than one original are left unresolved and
    flagged via reconciliation_note instead of guessing - a wrong guess would silently
    hide a real filing's trades. Returns a summary dict."""
    rows = conn.execute("""
        SELECT id, legislator_id, nominal_date, filing_date, is_amendment
        FROM filings
        WHERE chamber = 'senate' AND nominal_date IS NOT NULL
          AND superseded_by_filing_id IS NULL
    """).fetchall()

    groups = defaultdict(list)
    for row in rows:
        groups[(row["legislator_id"], row["nominal_date"])].append(row)

    summary = {"groups_with_amendments": 0, "filings_superseded": 0, "groups_ambiguous": 0}

    for (legislator_id, nominal_date), members in groups.items():
        amendments = [m for m in members if m["is_amendment"]]
        if not amendments:
            # No amendment in this group - either a single filing, or multiple genuinely
            # separate originals that happen to share a date. Nothing to reconcile.
            continue
        summary["groups_with_amendments"] += 1

        originals = [m for m in members if not m["is_amendment"]]
        if len(originals) > 1:
            note = (
                f"ambiguous: nominal_date {nominal_date} matches {len(originals)} original "
                f"filings for legislator_id {legislator_id}; cannot determine which this "
                f"amendment corrects"
            )
            for a in amendments:
                models.set_reconciliation_note(conn, a["id"], note)
                logger.warning("Amendment reconciliation ambiguous: filing %d - %s", a["id"], note)
            summary["groups_ambiguous"] += 1
            continue

        # Zero or one original plus one or more amendments in a chain - whichever was
        # actually filed most recently is current; everything else in the group is superseded.
        ordered = sorted(members, key=lambda m: m["filing_date"] or "")
        current = ordered[-1]
        for m in ordered[:-1]:
            models.set_superseded(conn, m["id"], current["id"])
            summary["filings_superseded"] += 1

    return summary
