import os
import logging
from typing import Dict, Any

from dotenv import load_dotenv

# 載入 .env 檔案（不會覆蓋已存在的環境變數，Docker 環境變數優先）
load_dotenv()


# ============== 環境設定 ==============
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

# ============== API 認證 ==============
API_KEY: str = os.getenv("BFX_API_KEY", "")
API_SECRET: str = os.getenv("BFX_API_SECRET", "")

def validate_config() -> None:
    """驗證必填設定，啟動時呼叫"""
    if not API_KEY or not API_SECRET:
        raise ValueError("BFX_API_KEY 和 BFX_API_SECRET 必須設定（透過 .env 或環境變數）")

# ============== WebSocket URIs ==============
PUBLIC_URI: str = "wss://api-pub.bitfinex.com/ws/2"
AUTH_URI: str = "wss://api.bitfinex.com/ws/2"

# ============== Channel Filters ==============
AUTH_FILTER = None  # 全部資訊，不過濾；可設 ["funding", "trading"]


# ============== 連線設定 ==============
PING_INTERVAL: int = int(os.getenv("PING_INTERVAL", "20"))
PING_TIMEOUT: int = int(os.getenv("PING_TIMEOUT", "30"))
MAX_CONNECTIONS_PER_MINUTE: int = 20  # Bitfinex API 限制，勿改
MAX_CONNECTIONS_PER_15_SECONDS: int = 5  # Bitfinex API 限制，勿改

# ============== 交易參數 ==============
_USER_SYMBOL: str = os.getenv("SYMBOL", "USDT")
# Bitfinex 正式環境 USDT 叫 UST，內部自動轉換
SYMBOL: str = _USER_SYMBOL if ENVIRONMENT == "development" else _USER_SYMBOL.replace("USDT", "UST")
# 最低年化利率（扣除平台15%手續費前），預設 6% / 0.85 ≈ 7.06%
MINIMUM_RATE: float = float(os.getenv("MINIMUM_RATE", "7.0588"))

# Channel subscriptions
_CHANNEL_SYMBOL: str = SYMBOL.replace("USDT", "UST")

CHANNELS: list[Dict[str, Any]] = [
    {"channel": "ticker", "symbol": f"f{_CHANNEL_SYMBOL}"},
    {"channel": "candles", "key": f"trade:1m:f{_CHANNEL_SYMBOL}:p2"},  # OHLCV (Open, High, Low, Close, Volume)
    {"event": "conf", "flags": 131072},
    {"event": "subscribe", "channel": "book", "symbol": f"f{_CHANNEL_SYMBOL}", "prec": "R0", "freq": "F0", "len": 100}
]

# ============== 時間設定 ==============
MAIN_LOOP_DELAY: float = float(os.getenv("MAIN_LOOP_DELAY", "2.0"))  # 主循環暫停時間(秒)
MARKET_INFO_MAX_AGE_MINUTES: float = float(os.getenv("MARKET_INFO_MAX_AGE_MINUTES", "2.0"))  # 預設2分鐘; 市場資訊過期時間
MARKET_DATA_FETCH_TIMEOUT: float = float(os.getenv("MARKET_DATA_FETCH_TIMEOUT", "1.0"))  # 預設1分鐘; 獲取市場數據的最長等待時間(分鐘)
MARKET_DATA_CRITICAL_STALE_MINUTES: float = float(os.getenv("MARKET_DATA_CRITICAL_STALE_MINUTES", "20.0"))  # 預設20分鐘; 市場數據嚴重過期時間(分鐘)，超過此時間觸發重連
FRR_ORDER_TIMEOUT: float = float(os.getenv("FRR_ORDER_TIMEOUT", "1.0"))  # 預設1分鐘; FRR模式掛單未成交時間(分鐘)，超過此時間則取消掛單

# ============== 主動交易模式設定 ==============
ACTIVE_ORDER_TIMEOUT: float = float(os.getenv("ACTIVE_ORDER_TIMEOUT", "10.0"))  # 主動搶單超時（秒）
CIRCUIT_BREAKER_DURATION: float = float(os.getenv("CIRCUIT_BREAKER_DURATION", "60"))  # 熔斷機制持續時間（秒）
FRR_FAILURE_THRESHOLD: int = int(os.getenv("FRR_FAILURE_THRESHOLD", "3"))  # FRR 模式失敗閾值
ACTIVE_MAX_ATTEMPTS: int = int(os.getenv("ACTIVE_MAX_ATTEMPTS", "6"))  # 主動搶單最大嘗試次數
ACTIVE_RATE_DISCOUNT: float = float(os.getenv("ACTIVE_RATE_DISCOUNT", "0.99"))  # 主動搶單利率折扣（1%）
FRR_RATE_DISCOUNT: float = float(os.getenv("FRR_RATE_DISCOUNT", "0.98"))  # FRR 模式利率折扣（2%）

# ============== Debug 設定 ==============
VERBOSE: bool = os.getenv("VERBOSE", "false").lower() in ("true", "1", "yes")
_log_level_str = os.getenv("LOGGING_LEVEL", "INFO").upper()
LOGGING_LEVEL: int = getattr(logging, _log_level_str, logging.INFO)  # logging.INFO / logging.DEBUG
