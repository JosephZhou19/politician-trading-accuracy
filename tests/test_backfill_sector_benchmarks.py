import datetime
from unittest.mock import patch

from src.db import models
from src.market.prices import TickerHistory
from src.market.sectors import SECTOR_ETFS
from scripts.backfill_sector_benchmarks import OVERLAP_DAYS, backfill_sector_benchmarks


def _insert_stock_trade(conn, ticker, transaction_date):
    leg_id = models.get_or_create_legislator(conn, "Alan", "Armstrong", "senate", "member")
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber="senate", external_filing_id=f"filing-{ticker}",
        filing_type="ptr", is_amendment=False, filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/x/",
        document_format="html", fetched_at="2026-09-05T00:00:00",
    )
    models.insert_trade(
        conn, filing_id=filing_id, source_row_number=1, ticker=ticker, asset_name=f"{ticker} Inc.",
        asset_type="Stock", transaction_type="purchase", transaction_date=transaction_date,
        notification_date=transaction_date, amount_low=1000, owner="self",
    )


def _fake_history(pairs):
    h = TickerHistory("TEST", None, price_type="open")
    h.daily_prices = lambda: pairs
    return h


def test_backfill_loads_every_sector_etf_into_sector_benchmark_prices(conn):
    _insert_stock_trade(conn, "AAPL", "2020-01-05")

    with patch(
        "scripts.backfill_sector_benchmarks.fetch_ticker_history",
        return_value=_fake_history([(datetime.date(2020, 1, 2), 50.0)]),
    ) as mock_fetch:
        summary = backfill_sector_benchmarks(conn)

    assert summary == {sector: 1 for sector in SECTOR_ETFS}
    assert mock_fetch.call_count == len(SECTOR_ETFS)
    called_tickers = {call.args[0] for call in mock_fetch.call_args_list}
    assert called_tickers == set(SECTOR_ETFS.values())
    called_starts = {call.args[1] for call in mock_fetch.call_args_list}
    assert called_starts == {datetime.date(2020, 1, 5)}  # the earliest stock trade date

    rows = conn.execute("SELECT sector, date, price FROM sector_benchmark_prices ORDER BY sector").fetchall()
    assert {r["sector"] for r in rows} == set(SECTOR_ETFS)
    assert all(r["date"] == "2020-01-02" and r["price"] == 50.0 for r in rows)


def test_backfill_no_op_when_no_stock_trades(conn):
    with patch("scripts.backfill_sector_benchmarks.fetch_ticker_history") as mock_fetch:
        summary = backfill_sector_benchmarks(conn)

    assert summary == {}
    mock_fetch.assert_not_called()


def test_backfill_raises_if_one_sector_etf_fetch_fails(conn):
    _insert_stock_trade(conn, "AAPL", "2020-01-05")

    with patch("scripts.backfill_sector_benchmarks.fetch_ticker_history", return_value=None):
        try:
            backfill_sector_benchmarks(conn)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass


def test_recurring_run_only_fetches_since_each_sectors_own_latest_stored_date(conn):
    """Each sector's series is independent - one sector already having data must not affect
    another sector's own first-ever backfill start date."""
    _insert_stock_trade(conn, "AAPL", "2015-01-05")  # would drive a much earlier start otherwise
    models.set_sector_benchmark_prices(conn, "Energy", [("2026-08-01", 90.0)])

    with patch(
        "scripts.backfill_sector_benchmarks.fetch_ticker_history",
        return_value=_fake_history([(datetime.date(2026, 8, 3), 91.0)]),
    ) as mock_fetch:
        summary = backfill_sector_benchmarks(conn)

    assert summary == {sector: 1 for sector in SECTOR_ETFS}
    calls_by_ticker = {call.args[0]: call.args[1] for call in mock_fetch.call_args_list}
    assert calls_by_ticker["XLE"] == datetime.date(2026, 8, 1) - datetime.timedelta(days=OVERLAP_DAYS)
    assert calls_by_ticker["XLF"] == datetime.date(2015, 1, 5)  # no prior data - full history

    rows = conn.execute(
        "SELECT date, price FROM sector_benchmark_prices WHERE sector = 'Energy' ORDER BY date"
    ).fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2026-08-01", 90.0), ("2026-08-03", 91.0)]
