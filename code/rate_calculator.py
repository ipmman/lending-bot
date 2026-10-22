import logging

logger = logging.getLogger(__name__)


def calculate_rate(
    mode: str,
    min_rate_yearly: float,
    frr_rate_discount: float,
    active_rate_discount: float,
    ticker_stats: dict | None = None,
    candles_stats: dict | None = None,
    book_rate: float | None = None,
) -> float:
    """
    Calculate desired daily lending rate based on strategy mode and market data.

    Pure function: no config access and no state changes (smart mode logs at debug level).

    Args:
        mode: Strategy mode ("smart", "frr", "active")
        min_rate_yearly: Minimum acceptable yearly rate percentage (e.g. 7.0588)
        frr_rate_discount: Discount factor for FRR mode (e.g. 0.98)
        active_rate_discount: Discount factor for active/book_rate adjustments (e.g. 0.99)
        ticker_stats: Ticker statistics dict with 'frr_yearly' key
        candles_stats: Candles statistics dict (for smart mode)
        book_rate: Current best book rate (daily fraction)

    Returns:
        Daily rate as a fraction (e.g. 0.000274 for ~10% yearly)
    """
    min_rate = min_rate_yearly / 365 / 100

    if mode == "smart" and candles_stats:
        desired_rate = calculate_smart_rate(ticker_stats, candles_stats, min_rate)

    elif mode == "frr":
        frr_yearly = ticker_stats.get("frr_yearly", min_rate_yearly) if ticker_stats else min_rate_yearly
        frr = frr_yearly / 365 / 100
        desired_rate = frr * frr_rate_discount

    elif mode == "active":
        if book_rate:
            desired_rate = book_rate * active_rate_discount
        else:
            desired_rate = min_rate
    else:
        desired_rate = min_rate

    # Outside Active mode, if the book rate after the Active discount beats this mode's target, use it instead
    # (book_rate is already a market price, so the active discount is enough; no need for FRR's deeper discount)
    if mode != "active":
        if book_rate and (book_rate * active_rate_discount) > desired_rate:
            desired_rate = book_rate * active_rate_discount

    return max(desired_rate, min_rate)


def calculate_smart_rate(ticker_stats, candles_stats, min_rate):
    """
    Smart rate calculation combining ticker and candles data.

    Currently not wired into the main FRR/Active flow.
    Will be activated when candles strategy is ready.
    """
    # 1. Base rate from FRR
    frr_rate = ticker_stats.get("frr_yearly", 0) / 365 / 100

    # 2. Candles analysis
    if candles_stats and candles_stats.get('count', 0) > 0:
        q1_close = candles_stats.get('q1_close', 0)
        median_close = candles_stats.get('median_close', 0)
        q3_close = candles_stats.get('q3_close', 0)
        volatility = candles_stats.get('volatility', 0)

        conservative_rate = (median_close + q1_close) / 2
        aggressive_rate = (median_close + q3_close) / 2

        if volatility > 0.1:
            candles_rate = conservative_rate
            logger.debug(f"High-volatility market (volatility: {volatility:.3f}); using conservative strategy")
        else:
            candles_rate = (conservative_rate + aggressive_rate) / 2
            logger.debug(f"Low-volatility market (volatility: {volatility:.3f}); using balanced strategy")

        # Weighted combination: FRR 55%, Candles 45%
        final_rate = (frr_rate * 0.55 + candles_rate * 0.45)

        logger.debug(f"Rate calculation breakdown:")
        logger.debug(f"  FRR annual rate: {frr_rate*365*100:.4f}% (weight 55%)")
        logger.debug(f"  Candles annual rate: {candles_rate*365*100:.4f}% (weight 45%)")
        logger.debug(f"  Combined annual rate: {final_rate*365*100:.4f}%")

    else:
        final_rate = frr_rate
        logger.debug(f"Using fallback strategy (no candles data): FRR {frr_rate*365*100:.4f}%")

    final_rate = max(final_rate, min_rate)
    return final_rate
