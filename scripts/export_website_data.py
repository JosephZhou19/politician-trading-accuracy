"""Exports a static JSON snapshot of disclosed trades for the (informative-only, no
alpha/signal scoring) congress-trades website.

Deliberately decoupled from Turso: reads only from the local mirror
(scripts/sync_local_mirror.py already keeps this fresh daily), and writes plain JSON files
a static site can serve directly - no live database query ever runs per website visitor,
so site traffic can't touch Turso's read/write quota at all. Run this once a day, after the
daily ingest and price trickle jobs, as a separate step - not a web backend endpoint.

Excludes superseded trades (superseded_by_trade_id IS NOT NULL) - same convention
src/ingest/reconcile_amendments.py uses for "the current, corrected version of a trade."

Output (web/data/):
  legislators.json      - every legislator's id/name/chamber/trade_count/party/state (the
                          directory page) - party/state come from matching against the
                          external unitedstates/congress-legislators roster (see
                          src/analysis/legislator_metadata.py), since our own ingest has no
                          source for either; both are null for anyone we can't match
  trades/{id}.json      - one file per legislator with just their own trades, so a
                          politician page loads only its own data instead of the whole
                          dataset - this is what keeps the export scaling cleanly as more
                          trades accumulate, rather than growing one ever-larger monolithic
                          file every visitor has to download in full
  by_ticker/{t}.json    - same trade records, split by ticker instead of legislator, with
                          the legislator's name embedded (so a ticker page needs no further
                          lookups) - powers the ticker detail page
  tickers.json          - ticker -> asset_name lookup (deduped, since repeating the full
                          asset name on all ~74k trade rows bloated the first version of
                          this export to 27MB for no reason - the name only needs to exist
                          once per ticker)
  latest_prices.json    - one current price per ticker, shared across all of that ticker's
                          trades rather than repeated per-trade
  recent_trades.json    - the newest 300 trades across everyone, with legislator name and
                          ticker embedded, for the homepage feed
  prices/{ticker}.json  - that ticker's full daily price history (one file per actually-
                          traded, priceable ticker, not all ~4,200 - only what a ticker page
                          could ever ask for), powering the ticker page's price chart
  issuers.json          - one row per traded ticker (name, last traded, disclosed volume,
                          trade count, distinct politician count, current price, 30-day
                          price change) for the issuers directory page - the ticker-side
                          equivalent of legislators.json. Sector is deliberately not
                          included: we have no GICS/sector classification data source for
                          ~4,200 tickers without a real new per-ticker data fetch, unlike
                          everything else here which is already a byproduct of data we load
                          anyway.

A trade's own "comment" field (cleaned - see clean_comment) and, for the ~17% of trades with
no real ticker at all (private funds, bonds, spin-offs, corporate-transaction cash-outs -
not every disclosed asset is a tradeable public stock), its "asset_name" are included
directly on each trade record - otherwise a ticker-less row like "Sale (Full), $1,001-
$15,000, no further detail" gives a visitor no way to tell a forced buyout cash-out
(comment: "Sale due to corporate transaction") from an ordinary disclosed sale.
"""
import datetime
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from src.analysis.legislator_metadata import get_legislator_metadata
from src.analysis.price_lookup import load_price_histories

DB_PATH = "data/congress_trades.db"
OUTPUT_DIR = Path("web/data")
TRADES_DIR = OUTPUT_DIR / "trades"
BY_TICKER_DIR = OUTPUT_DIR / "by_ticker"
PRICES_DIR = OUTPUT_DIR / "prices"
RECENT_TRADES_COUNT = 300

# Confirmed live: 489 of 13,079 non-empty trade comments (3.7%) have the PTR form's own
# certification/signature boilerplate OCR'd straight into the comment text, e.g. "Sale of
# 230 shares of Apple, Inc. offeringS Signature the statements I have made on the attached
# and belief. Mr. Lou Barletta , 10/3/2014" - the real comment is everything before
# "offering[s] Signature", consistently, across every sampled case. Cosmetic cleanup only -
# the stored raw_row_text/comment columns themselves are left untouched.
_COMMENT_BOILERPLATE_RE = re.compile(r"\boffering[s]?\s+signature\b.*", re.IGNORECASE | re.DOTALL)


def clean_comment(comment: str | None) -> str | None:
    if not comment:
        return None
    cleaned = _COMMENT_BOILERPLATE_RE.split(comment)[0].strip()
    return cleaned or None


