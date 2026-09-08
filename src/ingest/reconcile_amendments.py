"""Reconciles Senate PTR amendments against the originals they correct.

Senate report titles say "for MM/DD/YYYY" - equal to filing_date for a normal filing, but
for an amendment it's the ORIGINAL's date (the only reference an amendment carries to what
it corrects - confirmed against a real amendment document, which just restates every
transaction with no pointer back to the original). Grouping filings by (legislator_id,
nominal_date) clusters an original with all its amendments, since every amendment in a
chain references the original's date, not the previous amendment's.

Real case confirmed in Boozman's data: a legislator filed TWO originals on the same date
(one all sells, one all buys - a rebalance split across two submissions) and later amended
one of them. Metadata alone (nominal_date) can't say which - both originals share it. But
the amendment's own trade content matched one candidate almost entirely (13 of 14 trades)
and the other not at all, which is decisive evidence even though the date reference isn't.
Using content this way also matters for correctness beyond just resolving the ambiguity:
one of that amendment's 14 trades had its ticker corrected (RNWAX -> RNWGX) - a trade-level
content match (see reconcile_overlapping_trades.py) can never catch that, since the ticker
IS part of what changed. Resolving at the filing level sweeps up every trade in the
superseded original, including the ones whose content the amendment corrected, not just the
ones it restated unchanged.

Re-runnable and incremental: already-superseded filings are excluded from re-consideration,
so a later run only needs to look at whatever's still "current" plus anything new. House
isn't handled here - no equivalent reference is exposed there, and no House PTR amendment
has been observed to exist at all (checked 2020-2026 live).
"""

import logging
from collections import defaultdict

from src.db import models

logger = logging.getLogger(__name__)


def _trade_keys_for_filing(conn, filing_id):
    """Content key per trade in a filing, for matching against another filing's trades -
    same shape as reconcile_overlapping_trades.py's matching key."""
    rows = conn.execute(
        """SELECT ticker, asset_name, transaction_date, transaction_type, amount_low,
                  amount_high, owner
           FROM trades WHERE filing_id = ?""",
        (filing_id,),
    ).fetchall()
    return {
        (r["ticker"] or r["asset_name"], r["transaction_date"], r["transaction_type"],
         r["amount_low"], r["amount_high"], r["owner"])
        for r in rows
    }


def _effective_time(m):
    """filed_at (precise "Filed ... @ H:MM AM/PM" timestamp) when available, falling back
    to the coarser filing_date. Needed because filing_date alone can genuinely tie: three
    real Whitehouse amendments were all filed on the identical calendar day, and only the
    precise timestamp (9:41 AM, 3:42 PM, 4:15 PM) actually orders them."""
    return m["filed_at"] or m["filing_date"] or ""


def _amendment_ranks(chain):
    """If every amendment in the chain has an explicit sequence number ("(Amendment N)"),
    rank the whole chain purely by that number - original = rank 0, Amendment N = rank N.
    This is a direct instruction from the source (confirmed: Senate's own numbering runs in
    ascending order of actual filing time), more authoritative than inferring order from
    timestamps. Returns None if any amendment in the chain lacks a number, so the caller
    falls back to time-based ordering - true of older amendments, which just say
    "(Amendment)" with no number."""
    ranks = {}
    for m in chain:
        if not m["is_amendment"]:
            ranks[m["id"]] = 0
        elif m["amendment_number"] is not None:
            ranks[m["id"]] = m["amendment_number"]
        else:
            return None
    return ranks


