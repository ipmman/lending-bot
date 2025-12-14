[![lang-en](https://img.shields.io/badge/lang-en-lightgrey.svg)](README.md) [![lang-zh--TW](https://img.shields.io/badge/lang-zh--TW-blue.svg)](#lang-zh-tw)

## Bitfinex Lending Bot

Bitfinex 的自動化放貸（funding）機器人。

- **入口**：`code/main.py`
- **日誌**：預設寫入 `./logs/main.log`

---

## 核心策略邏輯

本機器人採用 **三階段循環策略**，旨在平衡「獲利最大化」與「資金使用率」。

### 1. FRR 模式（保守穩健）
- **預設啟動模式**。
- **利率計算**：以 **FRR (Flash Return Rate) * 0.98** 掛單。
  - 若當下市場掛單簿 (Order Book) 的利率極佳 (Book Rate * 0.98 > FRR * 0.98)，則會自動提升利率以獲取更高收益。
- **超時機制**：掛單後等待 **1 分鐘**。
- **狀態切換**：若掛單 **1 分鐘未成交**（由機器人取消掛單），視為一次失敗。
  - 連續失敗 **3 次** 後，切換至 **Active 模式**。

### 2. Active 模式（積極搶單）
- **觸發條件**：FRR 模式連續失敗，代表市場變動快或 FRR 失真，需要更積極的策略。
- **利率計算**：直接讀取掛單簿最佳利率 (Book Rate)，並以 **Book Rate * 0.99** (降價 1%) 掛出，確保排在隊列最前方。
- **超時機制**：掛單後僅等待 **10 秒**（追求即時成交）。
- **狀態切換**：
  - 若 **成交**：重置回 **FRR 模式**。
  - 若 **未成交**：連續失敗 **6 次** 後，觸發 **熔斷機制**。

### 3. 熔斷機制 (Circuit Breaker)
- **觸發條件**：積極搶單依然無法成交，可能市場極度不穩定或出現異常。
- **動作**：暫停所有掛單動作，**休眠 60 秒**。
- **恢復**：休眠結束後，重置所有計數器，回到 **FRR 模式** 重新開始。

---

## 執行方式（二選一）

請選擇適合您環境的方式啟動：**本機 Python** 或 **Docker 容器**。

### 選項一：本機執行 (Local)

#### 1. 準備環境

- **Python**：建議 3.11

#### 2. 安裝依賴

在專案根目錄：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 如果你偏好 `uv`：先安裝 `uv` 後再用 `uv pip install -r requirements.txt` 也可以。

#### 3. 設定環境變數（必做）

此專案會在啟動時檢查 `BFX_API_KEY` / `BFX_API_SECRET`，缺少會直接噴錯結束。

- 建議：複製模板

```bash
cp .env.example .env
```

- 然後在 `.env` 至少填入：

```bash
BFX_API_KEY=你的key
BFX_API_SECRET=你的secret
```

#### 4. 啟動

程式會自動建立 `logs` 目錄。

```bash
python code/main.py
```

---

### 選項二：Docker 執行

#### 1. 準備 `.env`

複製範例檔並填入您的 API Key：

```bash
cp .env.example .env
```

> Docker 啟動時我們會掛載 `logs` 目錄，若本機該目錄不存在，Docker 通常會自動建立，但權限可能會歸屬於 root。建議可先手動建立以確保權限正確。

#### 2. Build

```bash
docker build -t bfx-lending-bot -f Dockerfile .
```

#### 3. Run

```bash
docker run --rm -it \
  --env-file .env \
  -v "./logs:/workspace/logs" \
  bfx-lending-bot
```

> 容器內預設會跑 `python /workspace/code/main.py`（見 `Dockerfile` 的 `CMD`）。

---

## 環境變數設定 (Configuration)

### 必要設定 (Required)

| 變數名稱 | 說明 |
|---------|------|
| `BFX_API_KEY` | 您的 Bitfinex API Key |
| `BFX_API_SECRET` | 您的 Bitfinex API Secret |

### 基本設定 (Basic)

| 變數名稱 | 預設值 | 說明 |
|---------|--------|------|
| `SYMBOL` | `USDT` | 放貸幣別代號。注意：Bitfinex API 裡 USDT 叫 `UST`，程式會自動做 `USDT` → `UST` 轉換。|

### 進階參數 (Advanced)

> 以下參數皆有預設值，一般情況下無需調整。

#### 連線 (WebSocket)

| 變數名稱 | 預設值 | 說明 |
|---------|--------|------|
| `PING_INTERVAL` | `20` 秒 | Ping 發送頻率。每隔多久檢查一次連線是否正常。調小可更快偵測斷線，但會增加連線流量 |
| `PING_TIMEOUT` | `30` 秒 | 斷線判定時間。送出檢查後若超過此時間無回應，則視為斷線並重連 |

#### 策略與風控

| 變數名稱 | 預設值 | 說明 |
|---------|--------|------|
| `MINIMUM_RATE` | `7.0588` | 最低可接受年化利率 (%)。Bitfinex 收取 15% 手續費，此預設值對應淨年化約 6% |
| `FRR_RATE_DISCOUNT` | `0.98` | FRR 模式利率倍率。0.98 表示以 FRR 的 98% 掛單，提高成交機率 |
| `FRR_ORDER_TIMEOUT` | `1.0` 分鐘 | FRR 模式掛單逾時時間，超時會取消並計為一次失敗 |
| `FRR_FAILURE_THRESHOLD` | `3` 次 | FRR 模式連續失敗達此次數後，切換至 Active 模式 |
| `ACTIVE_RATE_DISCOUNT` | `0.99` | Active 模式利率倍率。0.99 表示降價 1% 搶成交 |
| `ACTIVE_ORDER_TIMEOUT` | `10` 秒 | Active 模式掛單逾時時間 |
| `ACTIVE_MAX_ATTEMPTS` | `6` 次 | Active 模式連續失敗達此次數後，觸發熔斷機制 |
| `CIRCUIT_BREAKER_DURATION` | `60` 秒 | 熔斷暫停時間。冷卻結束後，系統將自動恢復並重置回 FRR 模式 |

#### 市場資料與迴圈

| 變數名稱 | 預設值 | 說明 |
|---------|--------|------|
| `MAIN_LOOP_DELAY` | `2.0` 秒 | 主迴圈間隔。調小更積極，調大更保守 |
| `MARKET_DATA_FETCH_TIMEOUT` | `1.0` 分鐘 | 等待市場資料更新的最長時間 |
| `MARKET_INFO_MAX_AGE_MINUTES` | `2.0` 分鐘 | 市場資料允許的最大滯後時間。若資料過舊（超過此時間），將視為無效並跳過本輪掛單 |
| `MARKET_DATA_CRITICAL_STALE_MINUTES` | `20.0` 分鐘 | 市場資料嚴重過期門檻。超過會觸發重連 |

#### 日誌

| 變數名稱 | 預設值 | 說明 |
|---------|--------|------|
| `LOGGING_LEVEL` | `INFO` | 日誌等級。設為 `DEBUG` 可輸出更多細節 |
| `VERBOSE` | `false` | 額外 debug 輸出開關。通常搭配 `LOGGING_LEVEL=DEBUG` 使用 |

**日誌容量保護**：單檔上限 20MB，最多保留 5 份歷史備份，寫滿後自動刪除最舊的檔案。

---