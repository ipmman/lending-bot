import asyncio
import logging
import config
import traceback
import os

from datetime import datetime
from websocket_client import WebSocketClient, MessageDispatcher, LOCK_ORDER
from authenticator import Authenticator
from market_info_processor import MarketInfoProcessor
from order_manager import OrderManager
from wallet_manager import WalletManager
from utils.time import get_elapsed_time, format_elapsed_time
from utils.logging_config import setup_logging
from utils.backoff import ExponentialBackoff




# 添加超時常量
RECONNECTION_REQUEST_TIMEOUT = 5.0  # 請求重連時的鎖獲取超時

# 初始化 logging
filename = "main_dev.log" if config.ENVIRONMENT == "development" else "main.log"
setup_logging(log_level=config.LOGGING_LEVEL, 
              log_file=fr"./logs/{filename}")

# 獲取子logger
logger = logging.getLogger(__name__)


"""
TODO:
        1. 背景抓掛單簿 (v)、市場 (v)、錢包資訊 (v)
        2. 主程式(前景)
            - a. FRR掛單, 先嘗試簡單掛單，之後再調整 (v)
            - b. 動態調整；掛單簿幾小時沒掛出重新掛一次 (v)
        3. 成交的掛單狀態，如何抓資訊 (v)
        ---
        
        4. 抓OHLCV資料，`掛單未成交`次數太多時，直接使用此數據掛單，使用最小天數2天 (v)
        5. 階梯式掛單，不要一次性使用全部資金 -- 還沒有這麼多錢 :)
        6. 美化掛單簿時間log存活資訊 (v)
        
        * Google 的 Time Series Foundation Model，預測最佳利率 -- 等待放貸功能完善再執行
"""




async def reconnection_monitor(reconnect_queue, pub_client, auth_client, market_info_processor):
    """背景任務 - 監控並處理重新連線請求，使用隊列機制"""
    
    # 新增：按連接類型的指數退避策略
    reconnect_backoffs = {
        "pub": ExponentialBackoff(base_delay=5, max_delay=120, max_retries=10, jitter=True),
        "auth": ExponentialBackoff(base_delay=5, max_delay=120, max_retries=10, jitter=True)
    }
    
    while True:
        # 改用隊列機制：從隊列獲取重連請求
        client_type = await reconnect_queue.get()
        
        client = pub_client if client_type == "pub" else auth_client
        instance_id = client.instance_id
        backoff = reconnect_backoffs[client_type]
        
        # 檢查是否已有重連任務在進行
        should_attempt_reconnect = False
        existing_task = None
        async with WebSocketClient._reconnect_locks[client_type]:
            if not WebSocketClient._reconnecting.get(client_type, False):
                should_attempt_reconnect = True
            else:
                existing_task = WebSocketClient._reconnection_tasks.get(client_type)
        
        if existing_task and not existing_task.done():
            logger.warning(f"{client_type} 客戶端已有重連任務在進行中，跳過重複請求 (實例ID: {instance_id})")
            continue
        
        if should_attempt_reconnect and client:
            logger.warning(f"執行重新連線請求: {client_type} 客戶端 (實例ID: {instance_id})")
            try:          
                success = await client.ensure_connection(
                    authenticator=client.authenticator if client_type == "auth" else None,
                    auth_filter=client.auth_filter if client_type == "auth" else None,
                    dispatcher=client.dispatcher if client_type == "auth" else None,
                    processor=market_info_processor if client_type == "pub" else None
                )
                
                if success:
                    logger.info(f"{client_type} 重新連線成功 (實例ID: {instance_id})")
                    # 重連成功，重置退避計數器
                    backoff.success()
                    await asyncio.sleep(10.0)  # 成功後短暫等待
                else:
                    logger.error(f"{client_type} 重新連線失敗... (實例ID: {instance_id})")
                    # 重連失敗，使用指數退避策略
                    await backoff.wait(f"{client_type} 重連失敗")
                    
            except Exception as e:
                logger.error(f"{client_type} 重連過程中發生錯誤: {e} (實例ID: {instance_id})")
                logger.error(f"完整錯誤信息: {traceback.format_exc()}")
                # 發生異常，也使用指數退避策略
                await backoff.wait(f"{client_type} 重連異常")
        else:
            logger.debug(f"{client_type} 客戶端重連條件不滿足，跳過此次重連請求 (實例ID: {instance_id})")
            # 條件不滿足時使用較短的等待時間
            await asyncio.sleep(2.0)

            
