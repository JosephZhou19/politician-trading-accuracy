"""Copytrading backtest (layer 10): "what if I'd only copied the top 10 traders?" - the
natural follow-up to layer 9's population-wide result, and a natural place to accidentally
fool yourself, so this reports it two ways.

1. In-sample "top 10 by all-time alpha, backtested on those same all-time trades" - included
   ONLY to show why it's not a real answer: ranking people by their own historical alpha and
   then checking whether their historical alpha was good is circular by construction. It
   will look great no matter what, the same way it would for a mutual fund's own trailing
   returns. Reported for contrast, not as evidence.

2. Out-of-sample "top 10 by FIRST-half alpha, backtested on their SECOND-half trades only" -
   reuses layer 6's own persistence-test split (each legislator's trade history divided at
   their own median trade date) but for a stronger claim than layer 6's r=0.343 correlation:
   this asks "does PICKING today's apparent top 10 and following them from here actually
   make money," which is what a real copytrader would do. This is the only one of the two
   that means anything as a forward-looking answer.

Both rankings use dollar-weighted, notification-lag-adjusted alpha vs SPY (same convention
as scripts/copytrading_backtest.py's realistic test), restricted to legislators with >=5
priced trades in the relevant window so a 1-trade fluke can't make the cut.

Run against the local mirror (scripts/sync_local_mirror.py) - read-only, zero Turso cost.
"""
import datetime
import sqlite3

from scripts.copytrading_backtest import (
    _plus_365,
    _spy_price_on_or_after,
    get_active_legislator_ids,
    load_trade_alphas,
    summarize,
)
from src.analysis.price_lookup import load_price_histories

DB_PATH = "data/congress_trades.db"
MIN_TRADES = 5
TOP_N = 10
# A recommendation must come from someone still actually trading - see
# get_active_legislator_ids's docstring for why.
ACTIVE_SINCE = "2026-01-01"


def _dollar_weighted_alpha(rows):
    total = sum(r["amount_mid"] for r in rows)
    return sum(r["amount_mid"] * r["alpha_pct"] for r in rows) / total


def rank_top_n(rows, active_legislator_ids, min_trades=MIN_TRADES, top_n=TOP_N):
    by_leg = {}
    for r in rows:
        by_leg.setdefault(r["legislator_id"], []).append(r)
    ranked = [
        (leg_id, _dollar_weighted_alpha(leg_rows), len(leg_rows), leg_rows[0]["legislator"])
        for leg_id, leg_rows in by_leg.items()
        if len(leg_rows) >= min_trades and leg_id in active_legislator_ids
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:top_n]


def print_ranking(label, ranked):
    print(f"\n{label}")
    for leg_id, alpha, n, name in ranked:
        print(f"  {name:<22} alpha={alpha:+7.1f}%  n={n}")


def in_sample_test(lag_results, active_legislator_ids):
    ranked = rank_top_n(lag_results, active_legislator_ids)
    print_ranking(f"In-sample top {TOP_N} by all-time dollar-weighted alpha (>= {MIN_TRADES} trades):", ranked)
    top_ids = {leg_id for leg_id, *_ in ranked}
    subset = [r for r in lag_results if r["legislator_id"] in top_ids]
    print("\n[CIRCULAR - not a real forward test, included for contrast only]")
    summarize("All-time trades of the all-time-ranked top 10", subset)


def _median_date(dates):
    s = sorted(dates)
    return s[len(s) // 2]


def out_of_sample_test(conn, active_legislator_ids):
    """Rank by first-half alpha, evaluate ONLY on each person's own second half - the
    legitimate "pick today's leaders, does following them from here pay off" test."""
    rows = conn.execute(
        """
        SELECT f.legislator_id, t.transaction_date
        FROM trades t
        JOIN filings f ON f.id = t.filing_id
        WHERE t.transaction_type = 'purchase' AND t.asset_type IN ('ST', 'Stock', 'OP')
        """
    ).fetchall()
    dates_by_leg = {}
    for r in rows:
        dates_by_leg.setdefault(r["legislator_id"], []).append(r["transaction_date"])
    medians = {leg_id: _median_date(dates) for leg_id, dates in dates_by_leg.items()}

    rows2 = conn.execute(
        """
        SELECT f.legislator_id, l.first_name, l.last_name,
               t.transaction_date, t.notification_date, t.ticker,
               (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid
        FROM trades t
        JOIN filings f ON f.id = t.filing_id
        JOIN legislators l ON l.id = f.legislator_id
        WHERE t.transaction_type = 'purchase' AND t.asset_type IN ('ST', 'Stock', 'OP')
        """
    ).fetchall()

    histories = load_price_histories(conn, [r["ticker"] for r in rows2])
    spy_cache = {}
    first_half, second_half = [], []
    for r in rows2:
        median = medians.get(r["legislator_id"])
        if median is None:
            continue
        history = histories.get(r["ticker"])
        if history is None:
            continue
        transaction_date = datetime.date.fromisoformat(r["transaction_date"])
        notification_date = datetime.date.fromisoformat(r["notification_date"])
        price_365d = history.price_on_or_after(transaction_date + datetime.timedelta(days=365))
        price_at_notification = history.price_on_or_after(notification_date)
        if price_365d is None or price_at_notification is None:
            continue
        spy_entry = _spy_price_on_or_after(conn, spy_cache, r["notification_date"])
        spy_exit = _spy_price_on_or_after(conn, spy_cache, _plus_365(r["notification_date"]))
        if not (spy_entry and spy_exit):
            continue
        stock_ret = (price_365d - price_at_notification) / price_at_notification
        spy_ret = (spy_exit - spy_entry) / spy_entry
        entry = {
            "legislator_id": r["legislator_id"],
            "legislator": f"{r['first_name']} {r['last_name']}",
            "ticker": r["ticker"],
            "amount_mid": r["amount_mid"],
            "alpha_pct": (stock_ret - spy_ret) * 100,
        }
        (first_half if r["transaction_date"] <= median else second_half).append(entry)

    ranked = rank_top_n(first_half, active_legislator_ids)
    print_ranking(
        f"\nOut-of-sample: top {TOP_N} by FIRST-half dollar-weighted alpha (>= {MIN_TRADES} first-half trades):",
        ranked,
    )
    top_ids = {leg_id for leg_id, *_ in ranked}
    their_second_half = [r for r in second_half if r["legislator_id"] in top_ids]
    print("\n[HONEST FORWARD TEST - this is the one that matters]")
    summarize("Second-half trades of the first-half-ranked top 10", their_second_half)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _, lag_results = load_trade_alphas(conn)
    active_ids = get_active_legislator_ids(conn, since=ACTIVE_SINCE)
    print(f"{len(active_ids)} legislator(s) have traded since {ACTIVE_SINCE}.")

    in_sample_test(lag_results, active_ids)
    out_of_sample_test(conn, active_ids)
    conn.close()


if __name__ == "__main__":
    main()
