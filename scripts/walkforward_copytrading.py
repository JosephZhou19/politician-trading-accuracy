"""Walk-forward validation of "pick the current top-10 by trailing alpha, follow them
forward" (layer 12) - the direct fix for crossval_top10.py's own documented limitation:
random 50/50 splits prove a legislator's alpha is *persistent* (first-half predicts
second-half), but not that mechanically picking today's leaders and following them from
here actually would have made money, since a real copytrader can't reshuffle time. A
random split can leak information backward across the cut in a way a real decision-maker
never could.

At each of several historical cutoff dates T, ranks legislators using ONLY trades fully
RESOLVED by T (both disclosed - notification_date <= T - and their 365-day outcome already
knowable - notification_date + 365 days <= T; using anything else would let information
from after the decision point leak into the decision), then measures that same group's
real, never-touched forward performance on trades notified in (T, T + FORWARD_YEARS]. Rolls
T forward by STEP_MONTHS and repeats - this is the standard walk-forward design for a
time-series prediction problem, as opposed to the i.i.d.-style random resampling
crossval_top10.py uses (valid for a different question - see its own docstring).

A trailing alpha estimate is shrunk toward the cutoff's own population mean in proportion
to its sample size (empirical-Bayes style, SHRINKAGE_K trades' worth of "prior weight")
before ranking - added after the Wasserman Schultz incident (2026-09-22 NOTES.md), where a
20-trade, 2-ticker-concentrated record beat legitimate 600+-trade histories on raw alpha
alone. Eligibility also requires recent activity AS OF the cutoff (a stock trade within
ACTIVE_LOOKBACK_YEARS before T) - the same lesson from the Green/Beyer incidents, applied
historically instead of to "today".

Run against the local mirror (scripts/sync_local_mirror.py) - read-only, zero Turso cost.
"""
import datetime
import sqlite3
from collections import defaultdict

from scripts.copytrading_backtest import _plus_365, _spy_price_on_or_after, _t_stat_and_p
from src.analysis.price_lookup import load_price_histories

DB_PATH = "data/congress_trades.db"
MIN_TRAILING_TRADES = 5
TOP_N = 10
FORWARD_YEARS = 1
STEP_MONTHS = 6
ACTIVE_LOOKBACK_YEARS = 2
# How many trades' worth of "pull toward the population mean" a legislator's own trailing
# alpha estimate gets, before ranking - chosen so a 20-trade record (the Wasserman Schultz
# case) gets pulled roughly 60% of the way to the mean, while a 300+-trade record barely
# moves. Not tuned/backtested itself - a documented, revisitable choice, same spirit as
# MIN_PLAUSIBLE_PRICE elsewhere in this project.
SHRINKAGE_K = 30


def _plus_years(date_str, years):
    d = datetime.date.fromisoformat(date_str)
    try:
        return d.replace(year=d.year + years).isoformat()
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years).isoformat()


def load_dated_trade_alphas(conn):
    """Same notification-lag-adjusted alpha as copytrading_backtest.load_trade_alphas, but
    carrying notification_date too - needed to place each trade in walk-forward time,
    which the shared loader doesn't track."""
    rows = conn.execute(
        """
        SELECT f.legislator_id, l.first_name, l.last_name,
               t.ticker, t.transaction_date, t.notification_date,
               (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid
        FROM trades t
        JOIN filings f ON f.id = t.filing_id
        JOIN legislators l ON l.id = f.legislator_id
        WHERE t.transaction_type = 'purchase' AND t.asset_type IN ('ST', 'Stock', 'OP')
        """
    ).fetchall()

    histories = load_price_histories(conn, [r["ticker"] for r in rows])
    spy_cache = {}
    results = []
    for r in rows:
        history = histories.get(r["ticker"])
        if history is None:
            continue
        transaction_date = datetime.date.fromisoformat(r["transaction_date"])
        notification_date = datetime.date.fromisoformat(r["notification_date"])
        # 365 days after TRANSACTION date, matching the old price_365d column's exact
        # definition - not notification_date, even though the return below uses
        # price_at_notification as its entry price (the same "not exactly 365 days from
        # entry" approximation copytrading_backtest.py's docstring already flags).
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
        results.append({
            "legislator_id": r["legislator_id"],
            "legislator": f"{r['first_name']} {r['last_name']}",
            "ticker": r["ticker"],
            "amount_mid": r["amount_mid"],
            "notification_date": r["notification_date"],
            # The date this trade's own 365-day outcome becomes knowable - a ranking made
            # "as of" any date before this has no legitimate way to have used this trade.
            "resolved_date": _plus_365(r["notification_date"]),
            "alpha_pct": (stock_ret - spy_ret) * 100,
        })
    return results


