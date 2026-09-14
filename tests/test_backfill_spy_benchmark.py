import datetime
from unittest.mock import patch

from src.db import models
from src.market.prices import TickerHistory
from scripts.backfill_spy_benchmark import backfill_spy_benchmark


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


def test_backfill_loads_spy_history_into_benchmark_prices(conn):
    _insert_stock_trade(conn, "AAPL", "2020-01-05")

    fake_history = TickerHistory("SPY", None, price_type="open")
    fake_history.daily_prices = lambda: [
        (datetime.date(2020, 1, 2), 300.0),
        (datetime.date(2020, 1, 3), 301.0),
    ]

    with patch("scripts.backfill_spy_benchmark.fetch_ticker_history", return_value=fake_history) as mock_fetch:
        count = backfill_spy_benchmark(conn)

    assert count == 2
    mock_fetch.assert_called_once()
    called_ticker, called_start, _called_end = mock_fetch.call_args[0]
    assert called_ticker == "SPY"
    assert called_start == datetime.date(2020, 1, 5)  # the earliest stock trade date

    rows = conn.execute("SELECT * FROM benchmark_prices ORDER BY date").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2020-01-02", 300.0), ("2020-01-03", 301.0)]


def test_backfill_no_op_when_no_stock_trades(conn):
    with patch("scripts.backfill_spy_benchmark.fetch_ticker_history") as mock_fetch:
        count = backfill_spy_benchmark(conn)

    assert count == 0
    mock_fetch.assert_not_called()


def test_backfill_raises_if_spy_fetch_fails(conn):
    _insert_stock_trade(conn, "AAPL", "2020-01-05")

    with patch("scripts.backfill_spy_benchmark.fetch_ticker_history", return_value=None):
        try:
            backfill_spy_benchmark(conn)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass
