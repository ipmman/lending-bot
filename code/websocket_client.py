import asyncio
import websockets
import json
import logging
import time
import math
import config
import random
import uuid
import traceback


from typing import Dict, DefaultDict, Any
from collections import deque
from market_info_processor import MarketInfoProcessor
from utils.backoff import ExponentialBackoff

# 添加鎖順序規範，避免死鎖
LOCK_ORDER = ["auth", "pub"]
RECONNECTION_TIMEOUT = 10.0  # 獲取重連鎖的超時時間
RECONNECTION_WAIT_TIMEOUT = 60.0  # 等待其他重連完成的超時時間

class MessageDispatcher:
    def __init__(self, auth_ws_client, pub_ws_client):
        self.auth_ws_client = auth_ws_client
        self.pub_ws_client = pub_ws_client
        self.fo_queue = asyncio.Queue()      # 用於 handle_fo_messages
        self.wallet_queue = asyncio.Queue()  # 用於 handle_wallet_messages
        self.auth_queue = asyncio.Queue()
        self.market_queue = asyncio.Queue()

    async def dispatch_messages(self):
        async def dispatch_auth_messages():
            while True:
                try:
                    data = await self.auth_ws_client.get_message()
                    if isinstance(data, list) and len(data) >= 3:
                        channel_id, event_type, payload = data[0], data[1], data[2]
                        # 根據消息類型分發
                        if channel_id == 0:  # 認證通道的消息
                            if event_type in {"fon", "foc", "fou", "fos"}:
                                await self.fo_queue.put(data)  # fo 相關消息

                            elif event_type in {"ws", "wu"}:
                                await self.wallet_queue.put(data)  # wallet 相關消息
                            
                            else:
                                await self.auth_queue.put(data)
                except Exception as e:
                    logging.error(f"Error in dispatch_auth_messages: {e}")
                    await asyncio.sleep(1)
                    
        async def dispatch_public_messages():
            """處理公開 WebSocket 消息"""
            while True:
                try:
                    data = await self.pub_ws_client.get_message()
                    # 所有 public 消息都放入 market_queue
                    await self.market_queue.put(data)
                except Exception as e:
                    logging.error(f"Error in dispatch_public_messages: {e}")
                    await asyncio.sleep(1)
        
        # 同時運行兩個分發任務
        await asyncio.gather(
            dispatch_auth_messages(),
            dispatch_public_messages()
        )          
                

