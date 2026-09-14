import datetime
import math
from unittest.mock import Mock, patch

import pandas as pd

from src.market.prices import TickerHistory, _normalize_ticker, fetch_ticker_history_stockanalysis


def _history(dates_and_prices):
    dates = [datetime.date(*d) for d, _ in dates_and_prices]
    prices = [p for _, p in dates_and_prices]
    return TickerHistory("TEST", pd.Series(prices, index=pd.Index(dates)))


def test_price_on_exact_trading_day():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), 101.0)])
    assert h.price_on_or_after(datetime.date(2024, 1, 3)) == 101.0


def test_price_rolls_forward_over_weekend():
    # 2024-01-06 is a Saturday; next trading day is Monday 2024-01-08
    h = _history([((2024, 1, 5), 100.0), ((2024, 1, 8), 105.0)])
    assert h.price_on_or_after(datetime.date(2024, 1, 6)) == 105.0


def test_price_rolls_forward_over_holiday_gap():
    h = _history([((2024, 12, 24), 100.0), ((2024, 12, 26), 110.0)])
    assert h.price_on_or_after(datetime.date(2024, 12, 25)) == 110.0


def test_price_beyond_available_history_returns_none():
    h = _history([((2024, 1, 2), 100.0)])
    assert h.price_on_or_after(datetime.date(2024, 6, 1)) is None


def test_daily_prices_returns_every_real_point():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), 101.0)])
    assert h.daily_prices() == [(datetime.date(2024, 1, 2), 100.0), (datetime.date(2024, 1, 3), 101.0)]


def test_daily_prices_skips_nan():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), math.nan)])
    assert h.daily_prices() == [(datetime.date(2024, 1, 2), 100.0)]


def test_price_does_not_roll_forward_across_a_recycled_ticker_gap():
    """Regression: MON (Monsanto, delisted 2018) was recycled by an unrelated company
    trading under the same symbol from 2021 - querying MON for a real 2014 Monsanto trade
    date, with no cap, silently returned the unrelated company's 2021 price as if it were
    Monsanto's. A multi-year gap must be rejected, not treated like a weekend/holiday."""
    h = _history([((2021, 3, 16), 9.785)])
    assert h.price_on_or_after(datetime.date(2014, 9, 30)) is None


def test_price_still_rolls_forward_within_the_cap():
    # a 4-day gap (e.g. a holiday weekend) must still work
    h = _history([((2024, 1, 8), 100.0)])
    assert h.price_on_or_after(datetime.date(2024, 1, 4)) == 100.0


def test_price_skips_past_nan_gap():
    """A trading-halt or data-gap day can have no real Open (NaN) - confirmed this happens
    on real data. Must not be returned as if it were a real price."""
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), math.nan), ((2024, 1, 4), 102.0)])
    assert h.price_on_or_after(datetime.date(2024, 1, 3)) == 102.0


def test_price_all_remaining_days_nan_returns_none():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), math.nan)])
    assert h.price_on_or_after(datetime.date(2024, 1, 3)) is None


def test_normalize_ticker_converts_period_to_hyphen():
    """Yahoo requires a hyphen for share classes (BRK-B); disclosures often use a period."""
    assert _normalize_ticker("BRK.B") == "BRK-B"


def test_normalize_ticker_leaves_plain_ticker_unchanged():
    assert _normalize_ticker("AAPL") == "AAPL"


def _mock_response(json_body, status=200):
    resp = Mock()
    resp.status_code = status
    resp.raise_for_status = Mock() if status == 200 else Mock(side_effect=Exception("http error"))
    resp.json = Mock(return_value=json_body)
    return resp


def test_stockanalysis_fallback_converts_timestamps_as_utc():
    """Regression: date.fromtimestamp() uses local time and shifted a real timestamp back a
    day (ATVI's actual 2023-10-13 delisting date came out as 2023-10-12 on this machine's
    timezone) - the API's timestamps are UTC-midnight markers and must be read as UTC."""
    # 1697155200000 ms = 2023-10-13T00:00:00Z
    body = {"status": 200, "data": [[1697155200000, 94.42]]}
    with patch("src.market.prices.requests.get", return_value=_mock_response(body)):
        h = fetch_ticker_history_stockanalysis("ATVI")
    assert h.price_type == "close"
    assert h.price_on_or_after(datetime.date(2023, 10, 13)) == 94.42


def test_stockanalysis_fallback_returns_none_on_empty_data():
    body = {"status": 200, "data": []}
    with patch("src.market.prices.requests.get", return_value=_mock_response(body)):
        assert fetch_ticker_history_stockanalysis("UNKNOWNTICKER") is None


def test_stockanalysis_fallback_returns_none_on_request_failure():
    import requests

    with patch("src.market.prices.requests.get", side_effect=requests.RequestException("boom")):
        assert fetch_ticker_history_stockanalysis("ATVI") is None