async def market_info_task(market_info_processor, market_queue):
    """背景任務 - 持續更新市場資訊，不阻塞主流程"""
    logger.info("啟動市場資訊背景任務...")
    
    while True:
        try:
            await market_info_processor.handle_market_messages(market_queue)
                
        except Exception as e:
            logger.error(f"處理市場消息時發生錯誤: {e}")
            logger.error(f"完整錯誤信息: {traceback.format_exc()}")
            await asyncio.sleep(5.0)


# 處理掛單簿的背景任務
async def order_management_task(order_manager, fo_queue):
    """背景任務 - 處理掛單相關的消息"""
    logger.info("啟動掛單管理背景任務...")
    
    while True:
        try:
            await order_manager.handle_fo_messages(fo_queue)
            
        except Exception as e:
            logger.error(f"掛單管理任務出錯: {e}")
            logger.error(f"完整錯誤信息: {traceback.format_exc()}")
            await asyncio.sleep(5.0)
        
        
# 處理餘額的背景任務
async def wallet_manager_task(wallet_manager, wallet_queue):
    """背景任務 - 處理錢包相關的消息"""
    logger.info("啟動錢包管理背景任務...")
    
    while True:
        try:
            await wallet_manager.handle_wallet_messages(wallet_queue)
        
        except Exception as e:
            logger.error(f"錢包管理任務出錯: {e}")
            logger.error(f"完整錯誤信息: {traceback.format_exc()}")
            await asyncio.sleep(5)


# 掛單邏輯
async def place_order(order_manager, 
                      ticker_stats, 
                      funding_balance, 
                      symbol,
                      candles_stats=None,  # 新增 candles 統計數據
                      days=2,
                      book_rate=None,
                      mode="smart"):  # 改為智能模式
    """執行掛單邏輯，根據市場數據計算利率並提交掛單"""
    try:
        min_rate = config.MINIMUM_RATE / 365 / 100  # 轉換為每日利率
        
        if mode == "smart" and candles_stats:
            # 智能策略：結合多種數據源
            desired_rate = calculate_smart_rate(ticker_stats, candles_stats, min_rate)
        
        elif mode == "frr":
            discount = config.FRR_RATE_DISCOUNT
            frr = (ticker_stats.get("frr_yearly", min_rate) / 365 / 100)
            desired_rate = frr * discount  # 打折，為了讓掛單排在前面
        
        elif mode == "active":
            # 主動搶單模式：使用 Book Rate 並降價搶單
            if book_rate:
                rate_type = "Book Rate"
                # 主動搶單：降價確保排在最前面
                discount = config.ACTIVE_RATE_DISCOUNT
                desired_rate = book_rate * discount
                discount_pct = discount * 100  # 99
                logger.debug(f"主動搶單模式：使用 {rate_type} {book_rate*365*100:.4f}% (年) - {discount_pct:.1f} 折")
            else:
                desired_rate = min_rate  # 防禦性設定
        else:
            desired_rate = min_rate
        
        # 對於非主動模式，檢查是否需要調整到 book_rate
        if mode != "active":
            if book_rate and (book_rate * discount) > desired_rate:
                desired_rate = book_rate * discount
            
        desired_rate = max(desired_rate, min_rate)
        
        # 執行掛單
        logger.info(f"開始掛單: {funding_balance:.2f} {symbol} | 年利率: {desired_rate*365*100:.4f}% | 日利率: {desired_rate*100:.6f}% | 掛單期限: {days}天 | 策略: {mode}")

        await order_manager.new_funding_offer(
            symbol,
            funding_balance,
            desired_rate,
            days
        )
        logger.info("**掛單成功**")
        return True
    
    except Exception as e:
        logger.error(f"掛單失敗: {e}")
        logger.error(f"完整錯誤信息: {traceback.format_exc()}")
        return False


