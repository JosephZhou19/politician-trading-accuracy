"""Tests for the politician_ticker_positions view (src/db/schema.sql)."""
import datetime

from src.db import models

_leg_counter = {"n": 0}


def _insert_trade(conn, legislator, ticker, *, transaction_type="purchase",
                   amount_low=1000, amount_high=1000, price_at_transaction=100.0,
                   asset_type="Stock", transaction_date="2026-01-05"):
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
    if price_at_transaction is not None:
        models.set_trade_prices(conn, trade_id, {"price_at_transaction": price_at_transaction})
    return leg_id, trade_id


def _set_price(conn, ticker, price, status="active"):
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if price is not None:
        models.record_real_price(conn, ticker, price, checked_at)
    if status == "delisted":
        conn.execute("UPDATE ticker_prices SET price_status = 'delisted' WHERE ticker = ?", (ticker,))
        conn.commit()


def _positions(conn, legislator_id=None):
    sql = "SELECT * FROM politician_ticker_positions"
    params = ()
    if legislator_id is not None:
        sql += " WHERE legislator_id = ?"
        params = (legislator_id,)
    return conn.execute(sql, params).fetchall()


def test_net_position_buy_and_sell_and_avg_cost(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=200.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  amount_low=500, amount_high=500, price_at_transaction=None)
    _set_price(conn, "AAPL", 300.0)

    rows = _positions(conn, leg_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["net_position_estimate"] == 1500.0  # 1000 + 1000 - 500
    assert row["buy_count"] == 2
    assert row["sell_count"] == 1
    assert row["avg_cost"] == 150.0  # (100*1000 + 200*1000) / 2000
    assert row["current_price"] == 300.0
    assert row["estimated_gain"] == 1500.0  # 1500 * (300-150)/150


def test_excludes_exchange_trades_from_counts_and_position(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="exchange",
                  amount_low=99999, amount_high=99999, price_at_transaction=100.0)
    _set_price(conn, "AAPL", 150.0)

    row = _positions(conn, leg_id)[0]
    assert row["net_position_estimate"] == 1000.0
    assert row["buy_count"] == 1
    assert row["sell_count"] == 0


def test_excludes_non_stock_asset_type(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "MUNI", asset_type="Municipal Bond")
    _set_price(conn, "MUNI", 100.0)

    assert _positions(conn, leg_id) == []


def test_excludes_delisted_ticker(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "DEAD")
    _set_price(conn, "DEAD", 5.0, status="delisted")

    assert _positions(conn, leg_id) == []


def test_excludes_ticker_with_no_price_row(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "NOPRICE")
    assert _positions(conn, leg_id) == []


def test_excludes_fully_exited_position(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  price_at_transaction=None)  # same $1000 amount -> net exactly 0
    _set_price(conn, "AAPL", 150.0)

    assert _positions(conn, leg_id) == []


def test_scopes_correctly_across_legislators(conn):
    leg1, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    leg2, _ = _insert_trade(conn, ("Nancy", "Example"), "AAPL", price_at_transaction=100.0,
                             amount_low=5000, amount_high=5000)
    _set_price(conn, "AAPL", 150.0)

    assert leg1 != leg2
    row1 = _positions(conn, leg1)[0]
    row2 = _positions(conn, leg2)[0]
    assert row1["net_position_estimate"] == 1000.0
    assert row2["net_position_estimate"] == 5000.0


def test_avg_cost_ignores_buys_missing_price(conn):
    """Both the numerator and denominator of the weighted average must skip a buy with no
    price_at_transaction - otherwise it would silently drag the average toward zero instead
    of just being excluded from it."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=None,
                  amount_low=9000, amount_high=9000)
    _set_price(conn, "AAPL", 150.0)

    row = _positions(conn, leg_id)[0]
    assert row["avg_cost"] == 100.0  # not diluted by the $9000 unpriced buy
    assert row["net_position_estimate"] == 10000.0  # both buys still count toward position


def test_estimated_gain_is_null_without_avg_cost(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=None)
    _set_price(conn, "AAPL", 150.0)

    row = _positions(conn, leg_id)[0]
    assert row["avg_cost"] is None
    assert row["estimated_gain"] is None
    assert row["net_position_estimate"] == 1000.0  # still shown - position math doesn't need price
