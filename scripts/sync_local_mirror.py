"""Incremental sync of a local sqlite mirror from Turso, so exploring analytics locally
doesn't repeatedly pay a full-table rows-read for tables that barely changed.

Sync strategies per table, chosen by whether existing rows can change after insert:
- Append-only (legislators, trades, benchmark_prices): new rows only, `id`/`date` strictly
  greater than the local file's own current max. Correct, not just an optimization, for
  legislators/benchmark_prices because nothing ever updates an existing row after insert.
  For trades this is a deliberate simplification, not a strict guarantee - see the known gap
  below.
- filings: new rows the same way, PLUS a targeted re-fetch (by id, a cheap indexed lookup)
  of whatever's still locally "pending" (parse_status != 'parsed') - the one thing about a
  filing that changes after insert (parse_status flipping from pending/failed/needs_ocr to
  parsed, via either the automated parsers or the OCR human-review pass in
  scripts/review_ocr_drafts.py). Computed against the LOCAL file (free) using the same
  parse_status != 'parsed' definition the real pipeline uses.
- Small tables (ticker_status, ingestion_runs): always fully re-fetched. Each is small
  enough (<5k rows) that incremental logic isn't worth the complexity.
- ticker_daily_prices: its own per-ticker strategy (sync_ticker_daily_prices below) - see
  that function's docstring for why neither of the two strategies above applies to it (no
  single global ordering column across ~3,500 independent per-ticker timelines, AND a
  split-basis rebase can silently rewrite already-synced old rows, which the append-only
  strategy's own stated precondition explicitly rules out).

Not synced here at all: daily_price_backfill_cursor - an operational bookmark for a
production-only one-time job, with no analytical value locally.

Known gap, accepted rather than engineered around: a reconciliation event on an
already-synced trade or already-parsed filing (e.g. a very late-arriving amendment
correcting an old, already-settled trade's superseded_by_trade_id/reconciliation_note) won't
be caught until that row happens to reappear in a "pending" check for some other reason
(filings only - trades have no such check at all now that price columns are gone), or a full
re-export is run. Rare in practice (a handful of ambiguous/no-match cases per ingest run, per
PLAN.md) and this is an exploration mirror, not a system of record - not worth the
complexity of a proper updated_at column for every write path just to close this gap.

Usage:
    python -m scripts.sync_local_mirror --db data/congress_trades.db
"""
import argparse
import os

from dotenv import load_dotenv

from src.db import models

load_dotenv()

SMALL_TABLES = ["ticker_status", "ingestion_runs"]


def _connect_turso():
    """Refuses to proceed if Turso credentials aren't loaded, rather than letting
    models.connect() silently fall back to a local file - which here would mean syncing
    a file from itself. See PLAN.md's load_dotenv() search-path gotcha."""
    if not (os.environ.get("TURSO_DATABASE_URL") and os.environ.get("TURSO_AUTH_TOKEN")):
        raise RuntimeError(
            "TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set - refusing to sync, since "
            "models.connect() would otherwise open the same local file as both source "
            "and target."
        )
    return models.connect("unused-since-turso-env-is-set")


def _replace_rows(local, table, rows):
    if not rows:
        return 0
    columns = rows[0].keys()
    placeholders = ",".join("?" * len(columns))
    col_list = ",".join(columns)
    local.executemany(
        f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
        [tuple(r[c] for c in columns) for r in rows],
    )
    return len(rows)


def sync_append_only(turso, local, table, key_column):
    """Pulls only rows newer than the local file's own current max key - correct as long
    as the caller confirms this table never updates an existing row after insert."""
    local_max = local.execute(f"SELECT MAX({key_column}) FROM {table}").fetchone()[0]
    if local_max is None:
        rows = turso.execute(f"SELECT * FROM {table}").fetchall()
    else:
        rows = turso.execute(
            f"SELECT * FROM {table} WHERE {key_column} > ?", (local_max,)
        ).fetchall()
    return _replace_rows(local, table, rows)


_REFETCH_BATCH_SIZE = 500


