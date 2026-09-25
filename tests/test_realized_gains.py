"""Tests for FIFO dollar-lot realized-gain matching (src/analysis/realized_gains.py)."""
from src.analysis.realized_gains import compute_realized_gains
from src.db import models

_leg_counter = {"n": 0}


def _insert_trade(conn, legislator, ticker, *, transaction_type="purchase",
                   amount_low=10000, amount_high=10000, price_at_transaction=10.0,
                   asset_type="Stock", transaction_date="2024-01-01", chamber="senate"):
    first, last = legislator
    leg_id = models.get_or_create_legislator(conn, first, last, chamber, "member")
    _leg_counter["n"] += 1
    filing_id = models.insert_filing(
        conn, legislator_id=leg_id, chamber=chamber,
        external_filing_id=f"filing-{_leg_counter['n']}",
        filing_type="ptr", is_amendment=False, filing_date=transaction_date,
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


def test_simple_fifo_gain(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                               transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  price_at_transaction=30.0, transaction_date="2024-06-01")

    result = compute_realized_gains(conn)[leg_id]
    assert result.realized_gain == 20000.0
    assert result.realized_proceeds == 10000.0
    assert result.unpriced_sale_dollars == 0.0


def test_partial_lot_consumption_leaves_remainder_for_next_sale(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                               transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_partial",
                  amount_low=4000, amount_high=4000, price_at_transaction=20.0,
                  transaction_date="2024-02-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_partial",
                  amount_low=6000, amount_high=6000, price_at_transaction=30.0,
                  transaction_date="2024-03-01")

    result = compute_realized_gains(conn)[leg_id]
    # First sale: 4000 * (20-10)/10 = 4000. Second sale draws the remaining 6000 of the
    # SAME lot (still priced at 10): 6000 * (30-10)/10 = 12000.
    assert result.realized_gain == 4000.0 + 12000.0
    assert result.realized_proceeds == 10000.0


def test_sale_spans_two_lots_in_fifo_order(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                               transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=20.0,
                  transaction_date="2024-02-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  amount_low=15000, amount_high=15000, price_at_transaction=30.0,
                  transaction_date="2024-03-01")

    result = compute_realized_gains(conn)[leg_id]
    # Consumes all of the older $10-cost lot (10000 * (30-10)/10 = 20000) then 5000 of the
    # $20-cost lot (5000 * (30-20)/20 = 2500) - oldest lot first, not cheapest or priciest.
    assert result.realized_gain == 20000.0 + 2500.0
    assert result.realized_proceeds == 15000.0


def test_missing_buy_price_counts_as_unpriced_not_zero_gain(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=None,
                               transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  price_at_transaction=30.0, transaction_date="2024-02-01")

    result = compute_realized_gains(conn)[leg_id]
    assert result.realized_gain == 0.0
    assert result.realized_proceeds == 0.0
    assert result.unpriced_sale_dollars == 10000.0


def test_missing_sale_price_counts_as_unpriced(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                               transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  price_at_transaction=None, transaction_date="2024-02-01")

    result = compute_realized_gains(conn)[leg_id]
    assert result.realized_gain == 0.0
    assert result.unpriced_sale_dollars == 10000.0


def test_selling_more_than_ever_bought_is_unpriced_not_a_crash(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                               price_at_transaction=30.0, transaction_date="2024-01-01")

    result = compute_realized_gains(conn)[leg_id]
    assert result.realized_gain == 0.0
    assert result.unpriced_sale_dollars == 10000.0


def test_sale_full_does_not_clear_other_lots_same_as_sale_partial(conn):
    """sale_full is the filer's characterization of one transaction, not a guarantee the
    whole (legislator, ticker) pool is zeroed - a separate spouse/joint sub-holding could
    still be open. A sale_full for less than the full lot queue must only consume its own
    dollar amount, same as sale_partial would."""
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                               transaction_date="2024-01-01")  # lot A: 10000 @ 10
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=20.0,
                  transaction_date="2024-02-01")  # lot B: 10000 @ 20
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  amount_low=5000, amount_high=5000, price_at_transaction=30.0,
                  transaction_date="2024-03-01")  # consumes half of lot A only
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_partial",
                  amount_low=5000, amount_high=5000, price_at_transaction=40.0,
                  transaction_date="2024-04-01")  # should still draw the REST of lot A, not lot B

    result = compute_realized_gains(conn)[leg_id]
    # sale_full: 5000*(30-10)/10 = 10000. Second sale hits lot A's remaining 5000 @ 10:
    # 5000*(40-10)/10 = 15000. If sale_full had wrongly cleared lot A, the second sale would
    # instead draw lot B @ 20, giving 5000*(40-20)/20 = 5000 - a clearly different number.
    assert result.realized_gain == 10000.0 + 15000.0


def test_exchange_transactions_are_excluded(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                               transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="exchange",
                  price_at_transaction=999.0, transaction_date="2024-01-15")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  price_at_transaction=30.0, transaction_date="2024-06-01")

    result = compute_realized_gains(conn)[leg_id]
    assert result.realized_gain == 20000.0


def test_tickers_and_legislators_do_not_share_lots(conn):
    aapl_leg, _ = _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", price_at_transaction=10.0,
                                 transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "GOOG", price_at_transaction=50.0,
                  transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "AAPL", transaction_type="sale_full",
                  price_at_transaction=30.0, transaction_date="2024-06-01")

    other_leg, _ = _insert_trade(conn, ("Ben", "Cardin"), "AAPL", price_at_transaction=999.0,
                                  transaction_date="2024-01-01")

    results = compute_realized_gains(conn)
    # Armstrong's AAPL sale must use AAPL's own $10 lot, unaffected by his open GOOG lot or
    # Cardin's entirely separate (legislator, AAPL) pool.
    assert results[aapl_leg].realized_gain == 20000.0
    assert results[other_leg].realized_gain == 0.0


def test_non_stock_asset_type_is_excluded(conn):
    leg_id, _ = _insert_trade(conn, ("Alan", "Armstrong"), "MUNIBOND", asset_type="OL",
                               price_at_transaction=10.0, transaction_date="2024-01-01")
    _insert_trade(conn, ("Alan", "Armstrong"), "MUNIBOND", asset_type="OL",
                  transaction_type="sale_full", price_at_transaction=999.0,
                  transaction_date="2024-06-01")

    results = compute_realized_gains(conn)
    assert leg_id not in results