class WebSocketClient:
    _reconnecting = {
        "pub": False,
        "auth": False
    }
    # 將單一鎖改為按連接類型分離的鎖字典
    _reconnect_locks = {
        "pub": asyncio.Lock(),
        "auth": asyncio.Lock()
    }
    # 記錄重連開始的時間戳
    _reconnecting_timestamps = {
        "pub": time.monotonic(),
        "auth": time.monotonic()
    }
    # 新增：追蹤重連任務
    _reconnection_tasks = {
        "pub": None,
        "auth": None
    }
    # 重連最大允許時間（秒）
    _MAX_RECONNECT_TIME = 600  # 10分鐘，給重連過程更多時間
    
    # '類級別'的速率限制配置與追蹤
    _rate_limits = {
        "pub": {
            "interval": 60,  # 60秒窗口
            "max_connections": 20,  # 20次連線
            "timestamps": deque(),
            "lock": asyncio.Lock(),
        },
        "auth": {
            "interval": 15,  # 15秒窗口
            "max_connections": 5,
            "timestamps": deque(),
            "lock": asyncio.Lock(),
        },
    }
    
    def __init__(self, uri: str, uri_type: str) -> None:
        """
        Args:
            uri (str): bitfinex api url
            uri_type (str): pub / auth
        """
        self.instance_id = str(uuid.uuid4())[:8]
        self.uri = uri
        self.websocket = None
        self.uri_type = uri_type
        self.message_queue = asyncio.Queue()
        self.receiver_task = None
        self.authenticator = None
        self.auth_filter = None
        self.dispatcher = None
        self.processor = None
        
        # 新增：指數退避策略實例
        self.reconnect_backoff = ExponentialBackoff(
            base_delay=30, 
            max_delay=300, 
            max_retries=10, 
            jitter=True
        )
        
        # 根據 URI 確定速率限制類型
        if uri_type not in WebSocketClient._rate_limits:
            raise ValueError(f"無效的 URI type: {uri_type}")
        
        self.rate_limit_config = WebSocketClient._rate_limits[uri_type]

    
    def set_dispatcher(self, dispatcher: MessageDispatcher):
        self.dispatcher = dispatcher
        return self

    
    def set_processor(self, processor: MarketInfoProcessor):
        self.processor = processor
        return self


    async def start_receiver(self):
        """啟動單個消息接收器將消息放入隊列"""
        if self.receiver_task and not self.receiver_task.done():
            self.receiver_task.cancel()
            try:
                await asyncio.wait_for(self.receiver_task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                pass
        
        self.receiver_task = asyncio.create_task(self._receive_messages())


    async def _receive_messages(self):
        """背景任務: 接收WebSocket消息並放入隊列"""
        while True:
            try:
                if not self.websocket or self.websocket.closed:
                    raise websockets.exceptions.ConnectionClosed(None, None)
                
                raw_message = await self.websocket.recv()
                data = await self.receive(raw_message)
                await self.message_queue.put(data)
                
            except websockets.exceptions.ConnectionClosed:
                logging.error(f"接收訊息時連接已關閉: {self.uri}")
                # 檢查是否已有重連在進行
                reconnecting = False
                async with WebSocketClient._reconnect_locks[self.uri_type]:
                    reconnecting = WebSocketClient._reconnecting[self.uri_type]
                
                if not reconnecting:
                    success = await self.ensure_connection(authenticator=self.authenticator, 
                                                           auth_filter=self.auth_filter,
                                                           dispatcher=self.dispatcher,
                                                           processor=self.processor)
                    
                    # 改進：重連失敗時不應該直接退出，而是進入等待模式
                    if not success:
                        logging.error(f"重連失敗，進入指數退避等待模式 ({self.uri_type})")
                        # 使用指數退避策略實例
                        await self.reconnect_backoff.wait(f"重連 ({self.uri_type}) ")
                        continue  # 繼續嘗試，而不是 break
                    
                    # 重連成功，重置退避計數器
                    self.reconnect_backoff.success()
                    # ensure_connection 成功時會創建新的接收器任務，當前任務應該退出
                    return
                else:
                    logging.info(f"連接已關閉，但重連正在進行中，等待重連完成 ({self.uri_type})")
                    await asyncio.sleep(5)
                    continue
                
            except Exception as e:
                logging.error(f"接收訊息時發生錯誤: {e}")
                await asyncio.sleep(1)  # 短暫延遲後再試
                continue
            
    # 從隊列讀取消息
    async def get_message(self):
        """從消息隊列中獲取一條消息"""
        return await self.message_queue.get()
    
    
    async def _check_rate_limit(self) -> None:
        """使用滑動窗口強制速率限制"""
        async with self.rate_limit_config["lock"]:
            now = time.monotonic()
            timestamps = self.rate_limit_config["timestamps"]
            interval = self.rate_limit_config["interval"]
            max_conn = self.rate_limit_config["max_connections"]

            # 移除過期時間戳
            while timestamps and (now - timestamps[0]) > interval:
                timestamps.popleft()

            # 若超過限制，計算需等待的時間
            if len(timestamps) >= max_conn:
                oldest = timestamps[0]
                wait_time = interval - (now - oldest)
                # 增加30秒緩衝
                wait_time = math.ceil(wait_time * 1000) / 1000 + 30.0 
                logging.warning(
                    f"Rate limit reached. Waiting {wait_time:.2f} seconds."
                )
                await asyncio.sleep(wait_time)  # 等待（單位：秒
                # 更新時間並再次清理過期時間戳
                now = time.monotonic()
                while timestamps and (now - timestamps[0]) > interval:
                    timestamps.popleft()

            # 添加當前時間戳
            timestamps.append(now)
            
            
    async def connect(self, authenticator=None, auth_filter=None, verbose=False) -> None:
        """建立 WebSocket 連接
        
        Args:
            authenticator: 用於認證的認證器
            auth_filter: 認證過濾器
            verbose (bool): 是否輸出連接日誌
            
        Returns:
            bool: 連接是否成功
        """        
        self.authenticator = authenticator
        self.auth_filter = auth_filter
        
        try:
            await self._check_rate_limit()
            self.websocket = await websockets.connect(
                                        self.uri, 
                                        ping_interval=config.PING_INTERVAL,
                                        ping_timeout=config.PING_TIMEOUT)
            # 自動啟動接收任務
            await self.start_receiver()
            if verbose:
                logging.debug(f"Connected to {self.uri}")
            
            return True
            
        except Exception as e:
            logging.error(f"Error connecting to {self.uri}: {e}")
            return False
        
                
    async def disconnect(self, verbose=False) -> None:
        if self.websocket:
            try:
                await self.websocket.close()
                if self.receiver_task and not self.receiver_task.done():
                    self.receiver_task.cancel()
                    try:
                        await asyncio.wait_for(self.receiver_task, timeout=5.0)
                    except asyncio.CancelledError:
                        pass
                    except asyncio.TimeoutError:
                        pass
            except Exception as e:
                logging.error(f"Error disconnecting from {self.uri}: {e}")
                
            finally:
                self.websocket = None
                self.receiver_task = None
                if verbose:
                    logging.debug(f"Disconnected from {self.uri}")

                
    async def reconnect(self, verbose=True) -> bool:
        """重新連接到 WebSocket 服務器
        
        Args:
            verbose (bool): 是否輸出連接日誌
            
        Returns:
            bool: 重連是否成功
        """
        if verbose:
            logging.debug(f"Attempting to reconnect to {self.uri}...")
        
        # 安全關閉當前連接, 取消現有的接收任務
        await self.disconnect(verbose=verbose)
        
        # 使用指數退避策略重試連接
        self.reconnect_backoff.reset()

        while True:
            try:
                await self._check_rate_limit()
                self.websocket = await websockets.connect(
                                    self.uri, 
                                    ping_interval=config.PING_INTERVAL,
                                    ping_timeout=config.PING_TIMEOUT)
                # 自動啟動接收任務
                await self.start_receiver()

                if verbose:
                    logging.debug(f"Successfully reconnected to {self.uri}")
                
                self.reconnect_backoff.success()
                return True
            
            except Exception as e:
                if verbose:
                    logging.error(f"Reconnection failed (實例ID: {self.instance_id}):\n{traceback.format_exc()}")
                
                # wait() 方法會自動處理重試計數和重置邏輯，並記錄相應日誌
                was_reset = await self.reconnect_backoff.wait(f"重新連線 ({self.uri_type}) ")
                
                # 如果計數器被重置了，說明已經達到最大重試次數，退出循環
                if was_reset:
                    break
        if verbose:
            logging.critical(f"Failed to reconnect after {self.reconnect_backoff.max_retries} attempts (實例ID: {self.instance_id})")
        
        return False
    
    
    @classmethod
    async def check_stuck_reconnecting(cls):
        """改進版本：檢查並清理卡住的重連狀態"""
        now = time.monotonic()
        for conn_type in reversed(LOCK_ORDER):  # ["pub", "auth"] 反向清理
            async with cls._reconnect_locks[conn_type]:
                if (cls._reconnecting[conn_type] and 
                    now - cls._reconnecting_timestamps[conn_type] > cls._MAX_RECONNECT_TIME):
                    
                    stuck_duration = now - cls._reconnecting_timestamps[conn_type]
                    logging.warning(f"檢測到 {conn_type} 連接的重連狀態卡住 ({stuck_duration:.1f}秒)，強制重置")
                    
                    # 取消卡住的重連任務
                    stuck_task = cls._reconnection_tasks[conn_type]
                    if stuck_task and not stuck_task.done():
                        logging.warning(f"取消卡住的重連任務 {conn_type}")
                        stuck_task.cancel()
                        try:
                            await asyncio.wait_for(stuck_task, timeout=5.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass
                    
                    # 重置狀態
                    cls._reconnecting[conn_type] = False
                    cls._reconnection_tasks[conn_type] = None
                    cls._reconnecting_timestamps[conn_type] = now
                    
                    logging.info(f"{conn_type} 連接重連狀態已重置，時間戳已更新")
    
    
    async def ensure_connection(self, max_retries=10, authenticator=None, auth_filter=None, dispatcher=None, processor=None) -> bool:
        """改進版本，避免重複重連任務"""
        
        instance_id = self.instance_id
        should_reconnect = False
        
        try:
            async with asyncio.timeout(10.0):
                async with WebSocketClient._reconnect_locks[self.uri_type]:
                    if not WebSocketClient._reconnecting[self.uri_type]:
                        # 取消之前可能存在的重連任務
                        old_task = WebSocketClient._reconnection_tasks[self.uri_type]
                        if old_task and not old_task.done():
                            logging.warning(f"取消之前的重連任務 {self.uri_type}")
                            old_task.cancel()
                            try:
                                await asyncio.wait_for(old_task, timeout=5.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass
                        
                        WebSocketClient._reconnecting[self.uri_type] = True
                        WebSocketClient._reconnecting_timestamps[self.uri_type] = time.monotonic()
                        # 記錄當前重連任務
                        WebSocketClient._reconnection_tasks[self.uri_type] = asyncio.current_task()
                        should_reconnect = True
                        logging.debug(f"重連開始 {self.uri_type} (實例ID: {instance_id})")
                    else:
                        logging.debug(f"檢測到併發重連請求，等待進行中的重連完成 {self.uri_type} (實例ID: {instance_id})")
        except asyncio.TimeoutError:
            logging.error(f"無法獲取重連鎖 {self.uri_type}，操作超時")
            return False
        
        # 處理併發重連
        if not should_reconnect:
            # 等待其他任務完成重連，然後檢查連接狀態
            wait_result = await self._wait_for_reconnection_with_timeout()
            if wait_result:
                # 檢查連接是否真的可用
                if self.websocket and not self.websocket.closed:
                    try:
                        pong = await asyncio.wait_for(self.websocket.ping(), timeout=3)
                        await pong
                        logging.debug(f"等待重連完成，連接可用 {self.uri_type} (實例ID: {instance_id})")
                        return True
                    except Exception:
                        logging.warning(f"等待重連完成，但連接不可用 {self.uri_type} (實例ID: {instance_id})")
                        return False
                else:
                    logging.warning(f"等待重連完成，但連接不存在 {self.uri_type} (實例ID: {instance_id})")
                    return False
            else:
                logging.error(f"等待重連超時 {self.uri_type} (實例ID: {instance_id})")
                return False
        
        try:
            return await self._execute_reconnection(max_retries, instance_id, authenticator, auth_filter, dispatcher, processor)
        finally:
            # 快速清理狀態
            async with WebSocketClient._reconnect_locks[self.uri_type]:
                WebSocketClient._reconnecting[self.uri_type] = False
                WebSocketClient._reconnection_tasks[self.uri_type] = None
                logging.debug(f"重連狀態清理完成 {self.uri_type} (實例ID: {instance_id})")
    
    async def _wait_for_reconnection_with_timeout(self, timeout=RECONNECTION_WAIT_TIMEOUT):
        """新增：等待其他重連完成，帶超時機制"""
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            try:
                async with asyncio.timeout(1.0):  # 每次檢查最多1秒
                    async with WebSocketClient._reconnect_locks[self.uri_type]:
                        if not WebSocketClient._reconnecting[self.uri_type]:
                            return True
            
            except asyncio.TimeoutError:
                logging.warning(f"檢查重連狀態超時，繼續等待 {self.uri_type}")
            await asyncio.sleep(1)
        
        logging.warning(f"等待重連完成超時 {self.uri_type}")
        return False
    
    async def _execute_reconnection(self, max_retries, instance_id, authenticator=None, auth_filter=None, dispatcher=None, processor=None):
        """改進版：執行實際的重連邏輯（不持有鎖）"""
        try:
            # 檢查連接是否仍然活動
            if self.websocket and not self.websocket.closed:
                try:
                    # 發送一個 ping 測試連接
                    pong = await asyncio.wait_for(self.websocket.ping(), timeout=5)
                    await pong
                    
                    # 檢查接收器是否活動
                    if not self.receiver_task or self.receiver_task.done():
                        await self.start_receiver()
                        logging.debug(f"重啟消息接收器 {self.uri_type} (實例ID: {instance_id})")
                    
                    # 連接活動，但可能數據流有問題，繼續執行重新連線邏輯
                    logging.debug(f"連接仍活動，但將重新連線以確保數據流正常 {self.uri_type} (實例ID: {instance_id})")
                    
                except Exception as e:
                    logging.warning(f"連接不活動: {e}. 將重連 {self.uri_type} (實例ID: {instance_id})")

            if not await self.reconnect():
                return False
            
            logging.info(f"重連成功，等待系統穩定 {self.uri_type} (實例ID: {instance_id})")
            await asyncio.sleep(5)
            
            if self.uri_type == "pub" and processor:
                logging.debug(f"重置市場資訊處理器數據 (實例ID: {instance_id})")
                processor.reset_channel_state()
                processor.reset_instance_data()
                
                await self.subscribe(config.CHANNELS, processor)
                logging.debug(f"成功重新訂閱頻道 {self.uri_type} (實例ID: {instance_id})")
                await asyncio.sleep(3)
                
            # 重新認證
            if self.uri_type == "auth" and authenticator:
                try:
                    await authenticator.authenticate(self, 
                                                     auth_filter=auth_filter, 
                                                     dispatcher=dispatcher)
                    logging.debug(f"成功重新認證 {self.uri_type} (實例ID: {instance_id})")
                    return True
                except Exception as e:
                    logging.error(f"重連後認證失敗: {e} {self.uri_type} (實例ID: {instance_id})")
                    return False
            
            return True
            
        except Exception as e:
            logging.error(f"執行重連時發生錯誤: {e} {self.uri_type} (實例ID: {instance_id})")
            return False


    async def send(self, message: Dict[str, str], retries=3) -> None:
        """
        把訊息(Dict)轉換成JSON格式的字串 並發送到WebSocket伺服器
        
        Args:
            message (Dict[str, str])
        """
        for attempt in range(retries):
            try:
                await self.websocket.send(json.dumps(message))
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise Exception(f"Failed to send message after {retries} attempts: {e}")
                await asyncio.sleep(min(30, 2 ** attempt))
    
    
    async def receive(self, message) -> Dict[str, Any]:
        """
        把伺服器回傳的JSON格式字串轉換成Dict
        
        Args:
            message (string)

        Returns:
            Dict[str, Any]
        """
        return json.loads(message)


    async def subscribe(self, channels: list[Dict[str, Any]], processor: MarketInfoProcessor = None) -> None:
        # 推薦方式：使用實例變量
        if processor is None:
            raise Exception("processor is required for subscribe.")
        
        processor.desired_channels = len(channels)
        n_channels = len(processor.enrolled_channels)

        for channel in channels:
            if n_channels >= 25:
                raise Exception(f"Exceeded maximum number of subscriptions per connection ({self.uri_type}).")
            subscribe_message = {
                "event": "subscribe",
                **channel
            }
            await self.send(subscribe_message)
            n_channels += 1
    
    async def unsubscribe(self, channels: DefaultDict[int, Dict[str, Any]]) -> None:
        if not channels:
            return
        
        for channel_id in channels.keys():
            unsubscribe_message = {
                "event": "unsubscribe",
                "chanId":channel_id
            }
            await self.send(unsubscribe_message)
            await asyncio.sleep(0.1) 



if __name__ == "__main__":
    pass
    