def calculate_smart_rate(ticker_stats, candles_stats, min_rate):
    """
    智能利率計算策略
    
    結合：
    1. ticker 數據（即時市場狀況）
    2. candles 數據（歷史趨勢分析）
    3. 風險控制
    """
    
    # 1. 基礎利率來源
    frr_rate = ticker_stats.get("frr_yearly", 0) / 365 / 100  # FRR 利率
    
    # 2. Candles 數據分析
    if candles_stats and candles_stats.get('count', 0) > 0:
        # 使用四分位數來判斷市場趨勢
        q1_close = candles_stats.get('q1_close', 0)  # 25% 分位數
        median_close = candles_stats.get('median_close', 0)  # 50% 分位數  
        q3_close = candles_stats.get('q3_close', 0)  # 75% 分位數
        mean_close = candles_stats.get('mean_close', 0)  # 平均值
        volatility = candles_stats.get('volatility', 0)  # 波動率
        
        # 3. 策略邏輯
        
        # 策略A：保守策略 - 使用中位數附近的值
        # 適合穩定獲利，成交機率較高
        conservative_rate = (median_close + q1_close) / 2
        
        # 策略B：積極策略 - 使用較高分位數
        # 適合獲取更高利潤，但成交風險較大
        aggressive_rate = (median_close + q3_close) / 2
        
        # 策略C：動態調整 - 根據波動率選擇策略
        if volatility > 0.1:  # 高波動市場
            # 高波動時使用保守策略，降低風險
            candles_rate = conservative_rate
            logger.debug(f"高波動市場(波動率: {volatility:.3f})，採用保守策略")
        else:  # 低波動市場
            # 低波動時可以適度積極
            candles_rate = (conservative_rate + aggressive_rate) / 2
            logger.debug(f"低波動市場(波動率: {volatility:.3f})，採用平衡策略")
        
        # 4. 多源數據權重組合
        # FRR 權重 55%，Candles 權重 45%
        final_rate = (frr_rate * 0.55 + candles_rate * 0.45)
        
        logger.debug(f"利率計算明細:")
        logger.debug(f"  FRR年利率: {frr_rate*365*100:.4f}% (權重55%)")
        logger.debug(f"  Candles年利率: {candles_rate*365*100:.4f}% (權重45%)")
        logger.debug(f"  綜合年利率: {final_rate*365*100:.4f}%")
        
    else:
        # 沒有 candles 數據時的備用策略
        final_rate = frr_rate
        logger.debug(f"使用備用策略（無Candles數據）: FRR {frr_rate*365*100:.4f}%")
    
    # 5. 安全檢查
    final_rate = max(final_rate, min_rate)
    
    return final_rate


async def handle_cancel_order_timeout(order_manager, active_id, counter_cancel):
    """
    處理掛單逾時邏輯，回傳 (new_counter_cancel, success or not)
    """
    instance_id = order_manager.websocket_client.instance_id
    try:
        await order_manager.cancel_funding_offer(active_id)
        logger.info(f"掛單未成交時間超過{config.FRR_ORDER_TIMEOUT}分鐘，已取消掛單")
        return 0, True
    
    except Exception as e:
        logger.error(f"取消掛單時發生錯誤，將繼續運行: {e}")
        # 在重連期間不計錯誤次數
        # 避免死鎖，先檢查鎖狀態
        is_reconnecting = False
        async with WebSocketClient._reconnect_locks["auth"]:
            is_reconnecting = WebSocketClient._reconnecting.get("auth", False)

        # 檢查完鎖狀態後再進行處理
        if not is_reconnecting:
            counter_cancel += 1
            # 連續失敗超過 5 次，觸發重新連線
            if counter_cancel > 5:
                logger.error(f"`取消掛單`任務，出錯超過連續5次，請求重新連線... (實例ID: {instance_id})")
                raise
        else:
            logger.warning(f"取消掛單錯誤發生於重新連線期間，本次不計入錯誤次數。 (實例ID: {instance_id})")
            
        return counter_cancel, False  # 跳到下一輪迴圈


