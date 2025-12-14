import asyncio
import logging
import websockets
import config

from typing import Dict, Any
from websocket_client import WebSocketClient, MessageDispatcher
from authenticator import Authenticator




class WalletManager:
    def __init__(self, websocket_client: WebSocketClient, authenticator: Authenticator, dispatcher: MessageDispatcher):
        self.websocket_client = websocket_client
        self.authenticator = authenticator
        self.dispatcher = dispatcher
        self.wallets = {}  # 存儲錢包餘額
        self.wallets_updated = asyncio.Event()  # 事件通知錢包更新
        self.wallets_lock = asyncio.Lock()  # 保護數據存取的鎖
        self.update_timer = None  # 定時器
        self.update_delay = 1.5  # wallet_update超時時間(秒)


    async def parse_wallet_snapshot(self, wallets):
        """處理錢包快照消息"""
        async with self.wallets_lock:
            self.wallets.clear()
            for wallet in wallets:
                wallet_type, currency, balance, unsettled_interest, balance_available, _, _ = wallet
                if currency not in self.wallets:
                    self.wallets[currency] = {}
                
                self.wallets[currency][wallet_type] = {
                    "balance": balance,
                    "available": balance_available
                }
            
            logging.debug("錢包快照已更新")


    async def parse_wallet_update(self, wallet_update, verbose=False):
        """處理錢包更新消息"""
        wallet_type, currency, balance, unsettled_interest, balance_available, _, _ = wallet_update
        async with self.wallets_lock:
            if currency not in self.wallets:
                self.wallets[currency] = {}
            self.wallets[currency][wallet_type] = {
                "balance": balance,
                "available": balance_available
            }
            if verbose:
                if balance_available != None:
                    logging.debug(f"{wallet_type.capitalize()} Wallet updated for {currency}: {balance_available} available")

        # 重置計時器
        if self.update_timer:
            self.update_timer.cancel()
            
        self.update_timer = asyncio.get_event_loop().call_later(
            self.update_delay,
            self.set_wallets_updated
        )


    def set_wallets_updated(self):
        """設置 wallets_updated 事件"""
        self.wallets_updated.set()
        # logging.info("所有錢包資訊已更新!")
    
    
    async def get_wallet_balances(self, currency: str, wait_for_update: bool = False, timeout: float = None) -> Dict[str, float]:
        """獲取指定貨幣所有類型的可用餘額，可指定等待超時時間
        
        Args:
            currency: 貨幣代碼
            timeout: 等待更新的超時時間，0表示不等待
        """
        if wait_for_update:
            if timeout != 0:
                # 增加計時器延遲時間，確保有足夠時間等待計時器觸發事件
                timeout += self.update_delay
                try:
                    # 帶超時的等待
                    await asyncio.wait_for(self.wallets_updated.wait(), timeout=timeout)
                    self.wallets_updated.clear()
                except asyncio.TimeoutError:
                    logging.debug(f"返回當前錢包資料，等待更新超時（{timeout}秒）")
                except Exception as e:
                    logging.error(f"錢包更新時發生錯誤: {e}")
                    raise
        else:
            await self.wallets_updated.wait()
            
        self.wallets_updated.clear()
            
        async with self.wallets_lock:
            return {wallet_type: info.get("available", 0.0) for wallet_type, info in self.wallets.get(currency, {}).items()}


    async def handle_wallet_messages(self, wallet_queue):
        """背景任務：持續接收並處理錢包消息"""
        while True:
            # 從專屬隊列獲取消息，而不是直接從WebSocket接收
            data = await wallet_queue.get()
            
            try:
                if isinstance(data, list) and len(data) >= 3:
                    channel_id, event_type, payload = data[0], data[1], data[2]
                    if channel_id == 0:  # 認證通道的消息
                        if event_type == "ws":  # 錢包快照
                            await self.parse_wallet_snapshot(payload)
                        elif event_type == "wu":  # 錢包更新
                            await self.parse_wallet_update(payload, verbose=config.VERBOSE)
                            
            except Exception as e:
                logging.error(f"Error in handle_wallet_messages: {e}")
                if self.update_timer:
                    self.update_timer.cancel()
                await asyncio.sleep(5)  # 出錯後休息5秒再繼續
            
            finally:
                # 重要：標記隊列任務完成
                wallet_queue.task_done()
                