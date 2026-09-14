"""Tests for the politician_yearly_activity view (src/db/schema.sql)."""
from src.db import models

_leg_counter = {"n": 0}


def _insert_trade(conn, legislator, ticker, *, transaction_type="purchase",
                   amount_low=1000, amount_high=1000, price_at_transaction=100.0,
                   price_365d=None, asset_type="Stock", transaction_date="2020-01-05"):
    first, last = legislator
    leg_id = models.get_or_create_legislator(conn, first, last, "senate", "member")
    _leg_counter["n"] += 1
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber="senate",
        external_filing_id=f"filing-{_leg_counter['n']}",
        filing_type="ptr", is_amendment=False, filing_date="2026-07-21",
        source_url="https://efdsearch.senate.gov/search/view/ptr/x/",
        document_format="html", fetched_at="2026-09-05T00:00:00",
    )
    trade_id = models.insert_trade(
        conn, filing_id=filing_id, source_row_number=1,
        ticker=ticker, asset_name=f"{ticker} Inc.", asset_type=asset_type,
        transaction_type=transaction_type, transaction_date=transaction_date,
        notification_date=transaction_date, amount_low=amount_low, amount_high=amount_high,
        owner="self",
    )
    prices = {}
    if price_at_transaction is not None:
        prices["price_at_transaction"] = price_at_transaction
    if price_365d is not None:
        prices["price_365d"] = price_365d
    if prices:
        models.set_trade_prices(conn, trade_id, prices)
    return leg_id, trade_id


def _years(conn, legislator_id):
    return {
        row["year"]: row
        for row in conn.execute(
            "SELECT * FROM politician_yearly_activity WHERE legislator_id = ?", (legislator_id,)
        ).fetchall()
    }


def test_splits_activity_by_calendar_year(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-06-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2021-06-01")

    years = _years(conn, leg_id)
    assert set(years.keys()) == {2020, 2021}
    assert years[2020]["stock_buy_count"] == 1
    assert years[2021]["stock_buy_count"] == 1


def test_total_trades_includes_non_stock_but_stock_counts_dont(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "MUNI", asset_type="Municipal Bond",
                  transaction_date="2020-03-01")

    row = _years(conn, leg_id)[2020]
    assert row["total_trades_all_types"] == 2
    assert row["stock_buy_count"] == 1


def test_distinct_tickers_and_dollar_volume(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-01",
                              amount_low=1000, amount_high=1000)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  transaction_date="2020-06-01", amount_low=500, amount_high=500)
    _insert_trade(conn, ("Alan", "Armstrong"), "MSFT", transaction_date="2020-03-01",
                  amount_low=2000, amount_high=2000)

    row = _years(conn, leg_id)[2020]
    assert row["distinct_tickers_traded"] == 2
    assert row["stock_buy_count"] == 2
    assert row["stock_sell_count"] == 1
    assert row["total_stock_dollar_volume"] == 3500.0  # 1000 + 500 + 2000


def test_1yr_return_weighted_by_dollar_amount(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-01",
                              amount_low=1000, amount_high=1000,
                              price_at_transaction=100.0, price_365d=150.0)  # +50%
    _insert_trade(conn, ("Alan", "Armstrong"), "MSFT", transaction_date="2020-06-01",
                  amount_low=3000, amount_high=3000,
                  price_at_transaction=100.0, price_365d=90.0)  # -10%

    row = _years(conn, leg_id)[2020]
    assert row["trades_with_1yr_data"] == 2
    # weighted: (1000*50% + 3000*-10%) / 4000 = (500 - 300) / 4000 = 5%
    assert abs(row["avg_1yr_return_pct"] - 5.0) < 1e-9
    assert row["win_rate_1yr_pct"] == 50.0  # 1 of 2 was up


def test_1yr_return_null_when_no_qualifying_trades(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2026-01-01",
                              price_at_transaction=100.0, price_365d=None)  # too recent

    row = _years(conn, leg_id)[2026]
    assert row["trades_with_1yr_data"] == 0
    assert row["avg_1yr_return_pct"] is None
    assert row["win_rate_1yr_pct"] is None


def test_sells_excluded_from_1yr_return_since_they_have_no_purchase_basis(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                              transaction_date="2020-01-01",
                              price_at_transaction=100.0, price_365d=150.0)

    row = _years(conn, leg_id)[2020]
    assert row["trades_with_1yr_data"] == 0
    assert row["avg_1yr_return_pct"] is None


def test_scopes_correctly_across_legislators(conn):
    leg1, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-01")
    leg2, _ = _insert_trade(conn, ("Nancy", "Example"), "AAPL", transaction_date="2020-01-01",
                             amount_low=9000, amount_high=9000)

    assert leg1 != leg2
    assert _years(conn, leg1)[2020]["total_stock_dollar_volume"] == 1000.0
    assert _years(conn, leg2)[2020]["total_stock_dollar_volume"] == 9000.0


def test_delisted_ticker_history_still_counts(conn):
    """Unlike politician_totals' "held" columns, this view has no active-price
    requirement - a real historical trade must count even if the ticker is dead today."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "DEAD", transaction_date="2015-01-01")
    checked_at = "2026-01-01T00:00:00Z"
    models.record_zero_response(conn, "DEAD", checked_at)
    for _ in range(models.ZERO_STREAK_DELIST_THRESHOLD - 1):
        models.record_zero_response(conn, "DEAD", checked_at)

    row = _years(conn, leg_id)[2015]
    assert row["stock_buy_count"] == 1
