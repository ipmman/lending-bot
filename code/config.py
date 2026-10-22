import os
import logging
from typing import Dict, Any

from dotenv import load_dotenv

# Load .env (does not override existing environment variables; Docker environment variables take precedence)
load_dotenv()


# ============== Environment ==============
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

# ============== API Authentication ==============
# Production uses BFX_API_KEY/SECRET; development uses the paper-trading account's BFX_TEST_API_KEY/SECRET
_KEY_PREFIX: str = "BFX_TEST_" if ENVIRONMENT == "development" else "BFX_"
API_KEY: str = os.getenv(f"{_KEY_PREFIX}API_KEY", "")
API_SECRET: str = os.getenv(f"{_KEY_PREFIX}API_SECRET", "")

def validate_config() -> None:
    """Validate required settings; called at startup."""
    if not API_KEY or not API_SECRET:
        raise ValueError(f"{_KEY_PREFIX}API_KEY and {_KEY_PREFIX}API_SECRET must be set (via .env or environment variables, ENVIRONMENT={ENVIRONMENT})")

# ============== WebSocket URIs ==============
PUBLIC_URI: str = "wss://api-pub.bitfinex.com/ws/2"
AUTH_URI: str = "wss://api.bitfinex.com/ws/2"

# ============== Channel Filters ==============
AUTH_FILTER = None  # All messages, no filtering; can be set to ["funding", "trading"]


# ============== Connection Settings ==============
PING_INTERVAL: float = float(os.getenv("PING_INTERVAL", "20"))
PING_TIMEOUT: float = float(os.getenv("PING_TIMEOUT", "30"))
MAX_CONNECTIONS_PER_MINUTE: int = 20  # Bitfinex API limit; do not change
MAX_CONNECTIONS_PER_15_SECONDS: int = 5  # Bitfinex API limit; do not change

# ============== Trading Parameters ==============
_USER_SYMBOL: str = os.getenv("SYMBOL", "USDT")
# In Bitfinex production, USDT is named UST; converted automatically
# development deliberately keeps USDT: with the TEST prefix, lending_engine targets fTESTUSDT (the paper pool with liquidity),
# while _CHANNEL_SYMBOL is separately forced to fUST to subscribe to the real market. Splitting offers from market data is intentional, because fTESTUST is a dead pool.
SYMBOL: str = _USER_SYMBOL if ENVIRONMENT == "development" else _USER_SYMBOL.replace("USDT", "UST")
# Minimum annual rate (before the 15% platform fee); default 6% / 0.85 ≈ 7.06%
MINIMUM_RATE: float = float(os.getenv("MINIMUM_RATE", "7.0588"))

# Channel subscriptions
_CHANNEL_SYMBOL: str = SYMBOL.replace("USDT", "UST")

CHANNELS: list[Dict[str, Any]] = [
    {"channel": "ticker", "symbol": f"f{_CHANNEL_SYMBOL}"},
    {"channel": "candles", "key": f"trade:1m:f{_CHANNEL_SYMBOL}:p2"},  # OHLCV (Open, High, Low, Close, Volume)
    {"event": "conf", "flags": 131072},
    {"event": "subscribe", "channel": "book", "symbol": f"f{_CHANNEL_SYMBOL}", "prec": "R0", "freq": "F0", "len": 100}
]

# ============== Timing Settings ==============
MAIN_LOOP_DELAY: float = float(os.getenv("MAIN_LOOP_DELAY", "2.0"))  # Main loop delay (seconds)
MARKET_INFO_MAX_AGE_MINUTES: float = float(os.getenv("MARKET_INFO_MAX_AGE_MINUTES", "2.0"))  # Default 2 min; age at which market data is considered stale
MARKET_DATA_FETCH_TIMEOUT: float = float(os.getenv("MARKET_DATA_FETCH_TIMEOUT", "0.1"))  # Default 0.1 min (6 s); max wait for market data (minutes)
MARKET_DATA_CRITICAL_STALE_MINUTES: float = float(os.getenv("MARKET_DATA_CRITICAL_STALE_MINUTES", "20.0"))  # Default 20 min; age at which market data is critically stale (minutes); exceeding it triggers a reconnect
FRR_ORDER_TIMEOUT: float = float(os.getenv("FRR_ORDER_TIMEOUT", "1.0"))  # Default 1 min; in FRR mode, cancel the offer if still unfilled after this long (minutes)
HEARTBEAT_INTERVAL: float = float(os.getenv("HEARTBEAT_INTERVAL", "60.0"))  # Default 60 s; heartbeat log interval, 0 disables it

# ============== Active Mode Settings ==============
ACTIVE_ORDER_TIMEOUT: float = float(os.getenv("ACTIVE_ORDER_TIMEOUT", "30.0"))  # Active mode timeout (seconds)
CIRCUIT_BREAKER_DURATION: float = float(os.getenv("CIRCUIT_BREAKER_DURATION", "120"))  # Circuit breaker duration (seconds)
FRR_FAILURE_THRESHOLD: int = int(os.getenv("FRR_FAILURE_THRESHOLD", "3"))  # FRR mode failure threshold
ACTIVE_MAX_ATTEMPTS: int = int(os.getenv("ACTIVE_MAX_ATTEMPTS", "6"))  # Active mode max attempts
ACTIVE_RATE_DISCOUNT: float = float(os.getenv("ACTIVE_RATE_DISCOUNT", "0.99"))  # Active mode rate discount (1%)
FRR_RATE_DISCOUNT: float = float(os.getenv("FRR_RATE_DISCOUNT", "0.98"))  # FRR mode rate discount (2%)

# ============== Debug Settings ==============
VERBOSE: bool = os.getenv("VERBOSE", "false").lower() in ("true", "1", "yes")
_log_level_str = os.getenv("LOGGING_LEVEL", "INFO").upper()
LOGGING_LEVEL: int = getattr(logging, _log_level_str, logging.INFO)  # logging.INFO / logging.DEBUG
