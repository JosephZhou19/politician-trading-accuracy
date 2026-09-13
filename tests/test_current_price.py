from unittest.mock import Mock, patch

import requests

from src.market.current_price import fetch_current_price, fetch_current_prices


def _mock_response(c):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value={"c": c})
    return resp


def test_fetch_current_price_returns_real_price():
    with patch("src.market.current_price.requests.get", return_value=_mock_response(150.25)):
        assert fetch_current_price("AAPL", api_key="test") == 150.25


def test_fetch_current_price_returns_none_for_zero():
    """Finnhub's shape for a dead/unknown ticker - must never be treated as a real price."""
    with patch("src.market.current_price.requests.get", return_value=_mock_response(0)):
        assert fetch_current_price("DEADCO", api_key="test") is None


def test_fetch_current_prices_skips_a_failed_request_without_treating_it_as_zero():
    """Regression: a request failure (rate limit, network blip) must never be yielded as if
    it were a genuine c=0 response - that would corrupt the zero_streak delisting counter
    with transient errors instead of confirmed no-data responses."""
    responses = [requests.RequestException("boom"), _mock_response(99.0)]

    def fake_get(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("src.market.current_price.requests.get", side_effect=fake_get), \
            patch("src.market.current_price.time.sleep"):
        results = list(fetch_current_prices(["BADCALL", "AAPL"], api_key="test"))

    assert results == [("AAPL", 99.0)]
