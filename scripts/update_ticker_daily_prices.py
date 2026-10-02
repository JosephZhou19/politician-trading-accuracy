"""Cheap recurring catch-up for ticker_daily_prices - the counterpart to
backfill_ticker_daily_prices.py's heavy one-time full-history load. Meant to run often
(e.g. daily) without meaningfully costing API calls, yfinance rate-limit exposure, or Turso
writes.

Also fully replaces the old Finnhub-based daily trickle (scripts/update_current_prices.py,
now removed) - there's no reason to run two separate "how has this ticker's price changed
lately" jobs against two different APIs once this one already answers that question for
every ticker, every day. ticker_status (delisting-detection state - see
record_ticker_seen/record_ticker_missed) is kept in sync here from the same fetch, so a
dead ticker still gets skipped efficiently even though "current price" is no longer tracked
as its own value anywhere (it's just the latest row in ticker_daily_prices now).

Two ticker groups, handled differently:
- Tickers with NO stored history yet (a newly-disclosed ticker the one-time backfill hasn't
  reached, or hasn't started on at all) get a full single-ticker fetch_ticker_history call,
  same as the one-time backfill - rare per run, so sequential FETCHES here are fine; the
  resulting writes are still batched (see below), not one round-trip per ticker.
- Tickers that already have stored history are batched together (BATCH_SIZE per yfinance
  call via fetch_ticker_histories_batch) and only asked for a small recent OVERLAP_DAYS
  window, not their whole history - this is what keeps a run of ~3,500 tickers down to
  roughly 3,500/BATCH_SIZE yfinance requests instead of 3,500 individual ones.

OVERLAP_DAYS is deliberately small (see its own comment) - every day in the fetched window
gets re-upserted on every run, not just genuinely new days, so this number multiplies
directly into Turso write volume across ~2,500+ due tickers, every run. Confirmed live this
session: at the old value (10 calendar days, ~7 trading days), that was ~18,000 redundant
row-writes/run for corrections we have no actual evidence occur at that range - see
OVERLAP_DAYS's own comment for what we DO have evidence of.

Split-basis guard: before upserting a "due" ticker's fetched window, its fetched price at
its own last-already-stored date is compared against what's on record for that date (see
REBASE_RATIO_THRESHOLD below). A mismatch means a stock split has retroactively rewritten
yfinance's history since the last fetch - in that case ALL of the ticker's stored rows are
now on the wrong basis, not just the missing recent days, so the whole series is re-fetched
and replaced rather than just topped up. This is the real, confirmed correction mechanism -
OVERLAP_DAYS is NOT protecting against this (a real rebase affects years of history, not a
few days; this check catches it precisely via the ticker's own last-stored date, independent
of window size).

No resumable cursor by design (unlike the two trickle-style jobs) - a run that hits its time
budget partway just leaves some tickers stale until the next scheduled run reprocesses the
same small due-set from the top, which is cheap enough to be "insurance" rather than waste.

Per-BATCH, not per-ticker, reads/writes (fixed 2026-10-02): confirmed live that the previous
per-ticker version (one rebase-check read, one status upsert, and one execute() per price row
- all separate Turso round-trips, repeated for every single ticker) limited a full 30-minute
run to clearing only ~94 of ~2,770 due tickers. Applies to all three paths now: the due-tickers
loop (rebase-check read batched via get_ticker_daily_prices_batch, price writes via
bulk_insert_ticker_daily_prices, status writes via record_tickers_seen_batch/
record_tickers_missed_batch), the new-tickers loop (same batched writes, flushed every
BATCH_SIZE tickers), and a rebase's full-history replace (replace_ticker_daily_prices now
uses the same bulk insert internally instead of one execute() per row).
fast_forward_confirmed_dead_tickers runs once up front so a ticker already independently
confirmed dead doesn't keep cycling through the new-tickers pass every run.

Usage:
    python -m scripts.update_ticker_daily_prices --db data/congress_trades.db
"""
import argparse
import datetime
import sqlite3
import time

from dotenv import load_dotenv

from src.db import models
from src.market.prices import fetch_ticker_history, fetch_ticker_histories_batch

load_dotenv()

