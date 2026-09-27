"""Tests a specific hypothesis about WHY some legislators are worth copying: not raw
historical alpha (layer 10/crossval_top10's ranking signal), but "conviction" - a few large,
concentrated positions vs. many small, scattered ones. Confirmed directly in the data as a
real, large difference: Pelosi's 51 stock purchases sit in 23 tickers (avg ~$1.5M/trade, top
holding 14% of her book); Gottheimer's 1,368 purchases scatter across 425 tickers (avg
~$8,800/trade, top holding 7%) - two very different postures, not a subtle distinction.

Two conviction metrics per legislator, computed the same way as _dollar_weighted_alpha
(same PRICEABLE_ASSET_TYPES, same notification-lag-adjusted trade set as copytrading_backtest
so this ranks on exactly the population layer 10 already validated):
  - avg_trade_dollars: mean disclosed dollar size per priced trade - the direct "how big are
    the bets" measure.
  - hhi: Herfindahl-Hirschman index of dollar concentration across distinct tickers
    (sum of each ticker's dollar-share squared) - 1.0 if everything went into one ticker,
    ~0 if spread evenly across many. Captures "few big bets" separately from "big bets" -
    someone could have a large avg trade size but still spread it across dozens of tickers.

Cross-validated exactly like crossval_top10.py (independent random 50/50 splits per
legislator, ranked on TRAIN half, tested on TEST half, MIN_TRAIN_TRADES/TOP_N/ACTIVE_SINCE
all identical) so results are directly comparable to that script's top-10-by-alpha and
random-10 baselines - the only thing that changes is the ranking key.

CAVEAT confirmed live, not theoretical: computing HHI/avg_trade_dollars over
PRICEABLE_ASSET_TYPES (which treats an option's disclosed dollar bracket as equity notional,
same caveat copytrading_backtest.py already flags) badly distorts the "conviction" picture
for an options-heavy trader. Gottheimer looks diversified by TRADE COUNT (1,325 trades, 360
tickers) but his dollar-weighted book is 94% MSFT once his large options brackets are
counted at face value - the opposite of the "scattered, low-conviction" story trade count
alone suggests. Reported both ways below: options-inclusive (matches copytrading_backtest's
own population test) and stock-only (closer to what an actual copytrader following disclosed
STOCK position sizes - not options notional - would be ranking on).

Run against the local mirror (scripts/sync_local_mirror.py) - read-only, zero Turso cost.
"""
import random
import sqlite3
from collections import Counter, defaultdict

from scripts.copytrading_backtest import _t_stat_and_p, get_active_legislator_ids, load_trade_alphas
from scripts.crossval_top10 import _dollar_weighted_alpha, _one_split, _test_alpha_for_group, summarize_iterations

DB_PATH = "data/congress_trades.db"
MIN_TRAIN_TRADES = 5
TOP_N = 10
N_ITERATIONS = 200
ACTIVE_SINCE = "2026-01-01"


def _avg_trade_dollars(rows):
    return sum(r["amount_mid"] for r in rows) / len(rows)


def _hhi(rows):
    by_ticker = defaultdict(float)
    for r in rows:
        by_ticker[r["ticker"]] += r["amount_mid"]
    total = sum(by_ticker.values())
    if total <= 0:
        return 0.0
    return sum((amt / total) ** 2 for amt in by_ticker.values())


def _stock_only(rows):
    return [r for r in rows if r["asset_type"] in ("ST", "Stock")]


def describe_conviction(lag_results):
    """Per-legislator conviction profile over their FULL trade history (not train/test
    split) - descriptive only, to ground the hypothesis in real numbers before the
    predictive test below."""
    by_leg = defaultdict(list)
    for r in lag_results:
        by_leg[r["legislator_id"]].append(r)
    profiles = []
    for leg_id, rows in by_leg.items():
        if len(rows) < MIN_TRAIN_TRADES:
            continue
        profiles.append({
            "legislator_id": leg_id,
            "legislator": rows[0]["legislator"],
            "n_trades": len(rows),
            "n_tickers": len({r["ticker"] for r in rows}),
            "avg_trade_dollars": _avg_trade_dollars(rows),
            "hhi": _hhi(rows),
            "dw_alpha": _dollar_weighted_alpha(rows),
        })
    return profiles


