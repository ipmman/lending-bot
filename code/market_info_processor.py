from typing import Dict, Any, DefaultDict, Tuple, Optional
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from math import log10, floor

import logging
import asyncio
import traceback
import zlib
import ctypes

import pandas as pd



def _format_float(value: float) -> str:
    """
    Format float numbers into a string compatible with the Bitfinex API.
    """

    def _find_exp(number: float) -> int:
        base10 = log10(abs(number))

        return floor(base10)

    if _find_exp(value) >= -6:
        return format(Decimal(repr(value)), "f")

    return str(value).replace("e-0", "e-")


class MarketInfoProcessor:
    BOOK_COLUMNS = ["OFFER_ID", "PERIOD", "RATE", "AMOUNT"]

    def __init__(self):
        self.market_lock = asyncio.Lock()
        self.book_lock = asyncio.Lock()
        self.ticker_data = {}
        self.candles_df: Optional[pd.DataFrame] = None
        self.book_df: Optional[pd.DataFrame] = None
        self.ticker_stats = {}
        self.candles_stats = {}
        self.channel_symbol_mapping = {}
        self.ticker_data_ready = asyncio.Event()
        self.candles_data_ready = asyncio.Event()
        self.book_data_ready = asyncio.Event()
        self.reconnect_queue = None
        
        self.enrolled_channels: DefaultDict[int, Dict[str, Any]] = defaultdict(dict)  # 紀錄訂閱的channels
        self.received_channels = set()  # 追蹤已收到數據的 channel ID
        self.desired_channels: int = 0
        
        
    def reset_channel_state(self):
        self.enrolled_channels = defaultdict(dict)
        self.received_channels = set()
        self.desired_channels = 0
    
    
    def reset_instance_data(self):
        """重置實例數據，在重連時調用"""
        self.ticker_data = {}
        self.candles_df = None
        self.book_df = None
        self.ticker_stats = {}
        self.candles_stats = {}
        self.channel_symbol_mapping = {}
        # 清除事件狀態，等待新數據
        self.ticker_data_ready.clear()
        self.candles_data_ready.clear()
        self.book_data_ready.clear()
    
    
    def set_reconnect_queue(self, reconnect_queue):
        self.reconnect_queue = reconnect_queue
    
    
    async def get_ticker_info(self, timeout: int = 600) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        安全地獲取最新的 ticker 數據、統計數據
        
        Args:
            timeout (float, optional): 等待數據的最大時間（秒），默認值為10分鐘。
                
        Returns:
            Tuple[Dict[str, Any], Dict[str, Any]]: ticker數據副本, 統計數據副本
            如果超時且沒有數據，返回空字典
        """
        try:
            # 使用超時設置等待數據
            await asyncio.wait_for(self.ticker_data_ready.wait(), timeout=timeout)
            self.ticker_data_ready.clear()

        except asyncio.TimeoutError:
            logging.debug(f"返回當前市場數據，等待更新超時（{timeout}秒）")
        
        except Exception as e:
            logging.error(f"獲取ticker數據時發生錯誤: {e}")
            raise
            
        async with self.market_lock:
             return self.ticker_data.copy(), self.ticker_stats.copy()


    async def get_candles_info(self, timeout: int = 30) -> Dict[str, Any]:
        """獲取 candles 統計數據"""
        try:
            await asyncio.wait_for(self.candles_data_ready.wait(), timeout=timeout)
            self.candles_data_ready.clear()
            
        except asyncio.TimeoutError:
            logging.warning(f"返回當前可用數據，等待 candles 統計數據更新超時 ({timeout}秒)")
            
        async with self.market_lock:
            return self.candles_df.copy(), self.candles_stats.copy()
    
    
    async def enroll_channel_id(self, data: Dict[str, Any]) -> None:
        """
        設置已訂閱的channel ID

        Args:
            data (Dict[str, Any]): 伺服器回傳訂閱資料
        """
        chan_id = data.get("chanId")
        channel_type = data.get("channel")
        
        if channel_type == "candles":
            symbol = data.get("key")
        else:
            symbol = data.get("symbol")
            
        
        if chan_id and symbol:
            self.enrolled_channels[chan_id] = data  # 改為實例變量
            self.channel_symbol_mapping[chan_id] = symbol

    
    def calculate_ticker_statistics(self, ticker_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        計算ticker統計資料
        """
        stats = {}
        
        stats["frr_yearly"] = ticker_data.get("FRR", 0) * 365 * 100
        stats["low_rate_yearly"] = ticker_data.get("LOW", 0) * 365 * 100
        stats["high_rate_yearly"] = ticker_data.get("HIGH", 0) * 365 * 100
        
        return stats
    

    async def parse_ticker_message(self, data: list) -> None:
        """
        解析 ticker 消息並更新 ticker 數據
        
        Args:
            data (list): 從 WebSocket 接收的 ticker 數據列表
                
        Note:
            更新 ticker_data 字典並設置 ticker_data_ready 事件
            同時計算並更新 ticker_stats 統計信息
        """
        response_keys = [
                "FRR", "BID", "BID_PERIOD", "BID_SIZE", "ASK", "ASK_PERIOD", "ASK_SIZE",
                "DAILY_CHANGE", "DAILY_CHANGE_RELATIVE", "LAST_PRICE", "VOLUME", "HIGH", "LOW",
                "PLACEHOLDER_1", "PLACEHOLDER_2", "FRR_AMOUNT_AVAILABLE"
            ]
        
        channel_id = data[0]
        symbol = self.channel_symbol_mapping[channel_id]
        new_ticker_data = {
            "CHANNEL_ID": channel_id,
            "SYMBOL": symbol,
            "LAST_UPDATE_TIME": datetime.now()
        }
        for idx, key in enumerate(response_keys):
            new_ticker_data[key] = data[-1][idx]
        
        stats = self.calculate_ticker_statistics(new_ticker_data)

        async with self.market_lock:
            self.ticker_data = new_ticker_data
            self.ticker_stats = stats
            self.received_channels.add(channel_id)  # tag channel ID as received
            # 設置數據已準備好的標誌
            self.ticker_data_ready.set()
            
            
    def calculate_candles_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        計算 candles 統計資料
        """
        if df.empty:
            return {}
        
        # 使用 describe() 獲取基礎統計
        desc = df.describe()
        stats = {
                # 基礎統計直接從 describe() 取得
                'count': desc.loc['count', 'CLOSE'],
                'mean_close': desc.loc['mean', 'CLOSE'],
                'std_close': desc.loc['std', 'CLOSE'],
                'min_low': desc.loc['min', 'LOW'],
                'max_high': desc.loc['max', 'HIGH'],
                
                # 四分位數 - 用於風險評估
                'q1_close': desc.loc['25%', 'CLOSE'],
                'median_close': desc.loc['50%', 'CLOSE'],
                'q3_close': desc.loc['75%', 'CLOSE'],
                
                # 衍生指標
                'price_range': desc.loc['max', 'HIGH'] - desc.loc['min', 'LOW'],
                'volatility': desc.loc['std', 'CLOSE'] / desc.loc['mean', 'CLOSE'],  # 變異係數
                
                # 完整的 describe 結果供參考
                'full_describe': desc.to_dict()
            }

        return stats
    
    
    async def parse_candles_message(self, data: list) -> None:
        """
        解析 candles 消息並更新 DataFrame
        """
        try:
            channel_id = data[0]
            symbol = self.channel_symbol_mapping[channel_id]
            candles_data = data[-1]  # 多個 candles 數據點
            
            # 創建 DataFrame
            columns = ['MTS', 'OPEN', 'CLOSE', 'HIGH', 'LOW', 'VOLUME']
            df = pd.DataFrame(candles_data, columns=columns)
            
            # 轉換時間戳並設為索引
            df['timestamp'] = pd.to_datetime(df['MTS'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # 確保數值列為 float 類型
            numeric_columns = ['OPEN', 'CLOSE', 'HIGH', 'LOW', 'VOLUME']
            df[numeric_columns] = df[numeric_columns].astype(float)
            
            # 按時間排序（最新在前面）
            df.sort_index(ascending=False, inplace=True)
            
            # 計算統計數據
            stats = self.calculate_candles_statistics(df)
            
            async with self.market_lock:
                self.candles_df = df
                self.candles_stats = stats
                self.candles_data = {
                    "CHANNEL_ID": channel_id,
                    "KEY": symbol,
                    "LAST_UPDATE_TIME": datetime.now(),
                    "DATA_POINTS": len(df),
                    "TIME_RANGE": {
                        "START": df.index.min(),
                        "END": df.index.max()
                    }
                }
                
                self.received_channels.add(channel_id)
                self.candles_data_ready.set()
                
        except Exception as e:
            logging.error(f"解析 candles 數據時發生錯誤: {e}")
            raise


    async def _calculate_book_checksum(self) -> int:
        """
        根據 Bitfinex R0 book checksum 規則計算本地訂單簿的校驗和。
        文檔: https://docs.bitfinex.com/docs/ws-websocket-checksum

        規則:
        1. 分離 bids (AMOUNT < 0) 和 asks (AMOUNT > 0)。
        2. Bids 按 RATE 降序排序，Asks 按 RATE 升序排序。
        3. 選取排序後的頭 25 筆 bids 和 25 筆 asks。
        4. 交錯合併成一個字串: "bid_id:bid_amount:ask_id:ask_amount:..."
        5. 計算 CRC32 並返回帶符號的 32 位元整數。
        """
        async with self.book_lock:
            if self.book_df is None or self.book_df.empty:
                return 0
            
            # 1. 分離 bids and asks
            # Bids: 我想借錢 (bid to borrow), AMOUNT < 0, 願意付高利率 -> RATE 降序
            # Asks: 我想放貸 (ask to lend), AMOUNT > 0, 願意收低利率 -> RATE 升序
            bids = self.book_df[self.book_df['AMOUNT'] < 0]
            asks = self.book_df[self.book_df['AMOUNT'] > 0]
            
            # 2. 排序 (對於利率相同的訂單，按 OFFER_ID 升序進行次級排序)
            sorted_bids = bids.sort_values(by=['RATE', 'OFFER_ID'], ascending=[False, True])
            sorted_asks = asks.sort_values(by=['RATE', 'OFFER_ID'], ascending=[True, True])
            
            # 3. 選取頭 25 筆
            top_bids = sorted_bids.head(25)
            top_asks = sorted_asks.head(25)
            
            # 4. 建立校驗和字串
            # 根據文檔 [bids[0].id, bids[0].amount, asks[0].id, asks[0].amount, ...] 的格式交錯排列
            chk_data = []
            
            bids_list = list(top_bids.iterrows())
            asks_list = list(top_asks.iterrows())

            for i in range(25):
                # 如果有 bid 在此位置，則添加
                if i < len(bids_list):
                    bid_id, bid_row = bids_list[i]
                    chk_data.extend([str(bid_id), _format_float(bid_row['AMOUNT'].item()).rstrip('0').rstrip('.')])
                
                # 如果有 ask 在此位置，則添加
                if i < len(asks_list):
                    ask_id, ask_row = asks_list[i]
                    chk_data.extend([str(ask_id), _format_float(ask_row['AMOUNT'].item()).rstrip('0').rstrip('.')])

            if not chk_data:
                return 0

            chk_str = ":".join(chk_data)
            
            # 5. 計算 CRC32 並轉換為有符號的 32 位元整數
            crc = zlib.crc32(chk_str.encode('utf8'))
            signed_crc = ctypes.c_int32(crc).value
            
            return signed_crc


    async def parse_book_message(self, data: list) -> None:
        """
        解析 book 的快照(snapshot)或更新(update)消息，並更新訂單簿 DataFrame。
        
        Args:
            data (list): 從 WebSocket 接收的 book 數據列表
                        
        Note:
            1. 欄位為 OFFER_ID、PERIOD、RATE、AMOUNT
            2. AMOUNT > 0 時為出借(Ask)，AMOUNT < 0 時為借入(Bid)
            3. RATE, Funding rate for the offer; if 0 you have to remove the offer from your book
        """
        try:
            book_payload = data[-1]
            # 判斷是快照 (list of lists) 還是單筆更新 (list)
            is_snapshot = isinstance(book_payload, list) and \
                          isinstance(book_payload[0], list)

            # 統一在最外層獲取鎖，避免 deadlock
            async with self.book_lock:
                if is_snapshot:
                    self._handle_book_snapshot(book_payload)
                else:
                    self._handle_book_update(book_payload)
                    
            if not self.book_data_ready.is_set():
                self.book_data_ready.set()

        except Exception as e:
            logging.error(f"解析 book 數據時發生錯誤: {e} | data: {data}")
            raise


    def _handle_book_snapshot(self, book_payload: list) -> None:
        """處理訂單簿快照 - 假設已獲取鎖"""
        df = pd.DataFrame(book_payload, columns=self.BOOK_COLUMNS)
        df.set_index('OFFER_ID', inplace=True)
        self.book_df = df


    def _handle_book_update(self, book_payload: list) -> None:
        """處理訂單簿單筆更新 - 假設已獲取鎖"""
        if self.book_df is None:
            logging.warning("Book update received before snapshot.")
            return

        update_data = dict(zip(self.BOOK_COLUMNS, book_payload))
        offer_id = update_data['OFFER_ID']
        
        # RATE = 0 表示刪除
        if update_data['RATE'] == 0:
            self._remove_book_entry(offer_id)
        else:
            self._update_book_entry(offer_id, update_data)


    def _remove_book_entry(self, offer_id: int) -> None:
        """移除訂單簿條目 - 假設已獲取鎖"""
        if offer_id in self.book_df.index:
            self.book_df.drop(offer_id, inplace=True)


    def _update_book_entry(self, offer_id: int, update_data: dict) -> None:
        """更新或新增訂單簿條目 - 假設已獲取鎖"""
        self.book_df.loc[offer_id] = [
            update_data['PERIOD'],
            update_data['RATE'],
            update_data['AMOUNT']
        ]
    
    
    def get_best_rate(self, df: pd.DataFrame) -> float:
        """
        快速掛單模式下，回傳 book 裡的最高利率(PERIOD = 2)
        """
        if df.empty:
            return 0.0
        
        asks = df[(df['AMOUNT'] > 0) & (df['PERIOD'] == 2)]
        if asks.empty:
            return 0.0
        
        sorted_asks = asks.sort_values(by=['RATE', 'OFFER_ID'], ascending=[True, True])
        rate = sorted_asks["RATE"].iloc[0].item()
        
        return round(rate, 8)
    
    
    async def get_book_info(self, timeout: int = 30) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """獲取 book 數據和統計"""
        try:
            await asyncio.wait_for(self.book_data_ready.wait(), timeout=timeout)
            self.book_data_ready.clear()
        except asyncio.TimeoutError:
            logging.warning(f"返回當前可用數據，等待 book 數據超時 ({timeout}秒)")
        
        async with self.book_lock:  # 只獲取一次鎖
            book_rate = self.get_best_rate(self.book_df) if self.book_df is not None else 0
            return self.book_df.copy() if self.book_df is not None else pd.DataFrame(), book_rate
    
    
    async def handle_market_messages(self, market_queue) -> None:
        """處理從 WebSocket 接收的消息
        
        Returns:
            bool: 是否繼續處理消息
        """
        while True:
            data = await market_queue.get()
            
            try:
                if isinstance(data, dict) and data.get("event") == "subscribed":
                    await self.enroll_channel_id(data)
                
                elif isinstance(data, list):
                    if "hb" != data[1]:
                        if data[0] in self.enrolled_channels:
                            
                            channel_type = self.enrolled_channels[data[0]]["channel"]
                            
                            if channel_type == "ticker":
                                await self.parse_ticker_message(data)

                            # OCHLV (Open, Close, High, Low, Volume)
                            elif channel_type == "candles":  
                                # await self.parse_candles_message(data)
                                pass
                            
                            elif channel_type == "book":
                                # data[1] 為 "cs" 表示是校驗和(checksum)訊息
                                if data[1] == "cs":
                                    server_cs = data[2]
                                    client_cs = await self._calculate_book_checksum()
                                    
                                    if client_cs != server_cs:
                                        logging.error(f"Checksum 不符，資料可能已出錯，需要重新同步。")
                                        if self.reconnect_queue:
                                            await self.reconnect_queue.put("pub")
                                            await asyncio.sleep(1.0)
                                    else:
                                        # logging.info(f"Checksum 相符，資料同步正確。")
                                        pass
                                else:
                                    # 否則為訂單簿快照或更新訊息
                                    await self.parse_book_message(data)
            finally:
                # 標記隊列任務完成
                market_queue.task_done()
