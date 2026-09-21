import datetime
import math
from unittest.mock import Mock, patch

import pandas as pd

from src.market.prices import (
    TickerHistory,
    _normalize_ticker,
    fetch_ticker_history,
    fetch_ticker_history_stockanalysis,
)


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


def test_price_skips_implausibly_tiny_garbage_value():
    """Regression: yfinance's own historical Open data came back as 8e-07 for DAIUF
    (Daifuku Co Ltd, a real ~$35 stock) on real 2019 dates, and 9.8e-25 for AOZOF (a real
    ~$14 stock) - not noisy data, garbage many orders of magnitude off. Must be skipped
    like a NaN gap, not returned as a real price."""
    h = _history([((2019, 11, 19), 8.513364377904509e-07), ((2019, 11, 20), 25.0)])
    assert h.price_on_or_after(datetime.date(2019, 11, 19)) == 25.0


def test_price_does_not_reject_a_real_penny_stock():
    """A legitimate penny stock (confirmed real: GGSM at $0.0024) must still be returned -
    the garbage-value floor sits far below any real traded price, not just below $1."""
    h = _history([((2024, 1, 2), 0.0024)])
    assert h.price_on_or_after(datetime.date(2024, 1, 2)) == 0.0024


def test_price_all_remaining_days_garbage_returns_none():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), 9.8e-25)])
    assert h.price_on_or_after(datetime.date(2024, 1, 3)) is None


def test_daily_prices_skips_garbage_value():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), 8e-07)])
    assert h.daily_prices() == [(datetime.date(2024, 1, 2), 100.0)]


def test_price_skips_implausibly_huge_garbage_value():
    """Regression: yfinance's own historical data came back as ~2.9e15-3.6e15 for AEXAY
    (a real ~$6 stock) - confirmed live against real-world prices, off by 14+ orders of
    magnitude, not just noisy. Must be skipped like a NaN gap, not returned as a real
    price. NOTE: the ceiling is deliberately set only to catch astronomical-scale garbage
    like this - it does NOT catch SUNE/NVVE/REVB/APVO's $1.9M-$7.7M-range garbage (checked
    directly: all of those are below this ceiling), because any threshold low enough to
    catch them would risk rejecting BRK.A's real, actively-climbing price within a few
    years (see the MAX_PLAUSIBLE_PRICE comment). Those need per-ticker DB cleanup, not a
    threshold."""
    h = _history([((2023, 2, 1), 2.9e15), ((2023, 2, 2), 6.0)])
    assert h.price_on_or_after(datetime.date(2023, 2, 1)) == 6.0


def test_price_does_not_reject_a_real_ultra_high_price_stock():
    """A legitimate ultra-high-price stock (BRK.A, confirmed real up to ~$770k, and still
    growing - the ceiling must stay well clear of its plausible near-future range, not
    just its price today) must still be returned."""
    h = _history([((2024, 1, 2), 620000.0)])
    assert h.price_on_or_after(datetime.date(2024, 1, 2)) == 620000.0


def test_price_does_not_reject_brk_a_at_its_confirmed_all_time_high():
    """Regression: an earlier, lower ceiling (1,000,000) would have been crossed by BRK.A's
    own confirmed real all-time-high ($803,783 as of 2025-05) within a couple of years at
    its ~13%/year trend - the ceiling must clear this with real headroom, not just today's
    price."""
    h = _history([((2025, 5, 2), 803783.0)])
    assert h.price_on_or_after(datetime.date(2025, 5, 2)) == 803783.0


def test_price_all_remaining_days_huge_garbage_returns_none():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), 3.5e15)])
    assert h.price_on_or_after(datetime.date(2024, 1, 3)) is None


def test_daily_prices_skips_huge_garbage_value():
    h = _history([((2024, 1, 2), 100.0), ((2024, 1, 3), 3.5e15)])
    assert h.daily_prices() == [(datetime.date(2024, 1, 2), 100.0)]


def test_fetch_ticker_history_skips_known_bad_ticker_without_calling_yfinance():
    """A KNOWN_BAD_TICKERS entry (e.g. PARA, confirmed live to still return garbage) must
    return None before ever hitting the yfinance API - both to avoid re-storing the same
    garbage on the next scheduled backfill run, and to not waste the API call."""
    with patch("src.market.prices.yf.Ticker") as mock_ticker_cls:
        result = fetch_ticker_history("PARA", datetime.date(2023, 1, 1), datetime.date(2023, 6, 1))
    assert result is None
    mock_ticker_cls.assert_not_called()


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
