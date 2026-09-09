"""Reconciles Senate PTR amendments against the originals they correct.

Senate report titles say "for MM/DD/YYYY" - equal to filing_date for a normal filing, but
for an amendment it's the ORIGINAL's date, the only reference an amendment carries to what
it corrects. Grouping filings by (legislator_id, nominal_date) clusters an original with
its whole amendment chain, since every amendment in a chain references the original's
date, not the previous amendment's.

When an amendment's nominal_date matches more than one original (e.g. a rebalance split
across two same-day submissions, one later amended), trade-content overlap picks which one
it belongs to. Resolving at the filing level - not just the overlapping trades - matters
because an amendment can also correct a trade's content (e.g. a ticker typo), which a
trade-level match could never catch since the changed field is exactly what breaks the
match.

Content-matching here is deliberately scoped to amendments only: an explicit "(Amendment)"
reference already proves the two filings are linked, and content just picks *which*
original among several candidates - it never invents a link between filings that carry no
such reference. A content-only mechanism for *unrelated* filings was tried and reverted:
without a transaction-level timestamp, an identical-looking trade in two unrelated filings
can't be proven to be a restatement rather than a genuinely separate second trade.

Re-runnable and incremental: already-superseded filings are excluded from re-consideration.
House isn't handled here - no equivalent reference is exposed there, and no House PTR
amendment has been observed to exist at all.
"""

import logging
from collections import defaultdict

from src.db import models

logger = logging.getLogger(__name__)


def _trade_keys_for_filing(conn, filing_id):
    """Content key per trade in a filing, used only to pick which original an amendment's
    nominal_date-ambiguous group belongs to - never to link two filings that lack an
    explicit amendment reference in the first place."""
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
    to the coarser filing_date, which can genuinely tie when multiple amendments are filed
    the same calendar day."""
    return m["filed_at"] or m["filing_date"] or ""


def _amendment_ranks(chain):
    """If every amendment in the chain has an explicit sequence number ("(Amendment N)"),
    rank the whole chain purely by that number - original = rank 0, Amendment N = rank N.
    A direct instruction from the source, more authoritative than inferring order from
    timestamps. Returns None if any amendment in the chain lacks a number (true of older,
    unnumbered amendments), so the caller falls back to time-based ordering."""
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
    amendment is *for*. If the winning signal still ties between two members, nothing is
    guessed: the whole chain is left alone and flagged via reconciliation_note."""
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
