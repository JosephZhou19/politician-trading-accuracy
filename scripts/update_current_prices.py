"""Daily trickle job: refreshes ticker_prices.current_price via Finnhub for every ticker
that has at least one real historical price already backfilled. A ticker with zero price
data anywhere is already known-dead from the one-time backfill - see
models.get_tickers_due_for_price_check for why those are excluded outright.

Handles a ticker going through M&A/delisting while under active tracking: Finnhub's
/quote returns c == 0 (not an error) for a dead symbol, indistinguishable in shape from a
fake ticker. This never overwrites current_price with a zero - see
models.record_zero_response for the streak-tracking and eventual 'delisted' downgrade.

Resumable by design rather than racing a fixed timeout: stops once TIME_BUDGET_MINUTES is
up (a real Turso or Finnhub slowdown - already observed once - could otherwise blow past
any timeout mid-write) and picks up right after the last ticker it finished next run,
bookmarked in trickle_cursor by ticker value so it stays correct even as the due-ticker set
shifts between runs. A run that finishes the whole queue with time to spare just wraps
around to the start next time - no special-casing needed either way.

Usage:
    python -m scripts.update_current_prices --db data/congress_trades.db
"""
import argparse
import datetime
import sqlite3
import time

from dotenv import load_dotenv

from src.db import models
from src.market.current_price import fetch_current_prices

load_dotenv()

# Trades ~63 round-trips for ~3,137 individual ones - both faster in the normal case and,
# more importantly, far less exposed if Turso has another slow patch mid-run (already seen
# once this session): at most COMMIT_BATCH_SIZE tickers' worth of work is ever at risk.
COMMIT_BATCH_SIZE = 50

# Comfortably under the GitHub Actions job timeout, so the script always has room to stop
# itself gracefully (flush the pending batch, save the cursor) rather than getting killed
# mid-write.
DEFAULT_TIME_BUDGET_MINUTES = 75


def _rotate_to_resume_point(tickers, cursor):
    """Starts the list right after the bookmarked ticker, wrapping to the beginning - so a
    run that stopped partway last time picks up where it left off instead of always
    re-checking the same alphabetically-early tickers first."""
    if cursor is None:
        return tickers
    start_idx = next((i for i, t in enumerate(tickers) if t > cursor), 0)
    return tickers[start_idx:] + tickers[:start_idx]


def update_current_prices(conn, ticker_limit=None, time_budget_minutes=DEFAULT_TIME_BUDGET_MINUTES):
    cursor = models.get_trickle_cursor(conn)
    tickers = _rotate_to_resume_point(models.get_tickers_due_for_price_check(conn), cursor)
    if ticker_limit is not None:
        tickers = tickers[:ticker_limit]

    deadline = time.monotonic() + time_budget_minutes * 60 if time_budget_minutes else None
    summary = {"tickers_checked": 0, "prices_updated": 0, "zero_responses": 0, "newly_delisted": 0}
    print(f"{len(tickers)} ticker(s) due for a price check"
          + (f" (resuming after {cursor!r})." if cursor else "."))

    last_ticker = None
    since_commit = 0
    for ticker, price in fetch_current_prices(tickers):
        checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        summary["tickers_checked"] += 1

        if price is not None:
            models.record_real_price(conn, ticker, price, checked_at, commit=False)
            summary["prices_updated"] += 1
        else:
            existing = models.get_ticker_price(conn, ticker)
            old_streak = existing.zero_streak if existing else 0
            was_active = existing is None or existing.price_status == "active"
            models.record_zero_response(conn, ticker, checked_at, commit=False)
            summary["zero_responses"] += 1
            if was_active and old_streak + 1 >= models.ZERO_STREAK_DELIST_THRESHOLD:
                print(f"{ticker}: zero-streak hit {models.ZERO_STREAK_DELIST_THRESHOLD} - "
                      f"flagging delisted, dropping to a once-every-{models.DELISTED_RECHECK_DAYS}-day check.")
                summary["newly_delisted"] += 1

        last_ticker = ticker
        since_commit += 1
        if since_commit >= COMMIT_BATCH_SIZE:
            conn.commit()
            since_commit = 0

        if deadline is not None and time.monotonic() >= deadline:
            print(f"Time budget reached after {summary['tickers_checked']} ticker(s) - "
                  f"stopping early, will resume after {ticker!r} next run.")
            break

    if since_commit > 0:
        conn.commit()
    if last_ticker is not None:
        models.set_trickle_cursor(conn, last_ticker)

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    parser.add_argument("--ticker-limit", type=int, default=None, help="Check at most N tickers (for testing)")
    parser.add_argument(
        "--time-budget-minutes", type=float, default=DEFAULT_TIME_BUDGET_MINUTES,
        help="Stop and save the resume cursor after this long (0 to disable and run to completion)",
    )
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = update_current_prices(
        conn, ticker_limit=args.ticker_limit,
        time_budget_minutes=args.time_budget_minutes or None,
    )
    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