# How many tickers share one yfinance batch call - large enough to meaningfully cut the
# number of requests (3,500 tickers / 50 = ~70 calls instead of 3,500), small enough that
# one bad/slow batch doesn't put a huge fraction of the run at risk.
BATCH_SIZE = 50
# Was 10, copied from backfill_spy_benchmark.py's OVERLAP_DAYS without re-deriving whether
# "cheap insurance" still holds once it's multiplied across ~2,500+ tickers instead of that
# job's 1 (SPY was, and still is, genuinely cheap at any window size). It isn't cheap here -
# confirmed live, 10 days meant ~18,000 redundant re-written rows/run. Checked what
# correction we're actually protecting against: dividend-driven drift on old dates was
# tested directly this session and found negligible (fetching the same historical date with
# different end-dates years apart gave a ratio of 0.9999999 - floating-point noise, not
# real drift); the one real correction mechanism (a stock split retroactively rewriting
# history) is already caught precisely by the rebase check below, independent of this
# window. What's left to justify SOME overlap: today's/yesterday's close occasionally not
# being fully settled yet when this job runs - which only needs 1-2 days, not 10.
OVERLAP_DAYS = 2
# A split moving the adjustment basis by more than this since the last fetch triggers a
# full re-fetch/replace.
REBASE_RATIO_THRESHOLD = 1.5
# Courtesy delay between BATCH yfinance calls (not per-ticker, since each call already
# covers BATCH_SIZE tickers) - gentler on Yahoo's undocumented rate limiting than the
# per-ticker delay the one-time backfill uses, while still pacing the run.
BATCH_DELAY_SECONDS = 1.0
DEFAULT_TIME_BUDGET_MINUTES = 30


def _needs_rebase(stored, fresh):
    """True if the fetched window's price at the ticker's own last-stored date disagrees
    with what's already on record there - see the module docstring's split-basis guard.
    Takes the already-looked-up stored price and the already-computed fresh price directly
    (not conn/ticker/history) - the caller batches the stored-price lookup across a whole
    group of tickers in one round-trip now, instead of this function doing one per ticker."""
    if stored is None or fresh is None:
        return False
    ratio = fresh / stored
    return not (1 / REBASE_RATIO_THRESHOLD <= ratio <= REBASE_RATIO_THRESHOLD)


def _record_outcome(conn, ticker, history, checked_at):
    """Keeps ticker_status in sync with whatever this run found for `ticker` - see the
    module docstring's Finnhub-replacement note. `history` is whatever TickerHistory was
    actually just persisted for this ticker (None if yfinance had nothing)."""
    if history is None or not history.daily_prices():
        models.record_ticker_missed(conn, ticker, checked_at, commit=False)
        return
    models.record_ticker_seen(conn, ticker, checked_at, commit=False)


