# Project notes

A running record of non-obvious decisions, real incidents, and analysis findings for this
project — meant to be readable by a fresh session (local or remote) with no prior context.
For deep blow-by-blow history of *how* things were debugged, see `PLAN.md` (gitignored,
local-machine only, not in this repo).

## Architecture summary

- **Ingestion**: `src/ingest/house_clerk.py` and `src/ingest/senate_efd.py` scrape House
  Clerk and Senate eFD disclosure filings respectively; `src/ingest/run_all.py` runs both
  and then `src/ingest/reconcile_amendments.py` to resolve amendment chains. Scheduled via
  `.github/workflows/daily-ingest.yml` (4x/weekday, 2x/weekend).
- **Pricing**: `scripts/backfill_prices.py` fills in transaction/notification/30/90/180/365-
  day prices via yfinance; `scripts/backfill_spy_benchmark.py` maintains an S&P 500 (SPY)
  daily price table for alpha comparisons; `scripts/update_current_prices.py` refreshes
  live prices via Finnhub. All three run in sequence in
  `.github/workflows/daily-price-trickle.yml` (1x/weekday).
- **Storage**: Turso (managed libSQL/SQLite). `src/db/models.py` is the sole DB access
  layer; `src/db/schema.sql` holds the schema plus three analytics views
  (`politician_ticker_positions`, `politician_totals`, `politician_yearly_activity`).
- **Analysis**: this is a personal analysis tool, not a product with a UI — "using it" means
  querying the views/DB directly (see "Copytrading analysis" below), not visiting a website.

## Real incidents and fixes (chronological, most recent first)

### Daily-price-trickle crash: Turso idle-transaction rollback (2026-09-15, fixed same day)
`update_current_prices.py` batches up to 50 ticker updates per Turso commit. A slow run of
Finnhub calls (some 403s from mutual-fund tickers Finnhub's free tier can't quote, one 10s
read timeout) left a transaction open long enough that Turso rolled it back server-side:
`"interactive transaction was rolled back because the stream was idle for too long"`. The
existing reconnect-and-retry logic in `_TursoConnection._with_reconnect` only recognized a
different, unrelated error string (`"stream not found"`, a stale-session issue), so this
new message crashed the job uncaught. Fixed by recognizing both recoverable error patterns
(`src/db/models.py`, `_RECOVERABLE_STREAM_ERRORS`). No data was lost — the crash happened
before the resume cursor gets saved, so the retry just redid the same ground.

### Four Turso rows-read/cost bugs (2026-09-14)
A ~57M-row-read spike on Turso's dashboard traced to real bugs, all fixed and verified
against production:
1. **Ingest N+1 dedup**: the House/Senate scrapers checked "already ingested?" via one DB
   round-trip per candidate filing (~11,776 round trips/run) - fixed via one bulk
   `get_filing_statuses_by_chamber` fetch per chamber, checked in-memory. Mainly a
   **latency** fix (this had also just caused a run to hit the 30-min GH Actions timeout);
   each old round trip only read 1 row, so its rows-read impact alone was small.
2. **`reconcile_house_amendments`** (dominant cost): checked
   `WHERE superseded_by_trade_id = ?` with no index - a full 74k-row scan repeated ~665x
   per run (~49.5M rows/run). Fixed with a partial index + one bulk fetch of the resolved
   set. This was almost the entire spike.
3. **`get_tickers_due_for_price_check`**: a correlated `EXISTS` re-evaluated once per trade
   row instead of once per distinct ticker (~330-365k rows/call). Rewrote as a single-pass
   CTE (~65k rows/call).
4. **`backfill_delisted_status`**: identical pattern to #3, same fix. Low real impact (a
   manual one-time script, not scheduled) but fixed for consistency.

Estimated new steady-state cost: **~4.8M rows/month** (ingest ~1.56M across ~104 runs +
trickle ~3.2M across ~22 runs), down from an estimated 5+ billion/month if bug #2 had
continued at that cadence. One known, accepted remaining inefficiency:
`get_trades_needing_prices` does a genuine ~74k-row scan per call (inherent to its
multi-condition `OR`/computed-date `WHERE` clause, not a missing-index bug) - ~150-450x
smaller than the fixed bugs, not worth the complexity of fixing further right now.

### Tooling gotcha: `python-dotenv`'s search path
`load_dotenv()` with no explicit path searches upward from the *calling script's own file
location*, not the working directory. A throwaway script saved outside the project (e.g. a
scratchpad/temp dir) silently fails to find `.env`, and `models.connect()` falls back to a
local `data/congress_trades.db` file - which can exist and receive occasional real writes
too, producing plausible-looking but wrong results with **no error**. `python -c "..."` is
unaffected (no calling file, falls back to CWD-based search, which works). Any one-off
script placed outside the project directory should load `.env` by explicit absolute path.

## Local mirror + incremental sync

Built to let analysis happen against a local copy with zero Turso rows-read cost:
- One-time full export: 7 tables, 94,336 rows total (~188k rows-read including the count
  check) - cheap in absolute terms, but a *repeated* full re-export would cost more per
  month than the whole ingest+trickle pipeline.
- Incremental sync (`scripts/sync_local_mirror.py`, **built and tested, not yet committed
  as of this writing** - check `git log`/`git status` before assuming it's merged): new
  rows via `id`/`date` > local's own max (correct, not just fast, for tables that never
  update existing rows) + a targeted re-fetch by id of whatever's locally still "pending"
  for `filings`/`trades` (reusing the real pipeline's own pending-definitions, not a
  separately invented one). First real sync after the full export: 4,297 rows vs 94,336 for
  a full re-pull (95% reduction).

