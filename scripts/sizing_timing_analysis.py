"""Sizing-timing test (copytrading analysis, layer 8): for a legislator's biggest winning
positions, was the money mostly committed BEFORE the price had moved (consistent with
deliberate conviction/information) or added AFTER the price had already run up (consistent
with chasing an already-obvious winner, or just lucky averaging)?

For each (legislator, ticker) group of stock purchases:
  - front_load_ratio: dollar share of the chronologically FIRST purchase out of the total
    invested in that position. 1.0 = all-in on day one; low = built up gradually.
  - runup_before_last_buy_pct: how much the price had already moved between the first and
    last purchase, i.e. what fraction of the eventual move (if any) had already happened
    before the position was fully sized up.
  - outcome_365d_pct: dollar-weighted average forward return using each purchase's own
    price 365 days out (a fixed one-year-out benchmark price, independent of today's live
    price, looked up from ticker_daily_prices via src.analysis.price_lookup - see PLAN.md
    for why the old price_at_transaction/price_365d columns were retired in favor of this).

Run against the local mirror (scripts/sync_local_mirror.py) - read-only, zero Turso cost.
"""
import datetime
import sqlite3
from collections import defaultdict

from src.analysis.price_lookup import load_price_histories

DB_PATH = "data/congress_trades.db"


def load_purchases(conn):
    """Returns dicts (not sqlite3.Row - build_positions needs no changes either way, since
    both support the same ["key"] access) with price_at_transaction/price_365d computed via
    the price-lookup instead of read from columns."""
    query = """
        SELECT f.legislator_id, l.first_name, l.last_name, l.chamber,
               t.ticker, t.transaction_date,
               (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid
        FROM trades t
        JOIN filings f ON f.id = t.filing_id
        JOIN legislators l ON l.id = f.legislator_id
        WHERE t.transaction_type = 'purchase'
          AND t.asset_type IN ('ST', 'Stock')
          AND t.ticker IS NOT NULL AND t.ticker != ''
        ORDER BY f.legislator_id, t.ticker, t.transaction_date
    """
    rows = conn.execute(query).fetchall()
    histories = load_price_histories(conn, [r["ticker"] for r in rows])

    purchases = []
    for r in rows:
        history = histories.get(r["ticker"])
        if history is None:
            continue
        transaction_date = datetime.date.fromisoformat(r["transaction_date"])
        price_at_transaction = history.price_on_or_after(transaction_date)
        if price_at_transaction is None:
            continue
        purchases.append({
            "legislator_id": r["legislator_id"], "first_name": r["first_name"],
            "last_name": r["last_name"], "chamber": r["chamber"], "ticker": r["ticker"],
            "transaction_date": r["transaction_date"], "amount_mid": r["amount_mid"],
            "price_at_transaction": price_at_transaction,
            "price_365d": history.price_on_or_after(transaction_date + datetime.timedelta(days=365)),
        })
    return purchases


def build_positions(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["legislator_id"], r["ticker"])].append(r)

    positions = []
    for (leg_id, ticker), trades in groups.items():
        total_invested = sum(t["amount_mid"] for t in trades)
        if total_invested <= 0:
            continue
        first, last = trades[0], trades[-1]
        front_load_ratio = trades[0]["amount_mid"] / total_invested

        runup_before_last_buy_pct = None
        if len(trades) > 1 and first["price_at_transaction"]:
            runup_before_last_buy_pct = (
                (last["price_at_transaction"] - first["price_at_transaction"])
                / first["price_at_transaction"]
            )

        priced = [t for t in trades if t["price_365d"] is not None]
        outcome_365d_pct = None
        dollar_gain_365d = None
        if priced:
            priced_dollars = sum(t["amount_mid"] for t in priced)
            weighted_return = sum(
                t["amount_mid"] * (t["price_365d"] - t["price_at_transaction"])
                / t["price_at_transaction"]
                for t in priced
            ) / priced_dollars
            outcome_365d_pct = weighted_return
            # Scaled to the position's full invested amount, not just the priced subset -
            # a reasonable estimate as long as the priced subset isn't wildly unrepresentative.
            dollar_gain_365d = total_invested * weighted_return

        positions.append({
            "legislator": f"{first['first_name']} {first['last_name']}",
            "chamber": first["chamber"],
            "ticker": ticker,
            "n_purchases": len(trades),
            "total_invested": total_invested,
            "front_load_ratio": front_load_ratio,
            "runup_before_last_buy_pct": runup_before_last_buy_pct,
            "outcome_365d_pct": outcome_365d_pct,
            "dollar_gain_365d": dollar_gain_365d,
            "first_date": first["transaction_date"],
            "last_date": last["transaction_date"],
        })
    return positions


def pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    positions = build_positions(load_purchases(conn))

    priced_positions = [p for p in positions if p["dollar_gain_365d"] is not None]
    top20 = sorted(priced_positions, key=lambda p: p["dollar_gain_365d"], reverse=True)[:20]

    print(f"{len(positions)} (legislator, ticker) positions, {len(priced_positions)} with a "
          f"365d outcome price.\n")
    print("Top 20 positions by estimated 365d dollar gain, with sizing-timing detail:")
    print(f"{'legislator':<22}{'ticker':<8}{'$gain365d':>12}{'n_buys':>7}"
          f"{'front_load':>11}{'runup_pre_last_buy':>20}")
    for p in top20:
        runup = f"{p['runup_before_last_buy_pct']:.1%}" if p["runup_before_last_buy_pct"] is not None else "n/a (1 buy)"
        print(f"{p['legislator']:<22}{p['ticker']:<8}{p['dollar_gain_365d']:>12,.0f}"
              f"{p['n_purchases']:>7}{p['front_load_ratio']:>10.1%} {runup:>20}")

    multi_buy_priced = [
        p for p in priced_positions
        if p["n_purchases"] > 1 and p["runup_before_last_buy_pct"] is not None
    ]
    print(f"\n{len(multi_buy_priced)} multi-purchase positions with both a runup and an "
          f"outcome measure.")

    r_frontload = pearson_r(
        [p["front_load_ratio"] for p in multi_buy_priced],
        [p["outcome_365d_pct"] for p in multi_buy_priced],
    )
    r_runup = pearson_r(
        [p["runup_before_last_buy_pct"] for p in multi_buy_priced],
        [p["outcome_365d_pct"] for p in multi_buy_priced],
    )
    print(f"Pearson r(front_load_ratio, outcome_365d_pct) = {r_frontload:.3f}"
          if r_frontload is not None else "front_load correlation: n/a")
    print(f"Pearson r(runup_before_last_buy_pct, outcome_365d_pct) = {r_runup:.3f}"
          if r_runup is not None else "runup correlation: n/a")

    single_buy = [p for p in priced_positions if p["n_purchases"] == 1]
    multi_buy = [p for p in priced_positions if p["n_purchases"] > 1]
    avg_single = sum(p["outcome_365d_pct"] for p in single_buy) / len(single_buy)
    avg_multi = sum(p["outcome_365d_pct"] for p in multi_buy) / len(multi_buy)
    print(f"\nAvg 365d return, single-purchase positions (n={len(single_buy)}): {avg_single:.1%}")
    print(f"Avg 365d return, multi-purchase (averaged-in) positions (n={len(multi_buy)}): {avg_multi:.1%}")

    conn.close()


if __name__ == "__main__":
    main()