def print_named_comparison(profiles, names):
    by_name = {p["legislator"]: p for p in profiles}
    print("\nNamed comparison (full history, not train/test split):")
    print(f"{'legislator':<22}{'n_trades':>9}{'n_tickers':>10}{'avg_$/trade':>14}{'hhi':>8}{'dw_alpha':>10}")
    for name in names:
        p = by_name.get(name)
        if p is None:
            print(f"{name:<22} not found / below {MIN_TRAIN_TRADES}-trade minimum")
            continue
        print(f"{p['legislator']:<22}{p['n_trades']:>9}{p['n_tickers']:>10}"
              f"{p['avg_trade_dollars']:>14,.0f}{p['hhi']:>8.3f}{p['dw_alpha']:>+9.1f}%")


def _rank_key_runs(lag_results, active_legislator_ids, rank_key_fn, n_iterations=N_ITERATIONS, seed=0):
    """Same iteration loop as crossval_top10.run, generalized to any train-half ranking
    function (dollar-weighted alpha there, avg trade size / HHI here) so the exact same
    splits and test procedure can be reused for a fair comparison."""
    by_leg = defaultdict(list)
    for r in lag_results:
        by_leg[r["legislator_id"]].append(r)

    rng = random.Random(seed)
    stats = []
    selection_counts = Counter()
    name_by_id = {r["legislator_id"]: r["legislator"] for r in lag_results}

    for _ in range(n_iterations):
        train_by_leg, test_by_leg = _one_split(by_leg, rng)
        eligible = [lid for lid in train_by_leg if lid in active_legislator_ids]
        if len(eligible) < TOP_N:
            continue
        ranked = sorted(eligible, key=lambda lid: rank_key_fn(train_by_leg[lid]), reverse=True)
        top_ids = ranked[:TOP_N]
        result = _test_alpha_for_group(top_ids, test_by_leg)
        if result:
            stats.append(result)
            selection_counts.update(top_ids)

    return stats, selection_counts, name_by_id


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _, lag_results = load_trade_alphas(conn)
    active_ids = get_active_legislator_ids(conn, since=ACTIVE_SINCE)
    conn.close()
    print(f"{len(active_ids)} legislator(s) have traded since {ACTIVE_SINCE}.")

    stock_only_results = _stock_only(lag_results)

    for pool_label, pool in [("options-inclusive", lag_results), ("stock-only", stock_only_results)]:
        print(f"\n{'=' * 60}\n{pool_label.upper()}\n{'=' * 60}")
        profiles = describe_conviction(pool)
        print_named_comparison(profiles, ["Nancy Pelosi", "Josh Gottheimer"])

        # Population-level check: does conviction correlate with a legislator's OWN
        # historical alpha at all, before even getting to the predictive (train/test)
        # question?
        from scripts.sizing_timing_analysis import pearson_r
        r_avgsize = pearson_r([p["avg_trade_dollars"] for p in profiles], [p["dw_alpha"] for p in profiles])
        r_hhi = pearson_r([p["hhi"] for p in profiles], [p["dw_alpha"] for p in profiles])
        print(f"\nAcross {len(profiles)} legislators with >= {MIN_TRAIN_TRADES} trades:")
        print(f"  Pearson r(avg_trade_dollars, dw_alpha) = {r_avgsize:.3f}" if r_avgsize is not None else "  n/a")
        print(f"  Pearson r(hhi, dw_alpha) = {r_hhi:.3f}" if r_hhi is not None else "  n/a")

        for label, key_fn in [
            ("Top-10-by-TRAIN avg trade size ($)", _avg_trade_dollars),
            ("Top-10-by-TRAIN concentration (HHI)", _hhi),
        ]:
            stats, selection_counts, name_by_id = _rank_key_runs(pool, active_ids, key_fn)
            summarize_iterations(f"{label} [{pool_label}], tested on held-out random half", stats)
            print(f"  Most frequently selected:")
            for leg_id, count in selection_counts.most_common(10):
                print(f"    {name_by_id[leg_id]:<24} selected in {count}/{len(stats)} splits "
                      f"({count / len(stats) * 100:.0f}%)")


if __name__ == "__main__":
    main()