## Copytrading analysis: methodology and findings

Goal: identify which members of Congress, if any, show real evidence of stock-picking
skill worth following. Built iteratively, each layer added specifically to rule out a way
the previous layer's numbers could be misleading. **Also not yet committed as reusable
code** - built and run as one-off local scripts against the local mirror; only
`src/analysis/realized_gains.py` (FIFO realized-gain matching, tested) exists as an actual
module, and even that is uncommitted as of this writing.

Layers, in the order they were built and why each one was needed:

1. **Unrealized gain / alpha vs SPY** (`politician_totals` view) - dollar-weighted average
   return vs. holding SPY over the same window, per trade. Raw gain%/dollar-gain rankings
   are dominated by one early lucky/large position (confirmed: Wyden's 477.9% gain-pct is
   almost entirely one NVDA position at $9.65 avg cost; Rooney's 1108.8% is almost entirely
   one FTAI position at $8.72) - not evidence of repeatable skill.
2. **Realized gain** (FIFO dollar-lot matching, since disclosures report dollar brackets,
   not share counts) - catches money already taken off the table, which unrealized-only
   metrics miss entirely. **Major caveat, confirmed not assumed**: median coverage
   (`realized_proceeds / (realized_proceeds + unpriced_sale_dollars)`) across all
   legislators is only ~8%, because disclosures only go back to 2012 - a sale with no
   matching purchase in-window is structurally invisible, hitting the longest-tenured
   members hardest. Combining realized + unrealized surfaced two names invisible to every
   prior unrealized-only ranking: David Perdue ($21.4M realized, $1.8M unrealized) and
   Sheldon Whitehouse ($14.7M realized, $3.7M unrealized).
3. **Active-in-2026 filter** (`last_stock_trade_date`) - of 285 legislators with any stock
   history, only 82 (29%) have traded at all in 2026. No point ranking someone who's
   stopped trading.
4. **Ticker-level dedup for win-rate/accuracy** - trade-count "n" overstates independent
   sample size for anyone who dollar-cost-averages (buys the same ticker repeatedly).
   Gilbert Cisneros's "696 trades" is really 354 distinct tickers; some names had 4-6x
   repetition. Fix: dedupe to one dollar-weighted vote per distinct ticker before computing
   accuracy. (Alpha *magnitude*, being dollar-weighted already, doesn't need this fix - only
   count-based accuracy does.)
5. **Notification-lag adjustment** - disclosures can lag the actual trade by weeks (STOCK
   Act allows up to 45 days); a copytrader can only ever act on the notification-date price,
   never the transaction-date price. Recomputing alpha using `price_at_notification` as
   entry (both legs, stock and SPY) instead of `price_at_transaction`: average absolute
   price shift during the lag window is **6.47%** across 54,741 trades - not negligible.
   **Lisa McClain's entire apparent edge (12.9% alpha) reversed to -1.7%** under this
   adjustment - it was a measurement artifact from an unrealistic entry price, not a real
   edge. Several others (Wasserman Schultz, King, Evans, Pelosi) were robust or even
   improved under the adjustment.
6. **Persistence test** - split each legislator's own trade history at their own median
   date into first-half/second-half, compute alpha independently in each, then correlate
   across the population. Result: Pearson r = 0.343 across 137 legislators with enough data
   in both halves (weak-to-moderate, real but far from strong - r² ≈ 0.12). **37% of active
   traders flip sign entirely between halves.** McClain fails this too, independently
   (23.7% -> -1.6%), reinforcing #5. Pelosi is the most stable name checked (8.8% -> 9.8%).
7. **Concentration ratio + statistical significance** - what fraction of a legislator's
   positive alpha-dollars comes from their single best ticker, and is their ticker-level
   win rate distinguishable from a coin flip (exact two-sided binomial test) given sample
   size. Result: **not one name in the top-10-by-alpha list has a win rate statistically
   distinguishable from 50% in the positive direction** (McClain is the only significant
   result, and it's significantly *worse* than chance, p<0.001). Even Cisneros's 354-ticker
   sample lands at p=0.958 - exactly what a coin flip looks like at that sample size.
   Concentration varies a lot: Pelosi (56%, essentially an NVDA story) and Crenshaw (64%,
   one ETF) are single-position stories; Cisneros is the least concentrated found (18%),
   his edge spread across several large winners rather than one.

### Bottom-line verdict (as of this analysis)

No legislator in this dataset has a *proven* stock-picking edge - nobody's win rate clears
statistical significance in the positive direction. What the data does show: a small
number of people (Pelosi, Cisneros, Perdue, Whitehouse) made a few outsized bets, sized
them large, and held on - real money, but "conviction and patience on a couple of picks,"
not "reliably picks winners." Important nuance: **failing to find statistical significance
is not the same as proving there's no edge** - individual stock returns are noisy enough
that a real-but-modest picking edge, or a real sizing/conviction-based edge (as opposed to
pick-frequency-based), could both be genuinely present and still be undetectable at these
sample sizes with the tests built so far. Not proven good; not proven bad either - the
honest state is unresolved, and specifically requires more evidence (larger samples,
external data like committee assignments) before it could be, not just more of the same
kind of test.

### Not yet built (natural next steps if continuing this thread)
- Committee-assignment / sector cross-reference (needs external data not currently in this
  DB) - the classic congressional-trading conflict-of-interest signal.
- A test that looks at *when* someone sizes up a position (before vs. after the fact) to
  try to distinguish deliberate conviction-sizing from lucky concentration.
- Formalizing the ticker-dedup/notification-lag/persistence/significance logic above into
  real, tested modules under `src/analysis/` instead of one-off local scripts, if this
  thread gets picked back up.