# Raw disclosed asset names carry filing boilerplate that's redundant once the ticker is
# already shown next to the name everywhere on the site - e.g. "Microsoft Corporation -
# Common Stock (MSFT) [ST]" is just "Microsoft Corporation" once the "(MSFT)" and "[ST]" are
# dropped. Order matters: the asset-type tag is always the very last bracketed token, so it's
# stripped first, then the ticker-in-parens (only when it actually matches this row's own
# ticker - a different parenthetical, e.g. "Williams Companies, Inc. (The)", must survive),
# then a trailing "Common Stock" label. Confirmed live against the real exported names before
# landing on this exact order - stripping the ticker before the type tag would leave a
# dangling "[ST]" behind in several real cases.
_ASSET_TYPE_TAG_RE = re.compile(r"\s*\[\w+\]\s*$")
_TRAILING_COMMON_STOCK_RE = re.compile(r"\s*-?\s*Common Stock\s*$", re.IGNORECASE)


def clean_asset_name(name: str | None, ticker: str | None) -> str | None:
    if not name:
        return name
    cleaned = _ASSET_TYPE_TAG_RE.sub("", name)
    if ticker:
        cleaned = re.sub(rf"\s*\({re.escape(ticker)}\)\s*$", "", cleaned)
    cleaned = _TRAILING_COMMON_STOCK_RE.sub("", cleaned)
    return cleaned.strip() or name


# Same scope campaigns.py uses for "can we actually price this" - a real ticker and an
# asset type where the ticker is the priceable instrument (options priced via the
# underlying, same convention as the rest of this project).
PRICEABLE_ASSET_TYPES = ("ST", "Stock", "OP")


def fetch_trade_rows(conn):
    return conn.execute(
        """
        SELECT t.id, f.legislator_id, f.chamber, f.filing_date,
               t.ticker, t.asset_name, t.asset_type, t.transaction_type,
               t.transaction_date, t.notification_date, t.amount_low, t.amount_high, t.owner,
               t.comment
        FROM trades t
        JOIN filings f ON f.id = t.filing_id
        WHERE t.superseded_by_trade_id IS NULL
        ORDER BY t.notification_date DESC
        """
    ).fetchall()


def build_exports(conn, rows, legislator_names: dict[int, str]):
    """Returns (trades_by_legislator, trades_by_ticker, recent_trades, tickers,
    latest_prices, price_series). tickers and latest_prices are each deduped once rather
    than repeated on every trade row. `rows` is already ordered by notification_date
    descending (newest first), so every per-legislator and per-ticker list inherits that
    same newest-first order for free, and the first RECENT_TRADES_COUNT of them are simply
    the newest overall."""
    priceable_tickers = list({
        r["ticker"] for r in rows
        if r["ticker"] and r["asset_type"] in PRICEABLE_ASSET_TYPES
    })
    histories = load_price_histories(conn, priceable_tickers)

    latest_prices: dict[str, dict] = {}
    price_series: dict[str, list[list]] = {}
    for ticker, history in histories.items():
        prices = history.daily_prices()
        if prices:
            latest_date, latest_price = prices[-1]
            latest_prices[ticker] = {"price": latest_price, "date": latest_date.isoformat()}
            price_series[ticker] = [[d.isoformat(), p] for d, p in prices]

    tickers: dict[str, str] = {}
    trades_by_legislator: dict[int, list[dict]] = defaultdict(list)
    trades_by_ticker: dict[str, list[dict]] = defaultdict(list)
    all_trades: list[dict] = []
    for r in rows:
        if r["ticker"] and r["ticker"] not in tickers:
            tickers[r["ticker"]] = clean_asset_name(r["asset_name"], r["ticker"])

        price_at_notification = None
        price_date = None
        history = histories.get(r["ticker"]) if r["ticker"] else None
        if history is not None:
            notification_date = datetime.date.fromisoformat(r["notification_date"])
            point = history.price_point_on_or_after(notification_date + datetime.timedelta(days=1))
            if point is not None:
                price_date, price_at_notification = point[0].isoformat(), point[1]

        trade = {
            "id": r["id"],
            "legislator_id": r["legislator_id"],
            "chamber": r["chamber"],
            "filing_date": r["filing_date"],
            "ticker": r["ticker"],
            # Only carried per-row for ticker-less trades - a real ticker's name already
            # lives once in tickers.json, no need to repeat it on every row.
            "asset_name": clean_asset_name(r["asset_name"], None) if not r["ticker"] else None,
            "asset_type": r["asset_type"],
            "transaction_type": r["transaction_type"],
            "transaction_date": r["transaction_date"],
            "notification_date": r["notification_date"],
            "amount_low": r["amount_low"],
            "amount_high": r["amount_high"],
            "owner": r["owner"],
            "comment": clean_comment(r["comment"]),
            "price_at_notification": price_at_notification,
            # The actual trading day price_at_notification landed on after rolling forward
            # past weekends/holidays - lets the ticker page place this trade as a marker on
            # the exact matching point of its own daily_prices() chart series, not the
            # (possibly non-trading) notification_date itself.
            "price_date": price_date,
        }
        trades_by_legislator[r["legislator_id"]].append(trade)
        all_trades.append(trade)
        if r["ticker"]:
            trade_with_name = dict(trade)
            trade_with_name["legislator_name"] = legislator_names.get(r["legislator_id"], "")
            trades_by_ticker[r["ticker"]].append(trade_with_name)

    recent_trades = []
    for trade in all_trades[:RECENT_TRADES_COUNT]:
        trade_with_names = dict(trade)
        trade_with_names["legislator_name"] = legislator_names.get(trade["legislator_id"], "")
        # trade["asset_name"] is already set (see above) for the ticker-less case - only
        # look it up from the ticker dict when there's a real ticker to look up.
        if trade["ticker"]:
            trade_with_names["asset_name"] = tickers.get(trade["ticker"], "")
        recent_trades.append(trade_with_names)

    return trades_by_legislator, trades_by_ticker, recent_trades, tickers, latest_prices, price_series


