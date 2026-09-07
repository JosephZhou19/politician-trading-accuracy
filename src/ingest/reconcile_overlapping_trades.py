"""Reconciles trades that report the exact same real transaction across two unrelated
filings for the same legislator - no amendment, no shared nominal_date, nothing connecting
them except that the content matches (see reconcile_amendments.py for the linked case).

Confirmed real example: two Whitehouse filings, neither flagged as an amendment, with
overlapping transaction-date coverage - the later one re-discloses a few trades the earlier
one already reported, alongside genuinely new ones. Matching key: (legislator, ticker or
asset_name, transaction_date, transaction_type, amount_low, amount_high, owner) across
different filing_ids.

Owner and transaction_type MUST both be part of the match - verified necessary on real data,
not assumed: dropping either produces dozens of false matches, since it's common and
legitimate for self/spouse/joint accounts to trade the same stock for the same amount on the
same day (mirrored household trading), which looks identical to a duplicate if owner isn't
checked.

Deliberately NOT gated on any date-proximity check (transaction-date range, or how close
together the two filings were filed). Two attempts at that were tried and dropped:
transaction-date overlap is a no-op (the matched trade's own date is by construction in both
filings' ranges, so ranges always "overlap" whenever a match exists at all - it never
actually rejects anything); and filing-date proximity is unreliable because real filings can
be years late, so "filed close together" doesn't reliably distinguish a genuine re-filing
from a coincidence. The match key itself (owner pinned to a specific person/account, AND
ticker, AND exact date, AND type, AND amount bracket all independently coinciding) is strong
enough evidence on its own: verified with zero false positives across Whitehouse's entire
decade-plus trading history (~800+ trades, only the 4 real duplicates matched, no date
filtering involved). Caveat worth re-checking once more legislators are in the DB: only
validated against 3 legislators so far.

This is a content-based heuristic, not metadata like the amendment case, so a tie (two
candidate filings filed on the exact same date, no way to tell which is authoritative) is
left unresolved rather than guessed - same philosophy as the amendment case's ambiguous
groups.
"""

import logging
from collections import defaultdict

from src.db import models

logger = logging.getLogger(__name__)


def reconcile_overlapping_trades(conn):
    """Mark superseded trades (superseded_by_trade_id). Returns a summary dict."""
    rows = conn.execute("""
        SELECT t.id, t.filing_id, f.legislator_id, f.filing_date,
               t.ticker, t.asset_name, t.transaction_date, t.transaction_type,
               t.amount_low, t.amount_high, t.owner
        FROM trades t
        JOIN filings f ON t.filing_id = f.id
        WHERE f.superseded_by_filing_id IS NULL
          AND t.superseded_by_trade_id IS NULL
    """).fetchall()

    groups = defaultdict(list)
    for r in rows:
        instrument = r["ticker"] or r["asset_name"]
        key = (
            r["legislator_id"], instrument, r["transaction_date"], r["transaction_type"],
            r["amount_low"], r["amount_high"], r["owner"],
        )
        groups[key].append(r)

    summary = {"groups_checked": 0, "trades_superseded": 0, "groups_tied": 0}

    for key, members in groups.items():
        distinct_filings = {m["filing_id"] for m in members}
        if len(distinct_filings) < 2:
            continue
        summary["groups_checked"] += 1

        max_date = max((m["filing_date"] or "") for m in members)
        winners = [m for m in members if (m["filing_date"] or "") == max_date]
        if len(winners) > 1:
            summary["groups_tied"] += 1
            logger.warning("Overlapping-trade reconciliation tied, skipping: %s", key)
            continue

        winner = winners[0]
        for m in members:
            if m["id"] != winner["id"]:
                models.set_trade_superseded(conn, m["id"], winner["id"])
                summary["trades_superseded"] += 1

    return summary
