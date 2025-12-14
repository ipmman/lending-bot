[![lang-en](https://img.shields.io/badge/lang-en-red.svg)](#lang-en) [![lang-zh--TW](https://img.shields.io/badge/lang-zh--TW-lightgrey.svg)](README.zh-TW.md)

## Bitfinex Lending Bot

Automated funding bot for Bitfinex.

- **Entry Point**: `code/main.py`
- **Logs**: Default path `./logs/main.log`

---

## Core Strategy Logic

This bot adopts a **Three-Stage Cycle Strategy**, aiming to balance "Profit Maximization" and "Capital Utilization Rate".

### 1. FRR Mode (Conservative & Robust)
- **Default Startup Mode**.
- **Rate Calculation**: Place orders at **FRR (Flash Return Rate) * 0.98**.
  - If the current market Order Book rate is excellent (Book Rate * 0.98 > FRR * 0.98), it will automatically increase the rate to capture higher yield.
- **Timeout Mechanism**: Wait for **1 minute** after placing the order.
- **State Switch**: If the order is **not filled within 1 minute** (cancelled by the bot), it counts as a failure.
  - After **3 consecutive failures**, switch to **Active Mode**.

### 2. Active Mode (Aggressive Sniping)
- **Trigger Condition**: Continuous failures in FRR Mode, indicating fast market changes or FRR distortion, requiring a more aggressive strategy.
- **Rate Calculation**: Read the best Order Book rate directly and place order at **Book Rate * 0.99** (1% discount) to ensure queue priority.
- **Timeout Mechanism**: Wait for only **10 seconds** (pursuing immediate execution).
- **State Switch**:
  - If **Filled**: Reset to **FRR Mode**.
  - If **Not Filled**: After **6 consecutive failures**, trigger **Circuit Breaker**.

### 3. Circuit Breaker Mechanism
- **Trigger Condition**: Aggressive sniping fails to execute, possibly due to extreme market instability or anomalies.
- **Action**: Pause all lending actions, **sleep for 60 seconds**.
- **Recovery**: After sleep ends, reset all counters and restart from **FRR Mode**.

---

## Execution Methods (Choose One)

Please choose the method that suits your environment: **Local Python** or **Docker Container**.

### Option 1: Local Execution

#### 1. Prepare Environment

- **Python**: Recommended 3.11

#### 2. Install Dependencies

In the project root directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> If you prefer `uv`: Installing `uv` first and then running `uv pip install -r requirements.txt` is also fine.

#### 3. Set Environment Variables (Required)

The project checks for `BFX_API_KEY` / `BFX_API_SECRET` at startup. Missing these will cause an error exit.

- Suggestion: Copy the template

```bash
cp .env.example .env
```

- Then fill in at least the following in `.env`:

```bash
BFX_API_KEY=your_key
BFX_API_SECRET=your_secret
```

#### 4. Start

The program will automatically create the `logs` directory.

```bash
python code/main.py
```

---

### Option 2: Docker Execution

#### 1. Prepare `.env`

Copy the example file and fill in your API Key:

```bash
cp .env.example .env
```

> Docker mounts the `logs` directory at startup. If this directory doesn't exist locally, Docker usually creates it automatically, but permissions might belong to root. It's recommended to create it manually first to ensure correct permissions.

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

> The container runs `python /workspace/code/main.py` by default (see `CMD` in `Dockerfile`).

---

## Configuration

### Required Settings

| Variable Name | Description |
|---------------|-------------|
| `BFX_API_KEY` | Your Bitfinex API Key |
| `BFX_API_SECRET` | Your Bitfinex API Secret |

### Basic Settings

| Variable Name | Default Value | Description |
|---------------|---------------|-------------|
| `SYMBOL` | `USDT` | Funding currency symbol. Note: Bitfinex uses `UST` to represent `USDT` in its API; the bot normalizes `USDT` → `UST`. |

### Advanced Parameters

> The following parameters have default values and usually do not need adjustment.

#### WebSocket Connection

| Variable Name | Default Value | Description |
|---------------|---------------|-------------|
| `PING_INTERVAL` | `20` seconds | Ping frequency. How often to check if the connection is normal. Lower values detect disconnection faster but increase traffic. |
| `PING_TIMEOUT` | `30` seconds | Disconnection timeout. If no response after this time, it is considered disconnected and reconnection attempts begin. |

#### Strategy & Risk Control

| Variable Name | Default Value | Description |
|---------------|---------------|-------------|
| `MINIMUM_RATE` | `7.0588` | Minimum acceptable APR (%). Bitfinex charges 15% fee, so this default corresponds to approx 6% net APR. |
| `FRR_RATE_DISCOUNT` | `0.98` | FRR Mode rate multiplier. 0.98 means placing order at 98% of FRR to increase fill probability. |
| `FRR_ORDER_TIMEOUT` | `1.0` minute | FRR Mode order timeout. Timeout cancels the order and counts as one failure. |
| `FRR_FAILURE_THRESHOLD` | `3` times | Switch to Active Mode after this many consecutive failures in FRR Mode. |
| `ACTIVE_RATE_DISCOUNT` | `0.99` | Active Mode rate multiplier. 0.99 means 1% discount to snatch execution. |
| `ACTIVE_ORDER_TIMEOUT` | `10` seconds | Active Mode order timeout. |
| `ACTIVE_MAX_ATTEMPTS` | `6` times | Trigger Circuit Breaker after this many consecutive failures in Active Mode. |
| `CIRCUIT_BREAKER_DURATION` | `60` seconds | Circuit Breaker pause duration. After cooling down, the system automatically recovers and resets to FRR Mode. |

#### Market Data & Loop

| Variable Name | Default Value | Description |
|---------------|---------------|-------------|
| `MAIN_LOOP_DELAY` | `2.0` seconds | Main loop interval. Smaller is more aggressive, larger is more conservative. |
| `MARKET_DATA_FETCH_TIMEOUT` | `1.0` minute | Maximum wait time for market data update. |
| `MARKET_INFO_MAX_AGE_MINUTES` | `2.0` minutes | Maximum allowed market data staleness. If data is too old (exceeds this time), it is considered invalid and the current round is skipped. |
| `MARKET_DATA_CRITICAL_STALE_MINUTES` | `20.0` minutes | Critical staleness threshold. Exceeding this triggers reconnection. |

#### Logging

| Variable Name | Default Value | Description |
|---------------|---------------|-------------|
| `LOGGING_LEVEL` | `INFO` | Log level. Set to `DEBUG` for more details. |
| `VERBOSE` | `false` | Extra debug output switch. Usually used with `LOGGING_LEVEL=DEBUG`. |

**Log Rotation**: Max single file size 20MB, keep up to 5 historical backups, automatically delete oldest files when full.

---