def export_legislators(conn, trade_counts: dict[int, int], metadata: dict[int, dict]) -> list[dict]:
    """Default order is most-disclosed-trades first, not alphabetical - a directory led by
    hundreds of untouched 0-trade legislators (the common case: most members only disclose
    a handful of trades, if any) buries the people this site is actually useful for. The
    frontend's sortable column headers still let a visitor switch to alphabetical.

    `metadata` is {id: {"party", "state"}} from legislator_metadata.get_legislator_metadata -
    omitted (not None-filled) for anyone unmatched against the external roster, so a party or
    state of None here just means "not found," not "confirmed independent/no state."""
    rows = conn.execute(
        "SELECT id, first_name, last_name, chamber FROM legislators ORDER BY last_name, first_name"
    ).fetchall()
    legislators = [
        {
            "id": r["id"], "first_name": r["first_name"], "last_name": r["last_name"],
            "chamber": r["chamber"], "trade_count": trade_counts.get(r["id"], 0),
            "party": metadata.get(r["id"], {}).get("party"),
            "state": metadata.get(r["id"], {}).get("state"),
        }
        for r in rows
    ]
    legislators.sort(key=lambda l: -l["trade_count"])
    return legislators


def _price_change_30d(price_series: list[list]) -> float | None:
    """Percent change from ~30 calendar days before the series' last date to the last date
    itself - the same calendar-cutoff-to-index approach the ticker page's range buttons use,
    just computed once here instead of client-side. None if there's no data point that far
    back yet (a ticker priced for under a month) - confirmed live this matters: without this
    check, a ticker with only 2-8 days of real history (DOMO, TBPH, TOELY in one real
    export) silently showed a 2-day swing mislabeled "30-Day Change" on the issuers page,
    since `next(... d >= cutoff ...)` happily matches the series' own first point when
    nothing before the cutoff exists at all."""
    if len(price_series) < 2:
        return None
    last_date = datetime.date.fromisoformat(price_series[-1][0])
    cutoff = (last_date - datetime.timedelta(days=30)).isoformat()
    if price_series[0][0] > cutoff:
        return None
    start_price = next((p for d, p in price_series if d >= cutoff), None)
    if start_price is None or start_price == 0:
        return None
    return (price_series[-1][1] - start_price) / start_price


#: Issuers directory scope, narrower than PRICEABLE_ASSET_TYPES on purpose - this listing is
#: meant to read as "companies," so options (OP) are excluded even though they're priced via
#: the underlying elsewhere on the site. Trade-level filter, not ticker-level: a ticker with
#: some untyped/bond/ETF rows and at least one real stock row still qualifies, counted only by
#: its stock rows - confirmed live that ~1,300 tickers have a mix of typed and untyped trades
#: for the exact same underlying stock (an older filing just missing the field), so filtering
#: out the whole TICKER on one untyped row would wrongly drop real companies.
ISSUER_ASSET_TYPES = ("ST", "Stock")