def _refetch_by_id(turso, local, table, ids):
    """Batched to stay under Turso/SQLite's bound-parameter limit - a "pending" set can
    easily exceed it (e.g. every trade still awaiting a price-horizon backfill)."""
    total = 0
    for start in range(0, len(ids), _REFETCH_BATCH_SIZE):
        batch = ids[start:start + _REFETCH_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = turso.execute(
            f"SELECT * FROM {table} WHERE id IN ({placeholders})", batch
        ).fetchall()
        total += _replace_rows(local, table, rows)
    return total


def sync_filings(turso, local):
    new_count = sync_append_only(turso, local, "filings", "id")
    pending_ids = [
        row["id"] for row in local.execute(
            "SELECT id FROM filings WHERE parse_status != 'parsed'"
        ).fetchall()
    ]
    return new_count, _refetch_by_id(turso, local, "filings", pending_ids)


def sync_small_table(turso, local, table):
    """Small enough (<5k rows) that incremental logic isn't worth it - also correctly
    picks up any row deleted upstream, which an id-based sync never would."""
    rows = turso.execute(f"SELECT * FROM {table}").fetchall()
    local.execute(f"DELETE FROM {table}")
    return _replace_rows(local, table, rows)


# Two tickers agreeing within this ratio at the same date means no split has moved the
# adjustment basis since the last sync - same convention as
# update_ticker_daily_prices.py's _needs_rebase.
_REBASE_RATIO_THRESHOLD = 1.5
# Batching for the per-batch subquery joins below (_watermark_subquery). NOT bound by the
# param-count limit the original comment here assumed (SQLite/Turso's default is far higher
# than this) - confirmed live against production that this Turso instance's real ceiling is
# the *compound SELECT term* count (each ticker contributes one `UNION ALL SELECT` term):
# binary-searched directly, 50 terms succeeds, 51 fails with "too many terms in compound
# SELECT" - much stricter than the 250 originally assumed and apparently never actually
# exercised at that size before (every prior sync only ever pulled a small delta). Kept well
# under the confirmed ceiling, not right at it, in case it varies slightly by instance/plan.
_TICKER_JOIN_BATCH_SIZE = 40

_COMPOUND_SELECT_LIMIT_ERROR = "too many terms in compound SELECT"


def _run_ticker_batch(turso, run_batch, batch):
    """Runs a per-batch ticker join query (run_batch(batch) -> rows), bisecting and retrying
    on Turso's compound-SELECT term limit rather than failing the whole sync outright. Belt-
    and-suspenders, not the actual fix for the one real incident this guards against: a
    scheduled run failed with this exact error on 2026-10-06 because the COMMITTED
    _TICKER_JOIN_BATCH_SIZE was still 250 (a stale, never-pushed value) while a local,
    uncommitted fix to 40 had been sitting in the working tree - CI checks out git, not the
    working tree, so it ran the broken value. That's fully explained and fixed by committing
    40. This bisection is cheap insurance on top, in case the real ceiling is ever lower than
    expected for some other reason in the future - not a claim that it's needed today.
    Halving stops at a batch of 1, where a real failure has nothing left to blame on batch
    size and is left to propagate."""
    if len(batch) <= 1:
        return run_batch(batch)
    try:
        return run_batch(batch)
    except ValueError as e:
        if _COMPOUND_SELECT_LIMIT_ERROR not in str(e):
            raise
        mid = len(batch) // 2
        return (
            _run_ticker_batch(turso, run_batch, batch[:mid])
            + _run_ticker_batch(turso, run_batch, batch[mid:])
        )


def _local_ticker_watermarks(local):
    """Every already-mirrored ticker's own most recent date AND the price stored there, in
    one local (free) query - the per-ticker equivalent of sync_append_only's single global
    `local_max`, plus the reference point the rebase check below compares Turso against."""
    rows = local.execute(
        """SELECT tdp.ticker, tdp.date, tdp.price FROM ticker_daily_prices tdp
           JOIN (SELECT ticker, MAX(date) AS d FROM ticker_daily_prices GROUP BY ticker) mx
             ON mx.ticker = tdp.ticker AND mx.d = tdp.date"""
    ).fetchall()
    return {r["ticker"]: (r["date"], r["price"]) for r in rows}


def _watermark_subquery(batch):
    """A `SELECT ? AS ticker, ? AS wdate UNION ALL ...` stand-in for a named-column VALUES
    table - SQLite doesn't support the `(VALUES ...) AS w(a,b)` column-renaming syntax
    (confirmed: 3.45.1 raises a syntax error on it, unlike some other engines), so this is
    the portable way to give a batch of (ticker, date) pairs queryable column names."""
    return " UNION ALL ".join(["SELECT ? AS ticker, ? AS wdate"] * len(batch))


def _find_rebased_tickers(turso, watermarks):
    """Of the given already-mirrored tickers, which ones Turso now disagrees with at their
    own last-known-locally date - a split has retroactively rewritten that ticker's whole
    series since the last sync, so it needs a full re-pull (_refetch_full_ticker_history),
    not just the incremental tail (a plain `date > watermark` pull would never re-touch an
    already-synced date, since the DATE didn't change, only the PRICE stored there did).

    One Turso round-trip per _TICKER_JOIN_BATCH_SIZE tickers via a join against a small
    per-batch subquery, not one query per ticker - this is the whole reason a per-ticker
    point lookup stays cheap here."""
    tickers = list(watermarks)
    rebased = set()

    def run_batch(batch):
        params = [v for t in batch for v in (t, watermarks[t][0])]
        return turso.execute(
            f"""SELECT t.ticker, t.price FROM ticker_daily_prices t
                JOIN ({_watermark_subquery(batch)}) w
                  ON t.ticker = w.ticker AND t.date = w.wdate""",
            params,
        ).fetchall()

    for start in range(0, len(tickers), _TICKER_JOIN_BATCH_SIZE):
        batch = tickers[start:start + _TICKER_JOIN_BATCH_SIZE]
        rows = _run_ticker_batch(turso, run_batch, batch)
        turso_prices = {r["ticker"]: r["price"] for r in rows}
        for ticker in batch:
            turso_price = turso_prices.get(ticker)
            local_price = watermarks[ticker][1]
            if turso_price is None or local_price is None or local_price <= 0:
                continue
            ratio = turso_price / local_price
            if not (1 / _REBASE_RATIO_THRESHOLD <= ratio <= _REBASE_RATIO_THRESHOLD):
                rebased.add(ticker)
    return rebased


def _pull_incremental_ticker_prices(turso, local, tickers, watermarks):
    """For tickers confirmed NOT rebased: pulls only rows past each ticker's own last-known
    date, via the same per-batch subquery join as _find_rebased_tickers - Turso only returns
    rows that are actually new, so the read cost is proportional to what changed, not to how
    many tickers were checked or how much history each one has."""
    total = 0

    def run_batch(batch):
        params = [v for t in batch for v in (t, watermarks[t][0])]
        return turso.execute(
            f"""SELECT t.ticker, t.date, t.price FROM ticker_daily_prices t
                JOIN ({_watermark_subquery(batch)}) w
                  ON t.ticker = w.ticker AND t.date > w.wdate""",
            params,
        ).fetchall()

    for start in range(0, len(tickers), _TICKER_JOIN_BATCH_SIZE):
        batch = tickers[start:start + _TICKER_JOIN_BATCH_SIZE]
        rows = _run_ticker_batch(turso, run_batch, batch)
        total += _replace_rows(local, "ticker_daily_prices", rows)
    return total


def _refetch_full_ticker_history(turso, local, tickers):
    """Full per-ticker history pull, batched like _refetch_by_id - used for a brand-new
    ticker (nothing local to compare against yet) or a rebased one (the whole series needs
    replacing, not topping up). INSERT OR REPLACE (via _replace_rows) is sufficient with no
    local DELETE first, since a rebase's fresh fetch covers the same-or-wider date range as
    what was already stored, not a narrower one."""
    total = 0
    for start in range(0, len(tickers), _REFETCH_BATCH_SIZE):
        batch = tickers[start:start + _REFETCH_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = turso.execute(
            f"SELECT * FROM ticker_daily_prices WHERE ticker IN ({placeholders})", batch
        ).fetchall()
        total += _replace_rows(local, "ticker_daily_prices", rows)
    return total


def sync_ticker_daily_prices(turso, local):
    """Per-ticker incremental sync for ticker_daily_prices - see the module docstring for
    why neither sync_append_only nor sync_small_table applies to this table. Three passes,
    cheapest first:
      1. Rebase check (_find_rebased_tickers) - one row read per already-mirrored ticker,
         batched.
      2. Incremental pull (_pull_incremental_ticker_prices) for whichever of those aren't
         rebased - costs only however many new rows each ticker actually has.
      3. Full pull (_refetch_full_ticker_history) for brand-new tickers and rebased ones.
    A first-ever sync (empty local mirror) has no watermarks at all, so every ticker falls
    into step 3 - the one-time full pull this table's local mirror never had before, same
    spirit as sync_append_only's own first-run behavior for benchmark_prices.

    Known gap, accepted rather than engineered around (confirmed live, not theoretical): a
    small in-tolerance correction to a ticker's own watermark date - e.g. Turso's own
    OVERLAP_DAYS re-fetch settling on a slightly different value for a day the local mirror
    already has - won't propagate. It's too small to trip the rebase check, and the
    incremental pull only looks at dates strictly PAST the watermark, never the watermark
    date itself. Closing this would mean unconditionally re-pulling one row per
    already-mirrored ticker on every single sync forever (real, recurring cost at ~3,500
    tickers) just to guard against an occasional single-day, sub-1.5x value drift on data
    that's only ever used for backtesting - not worth it. A value that drifts far enough to
    matter is exactly what the rebase check exists to catch."""
    watermarks = _local_ticker_watermarks(local)
    all_turso_tickers = [
        r["ticker"] for r in turso.execute("SELECT DISTINCT ticker FROM ticker_daily_prices").fetchall()
    ]
    new_tickers = [t for t in all_turso_tickers if t not in watermarks]
    synced_tickers = [t for t in all_turso_tickers if t in watermarks]

    rebased_tickers = _find_rebased_tickers(turso, {t: watermarks[t] for t in synced_tickers})
    stable_tickers = [t for t in synced_tickers if t not in rebased_tickers]

    incremental_rows = _pull_incremental_ticker_prices(turso, local, stable_tickers, watermarks)
    full_pull_rows = _refetch_full_ticker_history(turso, local, new_tickers + list(rebased_tickers))

    return {
        "new_tickers": len(new_tickers), "rebased_tickers": len(rebased_tickers),
        "incremental_rows": incremental_rows, "full_pull_rows": full_pull_rows,
    }


def sync(db_path):
    turso = _connect_turso()
    local = models.connect_local(db_path)
    local.execute("PRAGMA foreign_keys = OFF")

    summary = {"legislators_new": sync_append_only(turso, local, "legislators", "id")}
    summary["filings_new"], summary["filings_rechecked"] = sync_filings(turso, local)
    # Plain append-only, unlike filings above - trades no longer have any mutable
    # "still pending" state to recheck now that price columns are gone (see PLAN.md). Known,
    # accepted side effect: a rare late amendment correcting an old, already-synced trade's
    # superseded_by_trade_id/reconciliation_note won't be caught until a full re-export -
    # same class of gap already documented above for filings/trades reconciliation.
    summary["trades_new"] = sync_append_only(turso, local, "trades", "id")
    summary["benchmark_prices_new"] = sync_append_only(turso, local, "benchmark_prices", "date")
    summary["ticker_daily_prices"] = sync_ticker_daily_prices(turso, local)
    for table in SMALL_TABLES:
        summary[f"{table}_refreshed"] = sync_small_table(turso, local, table)

    local.execute("PRAGMA foreign_keys = ON")
    local.commit()
    local.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    args = parser.parse_args()
    print(sync(args.db))


if __name__ == "__main__":
    main()
