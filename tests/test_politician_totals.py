"""Tests for the politician_totals view (src/db/schema.sql)."""
import datetime

from src.db import models

_leg_counter = {"n": 0}


def _insert_trade(conn, legislator, ticker, *, transaction_type="purchase",
                   amount_low=1000, amount_high=1000, price_at_transaction=100.0,
                   price_365d=None, asset_type="Stock", transaction_date="2026-01-05"):
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


def _set_price(conn, ticker, price):
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    models.record_real_price(conn, ticker, price, checked_at)


def _totals(conn, legislator_id):
    return conn.execute(
        "SELECT * FROM politician_totals WHERE legislator_id = ?", (legislator_id,)
    ).fetchone()


def test_total_trades_counts_every_asset_type(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", asset_type="Stock")
    _insert_trade(conn, ("Alan", "Armstrong"), "MUNI", asset_type="Municipal Bond")
    _set_price(conn, "AAPL", 150.0)

    row = _totals(conn, leg_id)
    assert row["total_trades_all_types"] == 2  # includes the non-stock trade
    assert row["total_stock_buy_count"] == 1   # stock-only total excludes it


def test_latest_trade_date_reflects_any_asset_type(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2026-01-05")
    _insert_trade(conn, ("Alan", "Armstrong"), "MUNI", asset_type="Municipal Bond",
                  transaction_date="2026-06-01")
    _set_price(conn, "AAPL", 150.0)

    assert _totals(conn, leg_id)["latest_trade_date"] == "2026-06-01"


def test_stock_totals_match_position_view(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=200.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "MSFT", price_at_transaction=50.0)
    _set_price(conn, "AAPL", 300.0)
    _set_price(conn, "MSFT", 100.0)

    row = _totals(conn, leg_id)
    assert row["distinct_tickers_held"] == 2
    assert row["total_stock_buy_count"] == 3
    assert row["total_stock_sell_count"] == 0
    # stock_net_worth is current market value, not gain: AAPL 2000 * (300/150) = 4000,
    # MSFT 1000 * (100/50) = 2000 -> 6000 total
    assert row["stock_net_worth"] == 6000.0
    # cost basis: AAPL 2000 + MSFT 1000 = 3000; gain = 6000 - 3000 = 3000; gain_pct = 100%
    assert row["stock_total_cost_basis"] == 3000.0
    assert row["total_estimated_gain"] == 3000.0
    assert row["gain_pct"] == 100.0
    # both positions are in profit (300 > 150, 100 > 50)
    assert row["win_count"] == 2
    assert row["win_rate_pct"] == 100.0
    # all-time dollar volume across the 3 buys, regardless of current holdings
    assert row["total_stock_dollar_volume"] == 3000.0


def test_win_rate_reflects_a_mix_of_winners_and_losers(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "WINNER", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "LOSER", price_at_transaction=100.0)
    _set_price(conn, "WINNER", 200.0)  # up
    _set_price(conn, "LOSER", 50.0)    # down

    row = _totals(conn, leg_id)
    assert row["distinct_tickers_held"] == 2
    assert row["win_count"] == 1
    assert row["win_rate_pct"] == 50.0
    assert row["gain_pct"] == 25.0  # net_worth (1000*2 + 1000*0.5=2500) vs cost basis 2000 -> +25%


def test_stock_trade_dates_and_years_active(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL",
                              transaction_date="2020-01-01", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL",
                  transaction_date="2022-01-01", price_at_transaction=150.0)
    _set_price(conn, "AAPL", 200.0)

    row = _totals(conn, leg_id)
    assert row["first_stock_trade_date"] == "2020-01-01"
    assert row["last_stock_trade_date"] == "2022-01-01"
    assert 1.99 < row["years_active"] < 2.01  # ~2 years apart, allowing for the 365.25 divisor


def test_dollar_volume_includes_exited_and_delisted_tickers_unlike_held_columns(conn):
    """total_stock_dollar_volume is a lifetime-activity metric, deliberately broader than
    distinct_tickers_held/stock_net_worth - it must still count a position that was fully
    sold or whose ticker later delisted, unlike the "held" columns."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "GONE", price_at_transaction=100.0,
                              amount_low=5000, amount_high=5000)
    _insert_trade(conn, ("Alan", "Armstrong"), "GONE", transaction_type="sale_full",
                  amount_low=5000, amount_high=5000, price_at_transaction=None)
    # No ticker_prices row at all for GONE - excluded from every "held" column.

    row = _totals(conn, leg_id)
    assert row["distinct_tickers_held"] == 0
    assert row["stock_net_worth"] is None
    assert row["total_stock_dollar_volume"] == 10000.0  # both the buy and the sell still count


def test_legislator_with_no_trades_still_appears(conn):
    leg_id = models.get_or_create_legislator(conn, "Nobody", "Traded", "senate", "member")

    row = _totals(conn, leg_id)
    assert row is not None
    assert row["total_trades_all_types"] == 0
    assert row["distinct_tickers_held"] == 0
    assert row["total_stock_buy_count"] == 0
    assert row["stock_net_worth"] is None
    assert row["latest_trade_date"] is None


def test_exchange_excluded_from_stock_totals_but_counted_overall(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="exchange",
                  amount_low=99999, amount_high=99999)
    _set_price(conn, "AAPL", 150.0)

    row = _totals(conn, leg_id)
    assert row["total_trades_all_types"] == 2
    assert row["total_stock_buy_count"] == 1
    assert row["total_stock_sell_count"] == 0


def test_holds_ticker_but_net_worth_null_without_cost_basis(conn):
    """distinct_tickers_held should reflect a known position even when stock_net_worth can't
    be computed - the two must be independently readable, not conflated."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=None)
    _set_price(conn, "AAPL", 150.0)

    row = _totals(conn, leg_id)
    assert row["distinct_tickers_held"] == 1
    assert row["stock_net_worth"] is None


def test_delisted_ticker_excluded_from_stock_totals_same_as_position_view(conn):
    """Regression: distinct_tickers_held/buy_count/sell_count must match exactly what
    politician_ticker_positions considers a current holding - caught for real against
    production data (54 vs 64) before this filter was added."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=100.0)
    _insert_trade(conn, ("Alan", "Armstrong"), "DEAD", price_at_transaction=5.0)
    _set_price(conn, "AAPL", 150.0)
    _set_price(conn, "DEAD", 1.0)
    conn.execute("UPDATE ticker_prices SET price_status = 'delisted' WHERE ticker = 'DEAD'")
    conn.commit()

    position_count = conn.execute(
        "SELECT COUNT(*) as n FROM politician_ticker_positions WHERE legislator_id = ?", (leg_id,)
    ).fetchone()["n"]
    row = _totals(conn, leg_id)

    assert position_count == 1  # only AAPL - DEAD is excluded by the view
    assert row["distinct_tickers_held"] == position_count
    assert row["total_stock_buy_count"] == 1
    assert row["total_trades_all_types"] == 2  # but still counted here - "all types" is literal


def test_all_time_alpha_matches_yearly_view(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-02",
                              price_at_transaction=100.0, price_365d=150.0)  # +50%
    models.set_benchmark_prices(conn, [("2020-01-02", 100.0), ("2021-01-02", 110.0)])  # spy +10%

    row = _totals(conn, leg_id)
    assert row["trades_with_alpha_data"] == 1
    assert abs(row["avg_1yr_alpha_pct"] - 40.0) < 1e-9  # (+50%) - (+10%)


def test_all_time_alpha_null_without_benchmark_data(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_date="2020-01-02",
                              price_at_transaction=100.0, price_365d=150.0)
    # No benchmark_prices rows loaded at all.

    row = _totals(conn, leg_id)
    assert row["trades_with_alpha_data"] == 0
    assert row["avg_1yr_alpha_pct"] is None