def export_issuers(
    trades_by_ticker: dict[str, list[dict]], tickers: dict[str, str],
    latest_prices: dict[str, dict], price_series: dict[str, list[list]],
) -> list[dict]:
    """One row per actually-traded stock ticker - the issuer-side equivalent of
    export_legislators. Default order is most-disclosed-trades first, same reasoning as the
    legislator directory."""
    issuers = []
    for ticker, all_trades in trades_by_ticker.items():
        trades = [t for t in all_trades if t["asset_type"] in ISSUER_ASSET_TYPES]
        if not trades:
            continue
        volume = sum((t["amount_low"] + (t["amount_high"] or t["amount_low"])) / 2 for t in trades)
        series = price_series.get(ticker)
        issuers.append({
            "ticker": ticker,
            "asset_name": tickers.get(ticker, ticker),
            "last_traded": max(t["notification_date"] for t in trades),
            "volume": volume,
            "trade_count": len(trades),
            "distinct_politicians": len({t["legislator_id"] for t in trades}),
            "current_price": latest_prices.get(ticker, {}).get("price"),
            "price_change_30d": _price_change_30d(series) if series else None,
        })
    issuers.sort(key=lambda i: -i["trade_count"])
    return issuers


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    BY_TICKER_DIR.mkdir(parents=True, exist_ok=True)
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    legislator_rows = conn.execute("SELECT id, first_name, last_name FROM legislators").fetchall()
    legislator_names = {r["id"]: f"{r['first_name']} {r['last_name']}" for r in legislator_rows}
    legislator_metadata = get_legislator_metadata(
        [(r["id"], r["first_name"], r["last_name"]) for r in legislator_rows]
    )

    rows = fetch_trade_rows(conn)
    trades_by_legislator, trades_by_ticker, recent_trades, tickers, latest_prices, price_series = build_exports(
        conn, rows, legislator_names
    )
    legislators = export_legislators(
        conn, {leg_id: len(t) for leg_id, t in trades_by_legislator.items()}, legislator_metadata,
    )
    issuers = export_issuers(trades_by_ticker, tickers, latest_prices, price_series)

    (OUTPUT_DIR / "legislators.json").write_text(json.dumps(legislators), encoding="utf-8")
    (OUTPUT_DIR / "issuers.json").write_text(json.dumps(issuers), encoding="utf-8")
    (OUTPUT_DIR / "tickers.json").write_text(json.dumps(tickers), encoding="utf-8")
    (OUTPUT_DIR / "latest_prices.json").write_text(json.dumps(latest_prices), encoding="utf-8")
    (OUTPUT_DIR / "recent_trades.json").write_text(json.dumps(recent_trades), encoding="utf-8")
    for leg_id, trades in trades_by_legislator.items():
        (TRADES_DIR / f"{leg_id}.json").write_text(json.dumps(trades), encoding="utf-8")
    for ticker, trades in trades_by_ticker.items():
        (BY_TICKER_DIR / f"{ticker}.json").write_text(json.dumps(trades), encoding="utf-8")
    for ticker, series in price_series.items():
        (PRICES_DIR / f"{ticker}.json").write_text(json.dumps(series), encoding="utf-8")

    trades_kb = sum(f.stat().st_size for f in TRADES_DIR.glob("*.json")) / 1024
    by_ticker_kb = sum(f.stat().st_size for f in BY_TICKER_DIR.glob("*.json")) / 1024
    prices_kb = sum(f.stat().st_size for f in PRICES_DIR.glob("*.json")) / 1024
    print(f"{len(legislators)} legislators, {len(rows)} trades, {len(tickers)} tickers, "
          f"{len(latest_prices)} priced tickers")
    print(f"  legislators.json: {(OUTPUT_DIR / 'legislators.json').stat().st_size / 1024:,.0f} KB")
    print(f"  issuers.json: {(OUTPUT_DIR / 'issuers.json').stat().st_size / 1024:,.0f} KB")
    print(f"  tickers.json: {(OUTPUT_DIR / 'tickers.json').stat().st_size / 1024:,.0f} KB")
    print(f"  latest_prices.json: {(OUTPUT_DIR / 'latest_prices.json').stat().st_size / 1024:,.0f} KB")
    print(f"  recent_trades.json: {(OUTPUT_DIR / 'recent_trades.json').stat().st_size / 1024:,.0f} KB")
    print(f"  prices/*.json: {len(price_series)} files, {prices_kb:,.0f} KB total")
    print(f"  trades/*.json: {len(trades_by_legislator)} files, {trades_kb:,.0f} KB total")
    print(f"  by_ticker/*.json: {len(trades_by_ticker)} files, {by_ticker_kb:,.0f} KB total")

    conn.close()


if __name__ == "__main__":
    main()
