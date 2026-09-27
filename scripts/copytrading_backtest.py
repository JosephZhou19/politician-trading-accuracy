"""Copytrading backtest (layer 9): does mechanically copying EVERY disclosed stock
purchase, at a realistically-executable price, produce a real edge over SPY when pooled
across the whole population - as opposed to the per-legislator win-rate significance tests
(layer 7), which ask a different question ("is any ONE person's pick rate better than a
coin flip") and found no individual with a statistically distinguishable edge?

Pooling matters here for a reason distinct from sample size: a copytrader isn't restricted
to following one legislator. Following a basket of many diversifies away the single-ticker
concentration layer 7 already found (Pelosi 56% from NVDA, Crenshaw 64% from one ETF, ...)
in a way no individual's own track record can. So the right test of "is copytrading worth
it" is a population-level one-sample test on trade-level alpha, not a per-person ranking.

Two entry conventions, both reported side by side:
- txn-anchored: entry price on transaction_date, exit price 365 days after transaction_date,
  SPY sampled at the same two calendar dates. Not realistically tradeable - a copytrader
  can't act before the trade is even disclosed - but is the apples-to-apples baseline the
  rest of this project's per-legislator numbers were built on.
- notification-lag-adjusted (layer 5's fix, applied here at the population level for the
  first time): entry price on notification_date, i.e. the actual first day a copytrader
  could have acted. Exit is still 365 days after the TRANSACTION date (not notification), so
  the realized holding period here is 365 days minus the notification lag (up to 45 days
  under the STOCK Act), not exactly 365 days from entry. Same approximation layer 5 used for
  its single-legislator check; noted, not solved, here. SPY is resampled at
  notification_date/+365d to match.

Every price (entry, exit, SPY) is looked up from ticker_daily_prices/benchmark_prices via
src.analysis.price_lookup rather than a pre-computed trades.price_* column - see PLAN.md for
why those columns were retired in favor of full daily history.

Run against the local mirror (scripts/sync_local_mirror.py) - read-only, zero Turso cost.
"""
import datetime
import sqlite3

from src.analysis.price_lookup import load_price_histories

DB_PATH = "data/congress_trades.db"

# 'OP' (options) is treated as equity throughout this analysis, on request (2026-09-23) -
# excluding it was dropping ~20% of some legislators' disclosed dollar volume (Pelosi: 46
# options purchases worth $32.35M, entirely uncounted before this). CAVEAT, real and
# unresolved: an option's disclosed dollar bracket is treated here as if it were straight
# stock exposure of that size, and "alpha" is computed off the UNDERLYING stock's price
# movement (the only price data this schema tracks for an options row) - not the option's
# actual leveraged P&L, which depends on strike/expiry/premium this DB doesn't have. A real
# option position of a given disclosed dollar size typically controls more notional stock
# exposure than that same dollar amount in the stock itself, so this systematically
# under- or over-states real option P&L in an unknown direction - it's the best proxy
# available, not a resolved measurement.
PRICEABLE_ASSET_TYPES = ("ST", "Stock", "OP")


def _plus_365(date_str):
    return (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=365)).isoformat()


def get_active_legislator_ids(conn, since):
    """Legislator ids with at least one trade in PRICEABLE_ASSET_TYPES (the same
    restriction load_trade_alphas itself uses, not "any trade of any kind") on or after
    `since`. A live "follow this person for stock-picking" recommendation must clear this
    before ranking/selection, separate from whether their historical alpha is good - a
    strong track record from someone no longer trading stocks/options isn't actionable for
    this analysis even if they're still trading something else.

    Confirmed necessary via two real failures, not theoretical: layer 11's #1 pick (Mark
    Green) had zero 2026 trades of any kind (last one 2025-06-24). Once that was fixed, the
    NEW #1 pick (Donald Beyer) turned out to have moved entirely into municipal bonds in
    2026 - 11 trades, all asset_type 'GS', zero tickers - so an earlier version of this
    function that checked "any trade, any asset type" still let him through. Both are the
    same underlying mistake: checking "still active" instead of "still active at the thing
    this analysis is about picking a stock-trader based on"."""
    rows = conn.execute(
        """SELECT DISTINCT f.legislator_id FROM trades t JOIN filings f ON f.id = t.filing_id
           WHERE t.transaction_date >= ? AND t.asset_type IN ('ST', 'Stock', 'OP')""",
        (since,),
    ).fetchall()
    return {r["legislator_id"] for r in rows}


def _spy_price_on_or_after(conn, cache, date_str, window_days=10):
    if date_str not in cache:
        row = conn.execute(
            """SELECT price FROM benchmark_prices
               WHERE date >= ? AND date <= date(?, ?)
               ORDER BY date ASC LIMIT 1""",
            (date_str, date_str, f"+{window_days} days"),
        ).fetchone()
        cache[date_str] = row["price"] if row else None
    return cache[date_str]


