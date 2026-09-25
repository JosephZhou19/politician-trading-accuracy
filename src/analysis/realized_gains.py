"""Per-legislator realized gain via FIFO dollar-lot matching, to pair with the unrealized
gain already in politician_totals.

Congressional disclosures report a DOLLAR RANGE per trade, not a share count, so lot
matching here tracks dollars through a FIFO queue of "buy lots" rather than share lots -
the same amount_mid midpoint-of-bracket estimate used everywhere else in this codebase
(politician_ticker_positions, politician_totals). A sale consumes dollars from the oldest
open lot(s) first; the realized gain on the consumed portion is
consumed_dollars * (sale_price - lot_price) / lot_price, only computed where both prices
are known - a trade missing price_at_transaction (or a sale exceeding everything ever
bought - a pre-2012 holding, an inherited/gifted position, or a missing filing) adds to
unpriced_sale_dollars instead of silently assuming a $0 cost basis.

This is a Python simulation, not a SQL view, because FIFO lot matching is inherently
sequential (each sale's outcome depends on the exact order and remaining size of prior
lots) - expressible in SQL via window functions and a triangular self-join, but a plain
loop is far easier to get right and to unit-test lot-by-lot. It still reads directly from
trades/filings on every call (no cached/duplicated state to drift), matching this
project's single-source-of-truth convention.

sale_full and sale_partial are treated identically (both just consume FIFO dollars) -
sale_full is the filer's own characterization of one specific transaction, not a
guarantee that this (legislator, ticker) pool's net position is fully zeroed elsewhere
(e.g. a separate spouse/joint sub-holding), so there's no special "clear all remaining
lots" case for it.

Trade selection matches politician_ticker_positions/politician_totals exactly (asset_type,
ticker, transaction_type scope; no superseded-trade filtering) so realized and unrealized
figures stay comparable rather than silently diverging in scope.

Coverage caveat (confirmed against production, not theoretical): House/Senate disclosure
data only goes back to 2012, so a position acquired before then and sold after shows up as
a sale with no purchase to match against - that's 94% of unpriced_sale_dollars overall
(missing price data on a trade that IS matched is only 6%). This hits long-tenured members
hardest: median sale_coverage (realized_proceeds / (realized_proceeds +
unpriced_sale_dollars)) across all legislators is ~8%. Always check a legislator's
coverage before trusting their realized_gain figure in isolation.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass


@dataclass
class RealizedGainResult:
    legislator_id: int
    realized_gain: float = 0.0
    realized_proceeds: float = 0.0
    unpriced_sale_dollars: float = 0.0


def compute_realized_gains(conn: sqlite3.Connection) -> dict[int, RealizedGainResult]:
    """Per-legislator realized gain across their full stock trade history. See module
    docstring for methodology. Every legislator with at least one qualifying trade gets an
    entry, even if it's all zeros (no sales yet is a real 0.0, not missing data)."""
    rows = conn.execute(
        """
        SELECT f.legislator_id, t.ticker, t.transaction_type,
               t.amount_low, t.amount_high, t.price_at_transaction
        FROM trades t JOIN filings f ON f.id = t.filing_id
        WHERE t.asset_type IN ('ST', 'Stock')
          AND t.ticker IS NOT NULL AND t.ticker != ''
          AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
        ORDER BY f.legislator_id, t.ticker, t.transaction_date, t.id
        """
    ).fetchall()

    results: dict[int, RealizedGainResult] = {}
    lots: deque[list] = deque()
    current_key = None

    for row in rows:
        key = (row["legislator_id"], row["ticker"])
        if key != current_key:
            lots = deque()
            current_key = key

        result = results.setdefault(row["legislator_id"], RealizedGainResult(row["legislator_id"]))
        amount_mid = (row["amount_low"] + (row["amount_high"] or row["amount_low"])) / 2.0

        if row["transaction_type"] == "purchase":
            lots.append([amount_mid, row["price_at_transaction"]])
            continue

        remaining = amount_mid
        sale_price = row["price_at_transaction"]
        while remaining > 1e-9 and lots:
            lot = lots[0]
            consumed = min(remaining, lot[0])
            if lot[1] is not None and sale_price is not None:
                result.realized_gain += consumed * (sale_price - lot[1]) / lot[1]
                result.realized_proceeds += consumed
            else:
                result.unpriced_sale_dollars += consumed
            lot[0] -= consumed
            remaining -= consumed
            if lot[0] <= 1e-9:
                lots.popleft()
        if remaining > 1e-9:
            result.unpriced_sale_dollars += remaining

    return results
