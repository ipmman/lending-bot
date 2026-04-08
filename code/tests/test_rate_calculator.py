import pytest
from rate_calculator import calculate_rate

# Test constants matching production defaults
MIN_RATE_YEARLY = 7.0588
FRR_DISCOUNT = 0.98
ACTIVE_DISCOUNT = 0.99


def _min_daily():
    return MIN_RATE_YEARLY / 365 / 100


def _call(mode, ticker_stats=None, book_rate=None, candles_stats=None):
    """Helper to call calculate_rate with default config values."""
    return calculate_rate(
        mode=mode,
        min_rate_yearly=MIN_RATE_YEARLY,
        frr_rate_discount=FRR_DISCOUNT,
        active_rate_discount=ACTIVE_DISCOUNT,
        ticker_stats=ticker_stats,
        candles_stats=candles_stats,
        book_rate=book_rate,
    )


class TestFRRMode:
    def test_basic_frr(self):
        """FRR mode: rate = frr_yearly/365/100 * discount"""
        ticker = {"frr_yearly": 10.0}
        result = _call("frr", ticker_stats=ticker)
        expected = (10.0 / 365 / 100) * FRR_DISCOUNT
        assert result == pytest.approx(expected)

    def test_frr_with_higher_book_rate(self):
        """FRR mode: book_rate * active_discount > frr rate → use book_rate"""
        ticker = {"frr_yearly": 10.0}
        frr_rate = (10.0 / 365 / 100) * FRR_DISCOUNT
        # book_rate that yields higher than frr after active discount
        high_book = frr_rate / ACTIVE_DISCOUNT * 1.5
        result = _call("frr", ticker_stats=ticker, book_rate=high_book)
        assert result == pytest.approx(high_book * ACTIVE_DISCOUNT)

    def test_frr_with_lower_book_rate(self):
        """FRR mode: book_rate * active_discount < frr rate → keep frr"""
        ticker = {"frr_yearly": 10.0}
        low_book = 0.00001  # very low
        result = _call("frr", ticker_stats=ticker, book_rate=low_book)
        expected = (10.0 / 365 / 100) * FRR_DISCOUNT
        assert result == pytest.approx(expected)

    def test_frr_fallback_no_frr_yearly(self):
        """FRR mode: ticker_stats missing frr_yearly → fallback to min_rate, floored"""
        ticker = {}
        result = _call("frr", ticker_stats=ticker)
        # min_rate * discount < min_rate, so floor applies
        assert result == pytest.approx(_min_daily())

    def test_frr_fallback_none_ticker(self):
        """FRR mode: ticker_stats is None → fallback to min_rate, floored"""
        result = _call("frr", ticker_stats=None)
        assert result == pytest.approx(_min_daily())


class TestActiveMode:
    def test_active_with_book_rate(self):
        """Active mode: rate = book_rate * active_discount"""
        book = 0.0005
        result = _call("active", book_rate=book)
        assert result == pytest.approx(book * ACTIVE_DISCOUNT)

    def test_active_no_book_rate(self):
        """Active mode: no book_rate → min_rate"""
        result = _call("active")
        assert result == pytest.approx(_min_daily())

    def test_active_book_rate_below_min(self):
        """Active mode: book_rate * discount < min_rate → floor to min_rate"""
        tiny_book = 0.00001
        result = _call("active", book_rate=tiny_book)
        assert result == pytest.approx(_min_daily())


class TestDefaultMode:
    def test_unknown_mode(self):
        """Unknown mode → min_rate"""
        result = _call("unknown")
        assert result == pytest.approx(_min_daily())

    def test_smart_without_candles(self):
        """Smart mode without candles_stats → falls to else → min_rate"""
        result = _call("smart", ticker_stats={"frr_yearly": 10.0})
        assert result == pytest.approx(_min_daily())

    def test_default_with_higher_book_rate(self):
        """Default/else path: book_rate override still applies"""
        high_book = 0.001
        result = _call("smart", book_rate=high_book)
        assert result == pytest.approx(high_book * ACTIVE_DISCOUNT)


class TestMinRateFloor:
    def test_frr_below_min(self):
        """FRR computed rate below min → floor to min_rate"""
        ticker = {"frr_yearly": 0.01}  # extremely low
        result = _call("frr", ticker_stats=ticker)
        assert result == pytest.approx(_min_daily())