async def lending_engine(order_manager, 
                         market_info_processor, 
                         wallet_manager, 
                         reconnect_queue,
                         pub_client, auth_client):
    """進階版本：FRR → Active → 熔斷機制"""
    
    # NOTE: Bitfinex production uses UST for USDT; config.SYMBOL normalizes it.
    symbol = f"TEST{config.SYMBOL}" if config.ENVIRONMENT == "development" else config.SYMBOL
    counter_cancel = 0  # 取消掛單的錯誤計數
    
    # 新增：掛單策略管理變數
    current_mode = "frr"  # 可能值: frr, active, paused
    frr_failure_count = 0  # FRR 掛單失敗次數
    active_attempt_count = 0  # 主動搶單嘗試次數
    circuit_breaker_triggered = False
    circuit_breaker_start_time = None
    
    # 策略參數 - 從配置文件讀取
    FRR_FAILURE_THRESHOLD = config.FRR_FAILURE_THRESHOLD
    ACTIVE_MAX_ATTEMPTS = config.ACTIVE_MAX_ATTEMPTS
    ACTIVE_ORDER_TIMEOUT = config.ACTIVE_ORDER_TIMEOUT
    CIRCUIT_BREAKER_DURATION = config.CIRCUIT_BREAKER_DURATION
    
    # 指數退避策略實例
    error_backoff = ExponentialBackoff(
        base_delay=30, 
        max_delay=300, 
        max_retries=8, 
        jitter=True
    )
    
    while True:
        try:
            logger.debug("-----------------------------------------------------------------")
            logger.debug(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            # 檢查熔斷機制狀態
            if circuit_breaker_triggered:
                if circuit_breaker_start_time:
                    elapsed = (datetime.now() - circuit_breaker_start_time).total_seconds()
                    if elapsed >= CIRCUIT_BREAKER_DURATION:
                        # 熔斷結束，重置所有狀態
                        circuit_breaker_triggered = False
                        circuit_breaker_start_time = None
                        current_mode = "frr"
                        frr_failure_count = 0
                        active_attempt_count = 0
                        logger.info("熔斷機制結束，重置策略狀態，回到 FRR 模式")
                    else:
                        remaining = CIRCUIT_BREAKER_DURATION - elapsed
                        logger.debug(f"熔斷機制進行中，剩餘 {remaining:.1f} 秒")
                        await asyncio.sleep(config.MAIN_LOOP_DELAY)
                        continue
            
            # 確保市場資訊處理器狀態已滿足
            _, ask_ids = await order_manager.get_active_order_ids(symbol)
            
            # 新增標記來追蹤本輪循環的狀態
            any_order_executed = False
            stop_processing_orders = False
            
            if ask_ids:
                for ask_id in ask_ids:
                    # 檢查掛單是否已成交
                    is_executed, execution_status, offer_info = await order_manager.check_order_executed(ask_id)
            
                    if is_executed:
                        # 掛單成交！標記成功，但繼續檢查其他掛單
                        logger.info(f"掛單成交 | 狀態: {execution_status} | ID: {ask_id}")
                        any_order_executed = True
                        continue  # 檢查下一個 ask_id
                
                    # ----- 如果未成交，才繼續處理超時邏輯 -----
                    last_update_time = await order_manager.get_order_update_time(ask_id)
                    if last_update_time:
                        time_diff = get_elapsed_time(last_update_time)
                        logger.debug(f"掛單ID: {ask_id} | 未成交時間: {format_elapsed_time(time_diff)}")
                    else:
                        logger.warning(f"掛單ID: {ask_id} | 未找到更新時間")
                        continue
                    
                    # 根據當前模式設定不同的超時時間
                    if current_mode == "active":
                        timeout_seconds = ACTIVE_ORDER_TIMEOUT
                        timeout_condition = time_diff and time_diff["total_seconds"] > timeout_seconds
                    else:
                        timeout_seconds = 60.0 * config.FRR_ORDER_TIMEOUT
                        timeout_condition = time_diff and time_diff["total_seconds"] > timeout_seconds
                    
                    # 處理掛單超時
                    if timeout_condition:
                        counter_cancel, cancel_success = await handle_cancel_order_timeout(
                            order_manager, ask_id, counter_cancel
                        )
                        
                        if not cancel_success:
                            # 取消掛單失敗(系統錯誤)，應停止本輪訂單處理
                            logger.warning(f"取消掛單 {ask_id} 失敗，暫停本輪訂單處理，等待下一輪主循環。")
                            stop_processing_orders = True
                            break  # 跳出 for 迴圈
                        else:
                            # 訂單因超時被成功取消(策略失敗)，更新失敗計數
                            if current_mode == "frr":
                                frr_failure_count += 1
                            elif current_mode == "active":
                                active_attempt_count += 1
                
                # 在迴圈後檢查旗標
                if stop_processing_orders:
                    await asyncio.sleep(config.MAIN_LOOP_DELAY)
                    continue  # 繼續 while 主循環

                if any_order_executed:
                    logger.info("偵測到掛單成交")
                    frr_failure_count = 0
                    active_attempt_count = 0
                    current_mode = "frr"

            # 模式切換：FRR → Active → 熔斷
            if current_mode == "frr":
                if frr_failure_count >= FRR_FAILURE_THRESHOLD:
                    current_mode = "active"
                    active_attempt_count = 0
                    logger.info(f"FRR 模式失敗 {FRR_FAILURE_THRESHOLD} 次，切換到主動搶單模式")
                    
            elif current_mode == "active":
                if active_attempt_count >= ACTIVE_MAX_ATTEMPTS:
                    # 觸發熔斷機制
                    circuit_breaker_triggered = True
                    circuit_breaker_start_time = datetime.now()
                    logger.warning(f"主動搶單失敗 {ACTIVE_MAX_ATTEMPTS} 次，觸發熔斷機制！暫停 {CIRCUIT_BREAKER_DURATION} 秒")
                    await asyncio.sleep(config.MAIN_LOOP_DELAY)
                    continue

                    
            # 獲取最新餘額
            balances_available = await wallet_manager.get_wallet_balances(symbol, wait_for_update=True, timeout=15.0)
            funding_balance = balances_available.get("funding", 0.0)
            
            if funding_balance is None:
                funding_balance = 0.0
            
            logger.debug(f"當前可用餘額: {funding_balance:.4f} {symbol}")

            # 根據策略模式決定是否需要市場資訊
            ticker_data = None
            ticker_stats = None
            last_update = None
            
            if current_mode == "active":
                # Active 模式只需要 book_rate，直接獲取
                logger.debug("Active 模式：直接獲取掛單簿資訊，跳過市場資訊等待")
                book_data, book_rate = await market_info_processor.get_book_info(timeout=30)
            else:
                # FRR 或其他模式需要完整市場資訊
                ticker_data, ticker_stats = await market_info_processor.get_ticker_info(timeout=config.MARKET_DATA_FETCH_TIMEOUT * 60)
                
                last_update = ticker_data.get("LAST_UPDATE_TIME", None)
                book_data, book_rate = await market_info_processor.get_book_info(timeout=30)
                # _, candles_stats = await market_info_processor.get_candles_info(timeout=30)

                # 檢查數據新鮮度
                if last_update:
                    age = (datetime.now() - last_update).total_seconds()
                    
                    if age > config.MARKET_DATA_CRITICAL_STALE_MINUTES * 60:
                        instance_id = pub_client.instance_id
                        logger.warning(f"市場資訊嚴重過期（{age/60:.1f}分鐘），請求重新連接... (實例ID: {instance_id})")
                        # 通過隊列觸發重連，避免死鎖風險
                        try:
                            async with asyncio.timeout(RECONNECTION_REQUEST_TIMEOUT):
                                async with WebSocketClient._reconnect_locks["pub"]:
                                    if not WebSocketClient._reconnecting["pub"]:
                                        await reconnect_queue.put("pub")
                                        logger.info(f"已觸發 pub client 重連 (實例ID: {instance_id})")
                        except asyncio.TimeoutError:
                            logger.warning(f"觸發重連請求超時，跳過本次請求 (實例ID: {instance_id})")
                        # 繼續當前循環，等待下一輪檢查  
                        continue
                
                    if age <= config.MARKET_INFO_MAX_AGE_MINUTES * 60:
                        logger.debug(f"市場資訊新鮮，數據年齡: {age/60:.1f}分鐘")
                    else:
                        logger.debug(f"市場資訊（數據年齡: {age/60:.1f}分鐘）超過{config.MARKET_INFO_MAX_AGE_MINUTES}分鐘未更新，等待數據更新...")
                        continue
                else:
                    logger.debug("尚未獲取有效的市場資訊，等待下次循環...")
                    continue
            
            # 掛單處理
            if funding_balance and funding_balance >= 150.0:
                if current_mode == "frr":
                    logger.debug(f"當前策略模式: {current_mode.upper()} - 失敗次數: {frr_failure_count}/{FRR_FAILURE_THRESHOLD}")
                elif current_mode == "active":
                    logger.debug(f"當前策略模式: {current_mode.upper()} - 失敗次數: {active_attempt_count}/{ACTIVE_MAX_ATTEMPTS}")
                
                place_success = await place_order(order_manager, ticker_stats, funding_balance, symbol,
                    candles_stats=None, book_rate=book_rate, mode=current_mode)
                
                if not place_success:
                    logger.warning(f"掛單提交失敗，將在下一輪重試。")
                else:
                    logger.info(f"掛單提交成功！等待成交...")
            else:
                logger.debug("餘額不足，最低掛單金額為 150.0 美元或等值的 USDT，等待下一輪...")

            
            # 當前市場資訊（僅在非 active 模式下顯示）
            if ticker_stats:
                logger.debug(f"{'USDT' if symbol == 'UST' else symbol} 市場數據:")
                logger.debug(f"  FRR (Year): {ticker_stats['frr_yearly']:.4f}%")
                logger.debug(f"  Low Rate (Year): {ticker_stats['low_rate_yearly']:.4f}%")
                logger.debug(f"  High Rate (Year): {ticker_stats['high_rate_yearly']:.4f}%")
            else:
                logger.debug(f"{current_mode.upper()} 模式：跳過市場數據顯示")
            
            # success, 重置計數器
            counter_cancel = 0
            error_backoff.success()  # 重置退避計數器
            # 短暫休眠後再次檢查
            await asyncio.sleep(config.MAIN_LOOP_DELAY)
            
        except Exception as e:
            logger.error(f"`lending_engine`出錯: {type(e).__name__}: {e}\n{traceback.format_exc()}")    
            
            # 按鎖順序檢查客戶端，engine 出錯兩個都可能有問題
            reconnection_triggered = False
            for client_type in LOCK_ORDER:  # ["auth", "pub"]
                try:
                    client = auth_client if client_type == "auth" else pub_client
                    instance_id = client.instance_id
                    
                    # 使用超時避免永久等待
                    async with asyncio.timeout(RECONNECTION_REQUEST_TIMEOUT):
                        async with WebSocketClient._reconnect_locks[client_type]:
                            if not WebSocketClient._reconnecting[client_type]:
                                logger.error(f"觸發重新連線: {client_type} 客戶端 (實例ID: {instance_id})")
                                await reconnect_queue.put(client_type)
                                reconnection_triggered = True
                                # 移除 break，讓兩個客戶端都有機會被檢查
                                
                except asyncio.TimeoutError:
                    logger.warning(f"獲取 {client_type} 鎖超時，跳過 (實例ID: {instance_id})")
                    continue
                
                except Exception as lock_error:
                    logger.error(f"檢查 {client_type} 重連狀態時出錯: {lock_error} (實例ID: {instance_id})")
                    continue
            
            if not reconnection_triggered:
                logger.warning("無法觸發任何重連，所有客戶端可能都在重連中")
            
            # 短暫休眠後繼續嘗試
            await asyncio.sleep(config.MAIN_LOOP_DELAY * 2)
            logger.error("`lending_engine`出錯，進入指數退避等待模式...")
            # 使用指數退避策略替代固定等待時間
            await error_backoff.wait("`lending_engine` ")
  

async def reconnection_state_monitor():
    """背景任務 - 定期檢查並重置可能卡住的重連狀態"""
    logger.info("啟動重連狀態監控任務...")
    
    while True:
        try:
            # 每60秒檢查一次
            await asyncio.sleep(60)
            await WebSocketClient.check_stuck_reconnecting()
        except Exception as e:
            # 這個任務沒有對應的 WebSocket 實例，保持使用任務ID
            task_id = id(asyncio.current_task())
            logger.error(f"重連狀態監控任務出錯: {e} (任務ID: {task_id})")
            await asyncio.sleep(60)  # 發生錯誤後等待一段時間再繼續


async def main():
    # 初始化重連狀態，確保啟動時所有重連標誌都是清理的狀態
    task_id = id(asyncio.current_task())
    for conn_type in ["pub", "auth"]:
        async with WebSocketClient._reconnect_locks[conn_type]:
            if WebSocketClient._reconnecting[conn_type]:
                logger.warning(f"啟動時發現 {conn_type} 連接的重連狀態為 True，重置為 False (任務ID: {task_id})")
                WebSocketClient._reconnecting[conn_type] = False
    
    # 建立 WebSocket 客戶端
    auth_ws_client = WebSocketClient(config.AUTH_URI, uri_type="auth")
    public_ws_client = WebSocketClient(config.PUBLIC_URI, uri_type="pub")
    # 改用隊列機制替代事件+字典
    reconnect_queue = asyncio.Queue()
    
    # 創建 MessageDispatcher
    dispatcher = MessageDispatcher(auth_ws_client, public_ws_client)
    
    # 建立其他模組實例
    authenticator = Authenticator(config.API_KEY, config.API_SECRET)
    market_info_processor = MarketInfoProcessor()
    order_manager = OrderManager(auth_ws_client, authenticator, dispatcher)
    wallet_manager = WalletManager(auth_ws_client, authenticator, dispatcher)
    
    # 設置依賴
    auth_ws_client.set_dispatcher(dispatcher)
    public_ws_client.set_processor(market_info_processor)
    market_info_processor.set_reconnect_queue(reconnect_queue)
    
    # 建立並認證連線（僅一次）
    auth_filter = list(config.AUTH_FILTER) if config.AUTH_FILTER else None
    await auth_ws_client.connect(authenticator, auth_filter=auth_filter)

    # 建立公開連線
    await public_ws_client.connect()
    await public_ws_client.subscribe(config.CHANNELS, market_info_processor)  # 傳遞processor實例
    
    # 啟動監聽任務
    market_task = asyncio.create_task(
        market_info_task(market_info_processor, dispatcher.market_queue)
    )
    dispatch_task = asyncio.create_task(
        dispatcher.dispatch_messages()
    )
    order_task = asyncio.create_task(
        order_management_task(order_manager, dispatcher.fo_queue)
    )
    wallet_task = asyncio.create_task(
        wallet_manager_task(wallet_manager, dispatcher.wallet_queue)
    )
    lending_engine_task = asyncio.create_task(
        lending_engine(order_manager, market_info_processor, wallet_manager, 
                    reconnect_queue, 
                    public_ws_client, auth_ws_client)
    )
    reconnect_task = asyncio.create_task(
        reconnection_monitor(reconnect_queue, 
                             public_ws_client, auth_ws_client, 
                             market_info_processor)
    )
    
    # 添加重連狀態監控任務
    reconnect_state_monitor_task = asyncio.create_task(
        reconnection_state_monitor()
    )
    
    # server回傳訊息需要時間，故放此處
    await authenticator.authenticate(auth_ws_client, auth_filter=auth_filter, dispatcher=dispatcher)
   
    tasks = [
        market_task,
        dispatch_task,
        order_task,
        wallet_task,
        lending_engine_task,
        reconnect_task,
        reconnect_state_monitor_task  # 添加到任務列表
    ]
    # 主循環
    try:
        
        await asyncio.gather(*tasks)
        
    except Exception as e:
        logger.error(f"主循環出錯: {e}")
            
    finally:
        # 清理資源
        # 按照依賴關係的反向順序取消任務
        # 任務依賴關係：lending_engine -> order/wallet -> dispatch -> market/reconnect
        cleanup_order = [
            lending_engine_task,           # 最先取消主要業務邏輯
            reconnect_state_monitor_task, # 取消監控重連狀態任務
            reconnect_task,             # 取消重連任務
            order_task,                 # 取消訂單處理
            wallet_task,                # 取消錢包處理
            dispatch_task,              # 取消消息分發
            market_task,                # 最後取消市場數據
        ]
        
        for task in cleanup_order:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.error(f"清理任務出錯: {e}")
        
        await public_ws_client.disconnect()
        await auth_ws_client.disconnect()
    

if __name__ == "__main__":
    config.validate_config()
    asyncio.run(main())