def _dollar_weighted_alpha(rows):
    total = sum(r["amount_mid"] for r in rows)
    return sum(r["amount_mid"] * r["alpha_pct"] for r in rows) / total


def _active_ids_as_of(all_rows, cutoff, lookback_years):
    lookback_start = _plus_years(cutoff, -lookback_years)
    return {
        r["legislator_id"] for r in all_rows
        if lookback_start <= r["notification_date"] <= cutoff
    }


def rank_as_of(all_rows, cutoff, active_lookback_years=ACTIVE_LOOKBACK_YEARS,
               min_trades=MIN_TRAILING_TRADES, shrinkage_k=SHRINKAGE_K, top_n=TOP_N):
    """Ranks legislators using only trades resolved by `cutoff`, shrunk toward the
    trailing population mean, restricted to those still active as of `cutoff`. Returns
    (top_ids, eligible_ids, name_by_id) - eligible_ids is the pool a random-N control
    should draw from, so it's evaluated on the same footing as the ranked pick."""
    trailing = [r for r in all_rows if r["resolved_date"] <= cutoff]
    if not trailing:
        return [], set(), {}

    by_leg = defaultdict(list)
    for r in trailing:
        by_leg[r["legislator_id"]].append(r)

    active_ids = _active_ids_as_of(all_rows, cutoff, active_lookback_years)
    population_mean = _dollar_weighted_alpha(trailing)
    name_by_id = {r["legislator_id"]: r["legislator"] for r in trailing}

    scored = []
    for leg_id, rows in by_leg.items():
        if leg_id not in active_ids or len(rows) < min_trades:
            continue
        raw_alpha = _dollar_weighted_alpha(rows)
        n = len(rows)
        shrunk = (n / (n + shrinkage_k)) * raw_alpha + (shrinkage_k / (n + shrinkage_k)) * population_mean
        scored.append((leg_id, shrunk, raw_alpha, n))

    scored.sort(key=lambda x: x[1], reverse=True)
    eligible_ids = {leg_id for leg_id, *_ in scored}
    top_ids = [leg_id for leg_id, *_ in scored[:top_n]]
    return top_ids, eligible_ids, name_by_id, scored[:top_n]


def forward_test(all_rows, cutoff, ids, forward_years=FORWARD_YEARS):
    """Real, never-touched-by-the-ranking forward performance: trades this group actually
    disclosed in (cutoff, cutoff + forward_years], using their own already-resolved
    outcome. A trade disclosed in this window whose outcome isn't resolved yet (still
    within FORWARD_YEARS + 1 of "today") is correctly absent - not a bug, just not knowable
    yet, same as any other unresolved trade."""
    end = _plus_years(cutoff, forward_years)
    trades = [
        r for r in all_rows
        if r["legislator_id"] in ids and cutoff < r["notification_date"] <= end
    ]
    if not trades:
        return None
    mean, t, p = _t_stat_and_p([r["alpha_pct"] for r in trades])
    return {"n": len(trades), "mean": mean, "p": p, "dw_mean": _dollar_weighted_alpha(trades)}