def _resolve_chain(conn, chain, summary):
    """Whichever member is authoritative supersedes everything else in the chain.
    Preference order: (1) explicit amendment number, when every amendment in the chain has
    one; (2) effective filing time (filed_at, falling back to filing_date), with an
    amendment always outranking a non-amendment it ties with on time, since that's what an
    amendment is *for* - a real, confirmed case: an original and the amendment correcting it
    were both recorded with the identical filing_date. If the winning signal still ties
    between two members (e.g. two amendments sharing both a number and the same effective
    time - not observed, but not assumed away either), nothing is guessed: the whole chain
    is left alone and flagged via reconciliation_note, same philosophy as the ambiguous-
    original case."""
    ranks = _amendment_ranks(chain)
    if ranks is not None:
        key = lambda m: ranks[m["id"]]
        tie_label = lambda top: f"amendment number {key(top)}"
    else:
        key = lambda m: (_effective_time(m), 1 if m["is_amendment"] else 0)
        tie_label = lambda top: f"effective filing time {_effective_time(top)}"

    ordered = sorted(chain, key=key)
    top = ordered[-1]
    tied_with_top = [m for m in ordered if key(m) == key(top)]
    if len(tied_with_top) > 1:
        note = (
            f"tied: {len(tied_with_top)} filings share the same {tie_label(top)}; "
            f"cannot determine which is authoritative"
        )
        for m in tied_with_top:
            models.set_reconciliation_note(conn, m["id"], note)
            logger.warning("Amendment reconciliation tied: filing %d - %s", m["id"], note)
        summary["chains_tied"] += 1
        return

    current = ordered[-1]
    for m in ordered[:-1]:
        models.set_superseded(conn, m["id"], current["id"])
        summary["filings_superseded"] += 1


def reconcile_senate_amendments(conn):
    """Mark superseded Senate filings (superseded_by_filing_id). Groups where an
    amendment's nominal_date matches more than one original try content-based
    disambiguation first (see module docstring); if that's ALSO inconclusive (content
    matches none or more than one candidate), the group is left unresolved and flagged via
    reconciliation_note instead of guessing - a wrong guess would silently hide a real
    filing's trades. Returns a summary dict."""
    rows = conn.execute("""
        SELECT id, legislator_id, nominal_date, filing_date, filed_at, is_amendment,
               amendment_number
        FROM filings
        WHERE chamber = 'senate' AND nominal_date IS NOT NULL
          AND superseded_by_filing_id IS NULL
    """).fetchall()

    groups = defaultdict(list)
    for row in rows:
        groups[(row["legislator_id"], row["nominal_date"])].append(row)

    summary = {
        "groups_with_amendments": 0, "filings_superseded": 0,
        "groups_ambiguous": 0, "groups_resolved_by_content": 0, "chains_tied": 0,
    }

    for (legislator_id, nominal_date), members in groups.items():
        amendments = [m for m in members if m["is_amendment"]]
        if not amendments:
            # No amendment in this group - either a single filing, or multiple genuinely
            # separate originals that happen to share a date. Nothing to reconcile.
            continue
        summary["groups_with_amendments"] += 1

        originals = [m for m in members if not m["is_amendment"]]
        if len(originals) > 1:
            amendment_keys = set()
            for a in amendments:
                amendment_keys |= _trade_keys_for_filing(conn, a["id"])
            overlap = {
                o["id"]: len(_trade_keys_for_filing(conn, o["id"]) & amendment_keys)
                for o in originals
            }
            matched = [oid for oid, count in overlap.items() if count > 0]

            if len(matched) == 1:
                resolved_original = next(o for o in originals if o["id"] == matched[0])
                _resolve_chain(conn, [resolved_original] + amendments, summary)
                summary["groups_resolved_by_content"] += 1
                continue

            note = (
                f"ambiguous: nominal_date {nominal_date} matches {len(originals)} original "
                f"filings for legislator_id {legislator_id}; cannot determine which this "
                f"amendment corrects (content overlap resolved {len(matched)} candidates, "
                f"needed exactly 1)"
            )
            for a in amendments:
                models.set_reconciliation_note(conn, a["id"], note)
                logger.warning("Amendment reconciliation ambiguous: filing %d - %s", a["id"], note)
            summary["groups_ambiguous"] += 1
            continue

        # Zero or one original plus one or more amendments in a chain.
        _resolve_chain(conn, members, summary)

    return summary