def load_trade_alphas(conn):
    """One row per stock/option purchase priced at both entry conventions (a trade is
    skipped only if it lacks what THAT convention needs - the two counts can differ). See
    PRICEABLE_ASSET_TYPES for the options-treated-as-equity caveat."""
    rows = conn.execute(
        """
        SELECT f.legislator_id, l.first_name, l.last_name, l.chamber,
               t.ticker, t.asset_type, t.transaction_date, t.notification_date,
               (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid
        FROM trades t
        JOIN filings f ON f.id = t.filing_id
        JOIN legislators l ON l.id = f.legislator_id
        WHERE t.transaction_type = 'purchase'
          AND t.asset_type IN ('ST', 'Stock', 'OP')
        """
    ).fetchall()

    histories = load_price_histories(conn, [r["ticker"] for r in rows])
    spy_cache = {}
    txn_results, lag_results = [], []

    for r in rows:
        history = histories.get(r["ticker"])
        if history is None:
            continue
        transaction_date = datetime.date.fromisoformat(r["transaction_date"])
        notification_date = datetime.date.fromisoformat(r["notification_date"])
        price_365d = history.price_on_or_after(transaction_date + datetime.timedelta(days=365))
        if price_365d is None:
            continue
        price_at_transaction = history.price_on_or_after(transaction_date)
        price_at_notification = history.price_on_or_after(notification_date)

        base = {
            "legislator_id": r["legislator_id"],
            "legislator": f"{r['first_name']} {r['last_name']}",
            "chamber": r["chamber"],
            "ticker": r["ticker"],
            "asset_type": r["asset_type"],
            "amount_mid": r["amount_mid"],
        }

        if price_at_transaction:
            spy_entry = _spy_price_on_or_after(conn, spy_cache, r["transaction_date"])
            spy_exit = _spy_price_on_or_after(conn, spy_cache, _plus_365(r["transaction_date"]))
            if spy_entry and spy_exit:
                stock_ret = (price_365d - price_at_transaction) / price_at_transaction
                spy_ret = (spy_exit - spy_entry) / spy_entry
                txn_results.append({**base, "alpha_pct": (stock_ret - spy_ret) * 100})

        if price_at_notification:
            spy_entry = _spy_price_on_or_after(conn, spy_cache, r["notification_date"])
            spy_exit = _spy_price_on_or_after(conn, spy_cache, _plus_365(r["notification_date"]))
            if spy_entry and spy_exit:
                stock_ret = (price_365d - price_at_notification) / price_at_notification
                spy_ret = (spy_exit - spy_entry) / spy_entry
                lag_results.append({**base, "alpha_pct": (stock_ret - spy_ret) * 100})

    return txn_results, lag_results


def _mean(xs):
    return sum(xs) / len(xs)


def _stdev(xs, mean):
    n = len(xs)
    if n < 2:
        return 0.0
    return (sum((x - mean) ** 2 for x in xs) / (n - 1)) ** 0.5


def _dollar_weighted_mean(rows):
    total = sum(r["amount_mid"] for r in rows)
    return sum(r["amount_mid"] * r["alpha_pct"] for r in rows) / total


def _t_stat_and_p(xs):
    """One-sample t-test of mean(xs) != 0. p-value via a normal approximation (n is in the
    thousands here, so t and normal are indistinguishable to any decimal place that
    matters) rather than pulling in scipy for one number."""
    n = len(xs)
    mean = _mean(xs)
    se = _stdev(xs, mean) / (n ** 0.5)
    if se == 0:
        return mean, float("inf"), 0.0
    t = mean / se
    # Two-sided p-value from the standard normal CDF via erf.
    import math
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / (2 ** 0.5))))
    return mean, t, p


def summarize(label, rows):
    alphas = [r["alpha_pct"] for r in rows]
    mean, t, p = _t_stat_and_p(alphas)
    dw_mean = _dollar_weighted_mean(rows)
    median = sorted(alphas)[len(alphas) // 2]
    pct_positive = sum(1 for a in alphas if a > 0) / len(alphas) * 100
    print(f"\n{label} (n={len(rows)})")
    print(f"  equal-weighted mean alpha vs SPY: {mean:+.2f}%  (t={t:.2f}, two-sided p={p:.4f})")
    print(f"  dollar-weighted mean alpha vs SPY: {dw_mean:+.2f}%")
    print(f"  median alpha vs SPY: {median:+.2f}%")
    print(f"  % of trades beating SPY: {pct_positive:.1f}%")
    return {"mean": mean, "t": t, "p": p, "dw_mean": dw_mean, "median": median, "pct_positive": pct_positive}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    txn_results, lag_results = load_trade_alphas(conn)
    conn.close()

    summarize("Transaction-date entry (not realistically tradeable - baseline)", txn_results)
    summarize("Notification-date entry (realistically tradeable - the actual copytrading test)", lag_results)

    print("\nExcluding each legislator's single largest-alpha-dollar position, notification-lag entry:")
    by_leg = {}
    for r in lag_results:
        by_leg.setdefault(r["legislator_id"], []).append(r)
    trimmed = []
    for leg_rows in by_leg.values():
        if len(leg_rows) < 2:
            continue
        biggest = max(leg_rows, key=lambda r: r["amount_mid"] * max(r["alpha_pct"], 0) / 100)
        trimmed.extend(r for r in leg_rows if r is not biggest)
    summarize("With each legislator's biggest winning-alpha-dollar bet removed", trimmed)


if __name__ == "__main__":
    main()