def _cutoffs(all_rows, forward_years=FORWARD_YEARS, step_months=STEP_MONTHS):
    """Every step_months-spaced date from 2 years after data starts (needs that much
    history to rank anyone) through forward_years + 1 year before the latest resolved
    trade (needs that much runway for the forward window's own trades to be priced)."""
    dates = sorted(r["resolved_date"] for r in all_rows)
    start = _plus_years(dates[0], 2)
    end = _plus_years(dates[-1], -(forward_years + 1))
    cutoffs = []
    d = datetime.date.fromisoformat(start)
    end_d = datetime.date.fromisoformat(end)
    while d <= end_d:
        cutoffs.append(d.isoformat())
        month = d.month - 1 + step_months
        d = d.replace(year=d.year + month // 12, month=month % 12 + 1)
    return cutoffs


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    all_rows = load_dated_trade_alphas(conn)
    conn.close()
    print(f"{len(all_rows)} priced, notification-lag-adjusted purchases loaded.\n")

    cutoffs = _cutoffs(all_rows)
    print(f"Walk-forward cutoffs ({len(cutoffs)}, every {STEP_MONTHS} months, "
          f"{FORWARD_YEARS}-year forward window each): {cutoffs[0]} to {cutoffs[-1]}\n")

    import random
    rng = random.Random(0)
    top_folds, random_folds = [], []
    for cutoff in cutoffs:
        top_ids, eligible_ids, name_by_id, top_scored = rank_as_of(all_rows, cutoff)
        if len(top_ids) < TOP_N or len(eligible_ids) < TOP_N:
            print(f"{cutoff}: skipped - not enough eligible legislators yet "
                  f"({len(eligible_ids)} < {TOP_N})")
            continue
        random_ids = rng.sample(sorted(eligible_ids), TOP_N)

        top_result = forward_test(all_rows, cutoff, set(top_ids))
        random_result = forward_test(all_rows, cutoff, set(random_ids))
        names = ", ".join(name_by_id[lid] for lid in top_ids[:3])
        if top_result:
            top_folds.append(top_result)
            print(f"{cutoff}: top-10 (e.g. {names}, ...) forward alpha "
                  f"{top_result['mean']:+.1f}% (n={top_result['n']})"
                  + (f"  |  random-10 {random_result['mean']:+.1f}% (n={random_result['n']})"
                     if random_result else "  |  random-10: no forward trades"))
            if random_result:
                random_folds.append(random_result)
        else:
            print(f"{cutoff}: top-10 picked, but no forward trades in this window yet - skipped")

    def _summarize(label, folds):
        if not folds:
            print(f"\n{label}: no folds with forward data.")
            return
        means = [f["mean"] for f in folds]
        pooled_alphas = []
        total_n = sum(f["n"] for f in folds)
        avg_mean, t, p = _t_stat_and_p(means)
        n_positive = sum(1 for m in means if m > 0)
        print(f"\n{label} ({len(folds)} folds, {total_n} forward trades total)")
        print(f"  forward alpha positive in {n_positive}/{len(folds)} folds "
              f"({n_positive / len(folds) * 100:.0f}%)")
        print(f"  mean of per-fold alpha: {avg_mean:+.2f}%  "
              f"(across-fold t={t:.2f}, p={p:.4f})")
        print(f"  range across folds: {min(means):+.1f}% to {max(means):+.1f}%")

    _summarize("Top-10-by-shrunk-trailing-alpha, real forward performance", top_folds)
    _summarize("Random-10 control (same eligible pool each fold)", random_folds)

    if cutoffs:
        print(f"\nMost recent fully-resolvable cutoff ({cutoffs[-1]}) top {TOP_N}, "
              f"for reference (not yet forward-tested - too recent):")
        top_ids, _, name_by_id, top_scored = rank_as_of(all_rows, cutoffs[-1])
        for leg_id, shrunk, raw, n in top_scored:
            print(f"  {name_by_id[leg_id]:<26} shrunk={shrunk:+7.1f}%  raw={raw:+7.1f}%  n={n}")


if __name__ == "__main__":
    main()