def update_ticker_daily_prices(conn, time_budget_minutes=DEFAULT_TIME_BUDGET_MINUTES):
    today = datetime.date.today()
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Tickers already tracked 'active' in ticker_status but with zero stored rows otherwise
    # sit in the new-tickers pass every run until their own zero_streak naturally crosses the
    # delist threshold - fast-forward ones already independently confirmed dead so they stop
    # burning a fetch+write each run.
    fast_forwarded = models.fast_forward_confirmed_dead_tickers(conn, checked_at)
    if fast_forwarded:
        print(f"Fast-forwarded {fast_forwarded} already-confirmed-dead ticker(s) to 'delisted'.")

    # Every traded ticker, minus ones confirmed 'delisted' except their once-a-month
    # safety-net recheck.
    all_tickers = models.filter_active_or_recheckable(conn, models.get_all_traded_tickers(conn))
    latest_dates = models.get_latest_daily_price_dates(conn, all_tickers)

    new_tickers = [t for t in all_tickers if t not in latest_dates]
    due_tickers = [t for t in all_tickers if t in latest_dates]

    deadline = time.monotonic() + time_budget_minutes * 60 if time_budget_minutes else None
    summary = {
        "new_tickers_backfilled": 0, "due_tickers_refreshed": 0,
        "tickers_rebased": 0, "tickers_no_data": 0, "days_written": 0,
    }
    print(f"{len(new_tickers)} new ticker(s) need a first fetch, "
          f"{len(due_tickers)} already-tracked ticker(s) due for a catch-up.")

    earliest_by_ticker = models.get_earliest_trade_dates_by_ticker(conn)

    # Each new ticker still needs its own individual fetch (a different full-history start
    # date per ticker), but the writes are batched in groups of BATCH_SIZE instead of one
    # execute() + commit() per ticker - same round-trip-collapsing fix as the due-tickers loop.
    new_seen, new_missed, new_bulk_rows = [], [], []

    def _flush_new_tickers():
        if new_bulk_rows:
            models.bulk_insert_ticker_daily_prices(conn, new_bulk_rows, commit=False)
        if new_seen:
            models.record_tickers_seen_batch(conn, new_seen, checked_at, commit=False)
        if new_missed:
            models.record_tickers_missed_batch(conn, new_missed, checked_at, commit=False)
        conn.commit()
        new_seen.clear()
        new_missed.clear()
        new_bulk_rows.clear()

    for ticker in new_tickers:
        history = fetch_ticker_history(ticker, earliest_by_ticker.get(ticker, today), today)
        if history is None or not history.daily_prices():
            new_missed.append(ticker)
            summary["tickers_no_data"] += 1
        else:
            prices = [(date.isoformat(), price) for date, price in history.daily_prices()]
            new_bulk_rows.extend((ticker, d, p) for d, p in prices)
            new_seen.append(ticker)
            summary["new_tickers_backfilled"] += 1
            summary["days_written"] += len(prices)

        if len(new_seen) + len(new_missed) >= BATCH_SIZE:
            _flush_new_tickers()

        if deadline is not None and time.monotonic() >= deadline:
            _flush_new_tickers()
            print("Time budget reached during new-ticker pass - stopping early.")
            return summary

    _flush_new_tickers()

    for start in range(0, len(due_tickers), BATCH_SIZE):
        batch = due_tickers[start:start + BATCH_SIZE]
        batch_start = min(
            datetime.date.fromisoformat(latest_dates[t]) for t in batch
        ) - datetime.timedelta(days=OVERLAP_DAYS)
        histories = fetch_ticker_histories_batch(batch, batch_start, today)
        time.sleep(BATCH_DELAY_SECONDS)

        # One batched rebase-check read per group instead of one round-trip per ticker - see
        # the module docstring for why.
        priced = [t for t in batch if histories.get(t) is not None]
        stored_prices = models.get_ticker_daily_prices_batch(conn, [(t, latest_dates[t]) for t in priced])

        seen_tickers, missed_tickers, rebase_tickers = [], [], []
        bulk_price_rows = []

        for ticker in batch:
            history = histories.get(ticker)
            # Same condition _record_outcome uses: a non-None history whose window happened
            # to filter down to zero valid days (every day NaN/implausible, without the whole
            # ticker failing the batch fetch's own all-NaN check) is still a miss, not a
            # successful (zero-row) "refresh".
            if history is None or not history.daily_prices():
                missed_tickers.append(ticker)
                summary["tickers_no_data"] += 1
                continue

            fresh = history.price_on_or_after(datetime.date.fromisoformat(latest_dates[ticker]))
            if _needs_rebase(stored_prices.get(ticker), fresh):
                rebase_tickers.append(ticker)
                continue

            seen_tickers.append(ticker)
            # batch_start is the MIN watermark across the whole batch; writing a ticker's full
            # fetched window (instead of just its own overlap) let one stale batch-mate drag
            # everyone else into writing years of redundant rows - confirmed live, 19.7x
            # amplification (1.17M rows vs. ~95k). Filter to this ticker's own watermark.
            own_start = datetime.date.fromisoformat(latest_dates[ticker]) - datetime.timedelta(days=OVERLAP_DAYS)
            prices = [
                (date.isoformat(), price) for date, price in history.daily_prices() if date >= own_start
            ]
            bulk_price_rows.extend((ticker, d, p) for d, p in prices)
            summary["due_tickers_refreshed"] += 1
            summary["days_written"] += len(prices)

        # One bulk multi-row insert for the whole group's price updates, instead of one
        # execute() per row per ticker.
        if bulk_price_rows:
            models.bulk_insert_ticker_daily_prices(conn, bulk_price_rows)
        if seen_tickers:
            models.record_tickers_seen_batch(conn, seen_tickers, checked_at, commit=False)
        if missed_tickers:
            models.record_tickers_missed_batch(conn, missed_tickers, checked_at, commit=False)

        # Rebases are rare (a real stock split since the last fetch) and still need an
        # individual full refetch+replace - handled one at a time, but their status write
        # still joins the single commit below instead of committing alone.
        for ticker in rebase_tickers:
            full_history = fetch_ticker_history(ticker, earliest_by_ticker.get(ticker, today), today)
            _record_outcome(conn, ticker, full_history, checked_at)
            if full_history is not None:
                prices = [(date.isoformat(), price) for date, price in full_history.daily_prices()]
                models.replace_ticker_daily_prices(conn, ticker, prices, commit=False)
                summary["tickers_rebased"] += 1
                summary["days_written"] += len(prices)

        conn.commit()

        if deadline is not None and time.monotonic() >= deadline:
            print(f"Time budget reached after {start + len(batch)}/{len(due_tickers)} due "
                  f"ticker(s) - stopping early, remaining stay stale until next run.")
            break

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    parser.add_argument(
        "--time-budget-minutes", type=float, default=DEFAULT_TIME_BUDGET_MINUTES,
        help="Stop early after this long (0 to disable and run to completion)",
    )
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = update_ticker_daily_prices(conn, time_budget_minutes=args.time_budget_minutes or None)
    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
