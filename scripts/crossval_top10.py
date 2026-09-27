"""Cross-validates layer 10's out-of-sample top-10 result, which was built on exactly ONE
train/test split (each legislator's trades divided at their own median date). One split
can't distinguish "these ~10 people have a real edge" from "we got a lucky split" - the
same top-10 list, or a different one, might look totally different under a different cut.

Repeats the same train/rank/test procedure many times with independent RANDOM 50/50 splits
per legislator (not date-based - a random split isolates "does train performance predict
test performance" from "did markets behave differently before/after some calendar date",
which a single fixed date split can't separate) and reports, across all iterations:
  - how often the resulting top-10-by-train-alpha group's test-set alpha is positive /
    significantly positive
  - the distribution of that test-set alpha across iterations
  - a RANDOM-10 control drawn from the same eligible pool each iteration, run through the
    identical test procedure - isolates whether the train-based ranking itself adds value,
    versus "any 10 sufficiently-active legislators tend to look fine here"
  - which legislators actually get selected most often - a stable core across independent
    random splits is real evidence of a persistent group; a different 10 names every time
    is evidence the layer-10 result was a lucky draw

Run against the local mirror (scripts/sync_local_mirror.py) - read-only, zero Turso cost.
"""
import random
import sqlite3
from collections import Counter, defaultdict

from scripts.copytrading_backtest import _t_stat_and_p, get_active_legislator_ids, load_trade_alphas

DB_PATH = "data/congress_trades.db"
MIN_TRAIN_TRADES = 5
TOP_N = 10
N_ITERATIONS = 200
# A recommendation must come from someone still actually trading - see
# get_active_legislator_ids's docstring for why (Mark Green's own #1 pick had zero 2026
# trades before this filter existed).
ACTIVE_SINCE = "2026-01-01"


def _dollar_weighted_alpha(rows):
    total = sum(r["amount_mid"] for r in rows)
    return sum(r["amount_mid"] * r["alpha_pct"] for r in rows) / total


def _one_split(by_leg, rng):
    """Random 50/50 split of each eligible legislator's own trades. Returns
    (eligible_leg_ids, train_by_leg, test_alphas_by_leg)."""
    train_by_leg, test_by_leg = {}, {}
    for leg_id, rows in by_leg.items():
        shuffled = rows[:]
        rng.shuffle(shuffled)
        mid = len(shuffled) // 2
        train, test = shuffled[:mid], shuffled[mid:]
        if len(train) >= MIN_TRAIN_TRADES and test:
            train_by_leg[leg_id] = train
            test_by_leg[leg_id] = test
    return train_by_leg, test_by_leg


def _test_alpha_for_group(leg_ids, test_by_leg):
    trades = [r for leg_id in leg_ids for r in test_by_leg[leg_id]]
    if not trades:
        return None
    alphas = [r["alpha_pct"] for r in trades]
    mean, t, p = _t_stat_and_p(alphas)
    dw_mean = _dollar_weighted_alpha(trades)
    return {"n": len(trades), "mean": mean, "p": p, "dw_mean": dw_mean}


def run(lag_results, active_legislator_ids, n_iterations=N_ITERATIONS, seed=0):
    by_leg = defaultdict(list)
    for r in lag_results:
        by_leg[r["legislator_id"]].append(r)

    rng = random.Random(seed)
    top10_stats, random10_stats = [], []
    selection_counts = Counter()
    name_by_id = {r["legislator_id"]: r["legislator"] for r in lag_results}

    for i in range(n_iterations):
        train_by_leg, test_by_leg = _one_split(by_leg, rng)
        # Only a still-actively-trading legislator can be RANKED/RECOMMENDED - their
        # trades still count as test data for whoever else gets picked, but someone with
        # no recent activity can't be a live "follow this person" answer regardless of how
        # good their historical alpha looks.
        eligible = [lid for lid in train_by_leg if lid in active_legislator_ids]
        if len(eligible) < TOP_N:
            continue

        ranked = sorted(
            eligible, key=lambda lid: _dollar_weighted_alpha(train_by_leg[lid]), reverse=True
        )
        top10_ids = ranked[:TOP_N]
        random10_ids = rng.sample(eligible, TOP_N)

        top_result = _test_alpha_for_group(top10_ids, test_by_leg)
        rand_result = _test_alpha_for_group(random10_ids, test_by_leg)
        if top_result:
            top10_stats.append(top_result)
            selection_counts.update(top10_ids)
        if rand_result:
            random10_stats.append(rand_result)

    return top10_stats, random10_stats, selection_counts, name_by_id


def summarize_iterations(label, stats):
    means = [s["mean"] for s in stats]
    dw_means = [s["dw_mean"] for s in stats]
    n_sig_positive = sum(1 for s in stats if s["mean"] > 0 and s["p"] < 0.05)
    n_positive = sum(1 for s in stats if s["mean"] > 0)
    avg_mean, t, p = _t_stat_and_p(means)
    print(f"\n{label} ({len(stats)} iterations)")
    print(f"  test-set alpha positive in {n_positive}/{len(stats)} iterations "
          f"({n_positive / len(stats) * 100:.0f}%)")
    print(f"  test-set alpha significantly positive (p<0.05) in {n_sig_positive}/{len(stats)} "
          f"({n_sig_positive / len(stats) * 100:.0f}%)")
    print(f"  mean of per-iteration equal-weighted alpha: {avg_mean:+.2f}%  "
          f"(across-iteration t={t:.2f}, p={p:.4f})")
    print(f"  mean of per-iteration dollar-weighted alpha: {sum(dw_means) / len(dw_means):+.2f}%")
    print(f"  range across iterations: {min(means):+.1f}% to {max(means):+.1f}%")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _, lag_results = load_trade_alphas(conn)
    active_ids = get_active_legislator_ids(conn, since=ACTIVE_SINCE)
    conn.close()
    print(f"{len(active_ids)} legislator(s) have traded since {ACTIVE_SINCE}.")

    top10_stats, random10_stats, selection_counts, name_by_id = run(lag_results, active_ids)

    summarize_iterations(f"Top-{TOP_N}-by-train-alpha, tested on held-out random half", top10_stats)
    summarize_iterations(f"Random-{TOP_N} control (same eligible pool)", random10_stats)

    print(f"\nMost frequently selected into the top {TOP_N} across "
          f"{len(top10_stats)} random splits:")
    for leg_id, count in selection_counts.most_common(15):
        print(f"  {name_by_id[leg_id]:<24} selected in {count}/{len(top10_stats)} splits "
              f"({count / len(top10_stats) * 100:.0f}%)")


if __name__ == "__main__":
    main()
