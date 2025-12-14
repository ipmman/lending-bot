from ast import List
from typing import Dict, Any, DefaultDict, Union, Optional, Tuple
from collections import defaultdict
from websocket_client import WebSocketClient, MessageDispatcher
from authenticator import Authenticator

import logging
import asyncio
import websockets
import config

import pandas as pd
import numpy as np



class OrderManager:
    def __init__(self, websocket_client: WebSocketClient, authenticator: Authenticator, dispatcher: MessageDispatcher):
        self.websocket_client = websocket_client
        self.authenticator = authenticator
        self.dispatcher = dispatcher
        
        # 使用 pandas DataFrame 取代字典
        self.offers = pd.DataFrame(columns=[
            "OFFER_ID", "SYMBOL", "MTS_CREATED", "MTS_UPDATED", "AMOUNT", "AMOUNT_ORIG",
            "OFFER_TYPE", "_PLACEHOLDER1", "_PLACEHOLDER2", "FLAGS", "OFFER_STATUS",
            "_PLACEHOLDER3", "_PLACEHOLDER4", "_PLACEHOLDER5", "RATE", "PERIOD",
            "NOTIFY", "HIDDEN", "_PLACEHOLDER6", "RENEW", "_PLACEHOLDER7"
        ])
        # 設置單一索引以簡化操作
        self.offers = self.offers.set_index('OFFER_ID')
        
        self.offers_updated = asyncio.Event()  # 新增事件機制
        self.offers_lock = asyncio.Lock()  # 添加鎖機制保護數據存取
        self.response_keys = [
            "OFFER_ID", "SYMBOL", "MTS_CREATED", "MTS_UPDATED", "AMOUNT", "AMOUNT_ORIG",
            "OFFER_TYPE", "_PLACEHOLDER1", "_PLACEHOLDER2", "FLAGS", "OFFER_STATUS",
            "_PLACEHOLDER3", "_PLACEHOLDER4", "_PLACEHOLDER5", "RATE", "PERIOD",
            "NOTIFY", "HIDDEN", "_PLACEHOLDER6", "RENEW", "_PLACEHOLDER7"
        ]


    def _build_offer_message(self, 
                             symbol: Optional[str] = None, 
                             amount: Optional[float] = None, 
                             rate: Optional[float] = None, 
                             period: Optional[int] = None, 
                             offer_id: Optional[int] = None) -> list:
        if offer_id is None:
            # 創建offer的message
            message = [0, "fon", None,
                        {
                            "type": "LIMIT",
                            "symbol": f"f{symbol}",
                            "amount": str(amount),
                            "rate": str(rate),
                            "period": period,
                            "flags": 0
                        }
                    ]
        else:
            # 取消offer的message
            message = [0, "foc", None, {"id": offer_id}]
        
        return message 

    async def new_funding_offer(self, 
                                symbol: str, 
                                amount: float, 
                                rate: float, 
                                period: int,
                                verbose: bool = False):
        """
        新增融資提供(funding offer)
        
        Args:
            symbol (str): 交易幣種
            amount (float): 掛單數量
            rate (float): 日利率
            period (int): 掛單時間
            
        Returns:
            Dict[int, Dict[str, Any]]: 所有最新掛單資訊
        """
        if rate < config.MINIMUM_RATE / 365 / 100:
            raise ValueError(f"利率 {rate*100*365:.2f}% 低於最低利率要求 {config.MINIMUM_RATE:.2f}%")
        
        try:
            self.offers_updated.clear()
            offer_message = self._build_offer_message(symbol, amount, rate, period)
            await self.websocket_client.send(offer_message)
            if verbose:
                logging.info(f"送出新的資金融通訂單請求： {amount} {symbol} @ {rate*100:.4f}% (年化 {rate*365*100:.2f}%)，週期 {period} 天")
            
            # 等待更新，最多等待60秒
            try:
                await asyncio.wait_for(self.offers_updated.wait(), 60.0)  # 等待掛單更新
                self.offers_updated.clear()
            except asyncio.TimeoutError:
                logging.warning("等待掛單更新確認超時")
                raise
        
        except Exception as e:
            logging.error(f"建立新的資金融通訂單時發生錯誤：{e}")
            raise


    async def cancel_funding_offer(self, offer_id: int, verbose: bool = False) -> None:
        """
        取消融資提供(funding offer)
        
        Args:
            offer_id (int): 掛單ID

        Returns:
            Dict[int, Dict[str, Any]]: 所有掛單資訊
        """
        try:
            self.offers_updated.clear()
            message = self._build_offer_message(offer_id=offer_id)
            await self.websocket_client.send(message)

            if verbose:
                logging.info(f"送出取消資金借出掛單請求，訂單ID：{offer_id}")
            
            # 等待更新
            try:
                await asyncio.wait_for(self.offers_updated.wait(), 30.0)
                self.offers_updated.clear()
                
            except asyncio.TimeoutError:
                logging.warning("掛單取消確認超時")
                raise
        
        except Exception as e:
            logging.error(f"取消資金借出掛單時發生錯誤：{e}")
            raise


    async def get_funding_offer(self, wait_for_update: bool = False, timeout: Optional[float] = None) -> Union[Dict[int, Dict[str, Any]], pd.DataFrame]:
        """取得融資提供(funding offer)"""
        if wait_for_update:
            try:
                if timeout is not None:
                    await asyncio.wait_for(self.offers_updated.wait(), timeout=timeout)
                self.offers_updated.clear()
                
            except asyncio.TimeoutError:
                logging.debug(f"返回當前掛單資料，等待更新超時（{timeout}秒）")
        
        async with self.offers_lock:
            return self.offers.copy()


    async def get_active_order_ids(self, symbol: str) -> Tuple[list, list]:
        """獲取當前活躍掛單的所有ID"""
        # 檢查是否為目標幣種的掛單
        symbol = f"f{symbol}"
        async with self.offers_lock:
            # 使用 pandas 查詢指定 symbol 的掛單
            symbol_offers = self.offers[self.offers['SYMBOL'] == symbol]
            
            if symbol_offers.empty:
                return [], []
            
            offer_bids = symbol_offers[(symbol_offers['AMOUNT'] < 0) | (symbol_offers['AMOUNT_ORIG'] < 0)]
            offer_asks = symbol_offers[(symbol_offers['AMOUNT'] > 0) | (symbol_offers['AMOUNT_ORIG'] > 0)]
            
            return offer_bids.index.tolist(), offer_asks.index.tolist()
    
    
    async def get_order_update_time(self, offer_id: int) -> Union[int, None]:
        """獲取最新的掛單更新時間"""
        if offer_id  not in self.offers.index:
            return None
        
        offer_series = self.offers.loc[offer_id]
        mts_updated = offer_series['MTS_UPDATED']
        
        return mts_updated.item() if hasattr(mts_updated, 'item') else mts_updated
    
    
    async def check_order_executed(self, offer_id: int) -> tuple[bool, str | None, dict | None]:
        if offer_id not in self.offers.index:
            return False, None, None
        
        offer_series = self.offers.loc[offer_id]
        status = offer_series['OFFER_STATUS']
        is_executed = 'EXECUTED' in status or 'PARTIALLY FILLED' in status
        if 'EXECUTED' in status:
            await self.remove_offer(offer_series)
        
        return is_executed, status, {'OFFER_ID': offer_id, **offer_series.to_dict()}

    
    async def get_symbol_offers(self, symbol: str, as_dataframe: bool = False) -> Union[Dict, pd.DataFrame]:
        """獲取指定symbol的所有掛單"""
        symbol = f"f{symbol}"
        async with self.offers_lock:
            symbol_offers = self.offers[self.offers['SYMBOL'] == symbol]
            
            if as_dataframe:
                return symbol_offers.copy()
            else:
                # 轉換為字典格式，保持與原始API的兼容性
                return {offer_id: row.to_dict() for offer_id, row in symbol_offers.iterrows()}
            
    
    async def get_symbol_stats(self, symbol: str) -> Dict[str, Any]:
        """獲取指定symbol的掛單統計資訊"""
        symbol = f"f{symbol}"
        async with self.offers_lock:
            symbol_offers = self.offers[self.offers['SYMBOL'] == symbol]
            
            if symbol_offers.empty:
                return {
                    'count': 0,
                    'total_amount': 0.0,
                    'avg_rate': 0.0,
                    'min_rate': 0.0,
                    'max_rate': 0.0,
                    'total_original_amount': 0.0
                }
            
            # 計算統計資訊
            amounts = pd.to_numeric(symbol_offers['AMOUNT'], errors='coerce').fillna(0)
            rates = pd.to_numeric(symbol_offers['RATE'], errors='coerce').fillna(0)
            original_amounts = pd.to_numeric(symbol_offers['AMOUNT_ORIG'], errors='coerce').fillna(0)
            
            return {
                'count': len(symbol_offers),
                'total_amount': float(amounts.sum()),
                'avg_rate': float(rates.mean()) if len(rates) > 0 else 0.0,
                'min_rate': float(rates.min()) if len(rates) > 0 else 0.0,
                'max_rate': float(rates.max()) if len(rates) > 0 else 0.0,
                'total_original_amount': float(original_amounts.sum()),
                'rates_list': rates.tolist(),
                'amounts_list': amounts.tolist()
            }


    async def update_offer(self, offer: pd.Series):
        """安全地更新 offers DataFrame"""
        async with self.offers_lock:
            offer_id = offer["OFFER_ID"]

            self.offers.loc[offer_id] = offer
            self.offers_updated.set()  # 觸發事件通知更新
    
    
    async def remove_offer(self, offer: pd.Series):
        """安全地刪除 offers DataFrame 中的項目"""
        async with self.offers_lock:
            offer_id = offer.name.item() if isinstance(offer.name, np.int64) else offer.name
            if offer_id in self.offers.index:
                self.offers = self.offers.drop(offer_id)
            self.offers_updated.set()  # 觸發事件通知更新
    

    async def _handle_snapshot(self, infos) -> None:
        """處理 snapshot """
        async with self.offers_lock:
            if not infos:
                return

            self.offers = pd.DataFrame(infos, columns=self.response_keys).set_index('OFFER_ID')
            self.offers_updated.set()

    def _parse_single_offer(self, infos) -> pd.Series:
        """解析單個 offer 更新，返回 pandas Series"""
        return pd.Series(infos, index=self.response_keys, name=infos[0])  # name 設為 OFFER_ID
    
    
    async def handle_fo_messages(self, fo_queue, verbose=False):
        """
        背景執行，接收融資提供(funding offer)訊息
        """
        while True:
            # 從專屬隊列獲取消息，而不是直接從WebSocket接收
            data = await fo_queue.get()
            
            try:
                curr_event = data[1]
                infos = data[-1]
                
                if curr_event == "fos":  # 資金提供快照
                    await self._handle_snapshot(infos)
                    if verbose:
                        logging.debug("Received funding offer snapshot")
                
                
                elif curr_event in {"fou", "fon"}:  # funding offer update
                    offer_series = self._parse_single_offer(infos)
                    await self.update_offer(offer_series)
                    status = offer_series['OFFER_STATUS']
                    
                    if verbose:      
                        if curr_event == "fon":
                            logging.info(f"New funding offer created: {offer_series['OFFER_ID']}")
                        else:
                            logging.info(f"Funding offer {status}: {offer_series['OFFER_ID']} | Status: {status}")

                
                elif curr_event == "foc":  # cancel funding offer response
                    offer_series = self._parse_single_offer(infos)
                    status = offer_series['OFFER_STATUS']
                    
                    if 'EXECUTED' in status:
                        if verbose:
                            logging.info(f"Funding offer {status}: {offer_series['OFFER_ID']} | 狀態: {status}")
                        await self.update_offer(offer_series)
                    else:
                        if verbose:
                            logging.info(f"Funding offer canceled: {offer_series['OFFER_ID']} | 狀態: {status}")
                        await self.remove_offer(offer_series)
                
            
            except Exception as e:
                logging.error(f"Error in handle_fo_messages: {e}")
                await asyncio.sleep(5)
            
            finally:
                fo_queue.task_done()
    