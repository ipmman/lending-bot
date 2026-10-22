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
        
        self.enrolled_channels: DefaultDict[int, Dict[str, Any]] = defaultdict(dict)  # subscribed channels
        self.received_channels = set()  # channel IDs that have received data
        self.desired_channels: int = 0
        
        
    def reset_channel_state(self):
        self.enrolled_channels = defaultdict(dict)
        self.received_channels = set()
        self.desired_channels = 0
    
    
    def reset_instance_data(self):
        """Reset instance data; called on reconnect."""
        self.ticker_data = {}
        self.candles_df = None
        self.book_df = None
        self.ticker_stats = {}
        self.candles_stats = {}
        self.channel_symbol_mapping = {}
        # Clear event flags and wait for fresh data
        self.ticker_data_ready.clear()
        self.candles_data_ready.clear()
        self.book_data_ready.clear()
    
    
    def set_reconnect_queue(self, reconnect_queue):
        self.reconnect_queue = reconnect_queue
    
    
    async def get_ticker_info(self, timeout: float = 600) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Safely get the latest ticker data and statistics.
        
        Args:
            timeout (float, optional): Maximum time to wait for data, in seconds. Defaults to 10 minutes.
                
        Returns:
            Tuple[Dict[str, Any], Dict[str, Any]]: copies of the ticker data and statistics.
            Returns empty dicts if the wait times out and no data is available.
        """
        try:
            # Wait for data with a timeout
            await asyncio.wait_for(self.ticker_data_ready.wait(), timeout=timeout)
            self.ticker_data_ready.clear()

        except asyncio.TimeoutError:
            logging.debug(f"Returning current market data; timed out waiting for an update ({timeout}s)")
        
        except Exception as e:
            logging.error(f"Error fetching ticker data: {e}")
            raise
            
        async with self.market_lock:
             return self.ticker_data.copy(), self.ticker_stats.copy()


    async def get_candles_info(self, timeout: int = 30) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Get the candles DataFrame and statistics."""
        try:
            await asyncio.wait_for(self.candles_data_ready.wait(), timeout=timeout)
            self.candles_data_ready.clear()
            
        except asyncio.TimeoutError:
            logging.warning(f"Returning currently available data; timed out waiting for a candles statistics update ({timeout}s)")
            
        async with self.market_lock:
            return self.candles_df.copy() if self.candles_df is not None else pd.DataFrame(), self.candles_stats.copy()
    
    
    async def enroll_channel_id(self, data: Dict[str, Any]) -> None:
        """
        Register a subscribed channel ID.

        Args:
            data (Dict[str, Any]): subscription response from the server
        """
        chan_id = data.get("chanId")
        channel_type = data.get("channel")
        
        if channel_type == "candles":
            symbol = data.get("key")
        else:
            symbol = data.get("symbol")
            
        
        if chan_id and symbol:
            self.enrolled_channels[chan_id] = data
            self.channel_symbol_mapping[chan_id] = symbol

    
    def calculate_ticker_statistics(self, ticker_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate ticker statistics.
        """
        stats = {}
        
        stats["frr_yearly"] = ticker_data.get("FRR", 0) * 365 * 100
        stats["low_rate_yearly"] = ticker_data.get("LOW", 0) * 365 * 100
        stats["high_rate_yearly"] = ticker_data.get("HIGH", 0) * 365 * 100
        
        return stats
    

    async def parse_ticker_message(self, data: list) -> None:
        """
        Parse a ticker message and update the ticker data.
        
        Args:
            data (list): ticker data list received from the WebSocket
                
        Note:
            Updates the ticker_data dict and sets the ticker_data_ready event.
            Also computes and updates the ticker_stats statistics.
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
            # Signal that data is ready
            self.ticker_data_ready.set()
            
            
    def calculate_candles_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Calculate candles statistics.
        """
        if df.empty:
            return {}
        
        # Use describe() for the basic statistics
        desc = df.describe()
        stats = {
                # Basic statistics taken directly from describe()
                'count': desc.loc['count', 'CLOSE'],
                'mean_close': desc.loc['mean', 'CLOSE'],
                'std_close': desc.loc['std', 'CLOSE'],
                'min_low': desc.loc['min', 'LOW'],
                'max_high': desc.loc['max', 'HIGH'],
                
                # Quartiles - used for risk assessment
                'q1_close': desc.loc['25%', 'CLOSE'],
                'median_close': desc.loc['50%', 'CLOSE'],
                'q3_close': desc.loc['75%', 'CLOSE'],
                
                # Derived metrics
                'price_range': desc.loc['max', 'HIGH'] - desc.loc['min', 'LOW'],
                'volatility': desc.loc['std', 'CLOSE'] / desc.loc['mean', 'CLOSE'],  # coefficient of variation
                
                # Full describe() output for reference
                'full_describe': desc.to_dict()
            }

        return stats
    
    
    async def parse_candles_message(self, data: list) -> None:
        """
        Parse a candles message and update the DataFrame.
        """
        try:
            channel_id = data[0]
            symbol = self.channel_symbol_mapping[channel_id]
            candles_data = data[-1]  # multiple candle data points
            
            # Build the DataFrame
            columns = ['MTS', 'OPEN', 'CLOSE', 'HIGH', 'LOW', 'VOLUME']
            df = pd.DataFrame(candles_data, columns=columns)
            
            # Convert timestamps and set them as the index
            df['timestamp'] = pd.to_datetime(df['MTS'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # Ensure numeric columns are float
            numeric_columns = ['OPEN', 'CLOSE', 'HIGH', 'LOW', 'VOLUME']
            df[numeric_columns] = df[numeric_columns].astype(float)
            
            # Sort by time (newest first)
            df.sort_index(ascending=False, inplace=True)
            
            # Compute statistics
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
            logging.error(f"Error parsing candles data: {e}")
            raise


    async def _calculate_book_checksum(self) -> int:
        """
        Compute the checksum of the local order book per the Bitfinex R0 book checksum rules.
        Docs: https://docs.bitfinex.com/docs/ws-websocket-checksum

        Rules:
        1. Split bids (AMOUNT < 0) and asks (AMOUNT > 0).
        2. Sort bids by RATE descending and asks by RATE ascending.
        3. Take the top 25 bids and top 25 asks after sorting.
        4. Interleave them into one string: "bid_id:bid_amount:ask_id:ask_amount:..."
        5. Compute the CRC32 and return it as a signed 32-bit integer.
        """
        async with self.book_lock:
            if self.book_df is None or self.book_df.empty:
                return 0
            
            # 1. Split bids and asks
            # Bids: bid to borrow, AMOUNT < 0, willing to pay a higher rate -> RATE descending
            # Asks: ask to lend, AMOUNT > 0, willing to accept a lower rate -> RATE ascending
            bids = self.book_df[self.book_df['AMOUNT'] < 0]
            asks = self.book_df[self.book_df['AMOUNT'] > 0]
            
            # 2. Sort (ties on RATE are broken by OFFER_ID ascending)
            sorted_bids = bids.sort_values(by=['RATE', 'OFFER_ID'], ascending=[False, True])
            sorted_asks = asks.sort_values(by=['RATE', 'OFFER_ID'], ascending=[True, True])
            
            # 3. Take the top 25
            top_bids = sorted_bids.head(25)
            top_asks = sorted_asks.head(25)
            
            # 4. Build the checksum string
            # Interleaved as in the docs: [bids[0].id, bids[0].amount, asks[0].id, asks[0].amount, ...]
            chk_data = []
            
            bids_list = list(top_bids.iterrows())
            asks_list = list(top_asks.iterrows())

            for i in range(25):
                # Append the bid at this position, if any
                if i < len(bids_list):
                    bid_id, bid_row = bids_list[i]
                    chk_data.extend([str(bid_id), _format_float(bid_row['AMOUNT'].item()).rstrip('0').rstrip('.')])
                
                # Append the ask at this position, if any
                if i < len(asks_list):
                    ask_id, ask_row = asks_list[i]
                    chk_data.extend([str(ask_id), _format_float(ask_row['AMOUNT'].item()).rstrip('0').rstrip('.')])

            if not chk_data:
                return 0

            chk_str = ":".join(chk_data)
            
            # 5. Compute the CRC32 and convert it to a signed 32-bit integer
            crc = zlib.crc32(chk_str.encode('utf8'))
            signed_crc = ctypes.c_int32(crc).value
            
            return signed_crc


    async def parse_book_message(self, data: list) -> None:
        """
        Parse a book snapshot or update message and update the order book DataFrame.
        
        Args:
            data (list): book data list received from the WebSocket
                        
        Note:
            1. Fields are OFFER_ID, PERIOD, RATE, AMOUNT
            2. AMOUNT > 0 is the lending side (ask); AMOUNT < 0 is the borrowing side (bid)
            3. RATE, Funding rate for the offer; if 0 you have to remove the offer from your book
        """
        try:
            book_payload = data[-1]
            # Determine whether this is a snapshot (list of lists, possibly empty) or a single update (list)
            is_snapshot = isinstance(book_payload, list) and \
                          (not book_payload or isinstance(book_payload[0], list))

            # Acquire the lock once at the outermost level to avoid deadlock
            async with self.book_lock:
                if is_snapshot:
                    self._handle_book_snapshot(book_payload)
                else:
                    self._handle_book_update(book_payload)
                    
            if not self.book_data_ready.is_set():
                self.book_data_ready.set()

        except Exception as e:
            logging.error(f"Error parsing book data: {e} | data: {data}")
            raise


    def _handle_book_snapshot(self, book_payload: list) -> None:
        """Handle an order book snapshot - assumes the lock is held."""
        df = pd.DataFrame(book_payload, columns=self.BOOK_COLUMNS)
        df.set_index('OFFER_ID', inplace=True)
        self.book_df = df


    def _handle_book_update(self, book_payload: list) -> None:
        """Handle a single order book update - assumes the lock is held."""
        if self.book_df is None:
            logging.warning("Book update received before snapshot.")
            return

        update_data = dict(zip(self.BOOK_COLUMNS, book_payload))
        offer_id = update_data['OFFER_ID']
        
        # RATE = 0 means delete
        if update_data['RATE'] == 0:
            self._remove_book_entry(offer_id)
        else:
            self._update_book_entry(offer_id, update_data)


    def _remove_book_entry(self, offer_id: int) -> None:
        """Remove an order book entry - assumes the lock is held."""
        if offer_id in self.book_df.index:
            self.book_df.drop(offer_id, inplace=True)


    def _update_book_entry(self, offer_id: int, update_data: dict) -> None:
        """Update or add an order book entry - assumes the lock is held."""
        self.book_df.loc[offer_id] = [
            update_data['PERIOD'],
            update_data['RATE'],
            update_data['AMOUNT']
        ]
    
    
    def get_best_rate(self, df: pd.DataFrame) -> float:
        """
        Return the lowest ask rate with PERIOD = 2 in the book (the best lending quote).
        """
        if df.empty:
            return 0.0
        
        asks = df[(df['AMOUNT'] > 0) & (df['PERIOD'] == 2)]
        if asks.empty:
            return 0.0
        
        sorted_asks = asks.sort_values(by=['RATE', 'OFFER_ID'], ascending=[True, True])
        rate = sorted_asks["RATE"].iloc[0].item()
        
        return round(rate, 8)
    
    
    async def get_book_info(self, timeout: int = 30) -> Tuple[pd.DataFrame, float]:
        """Get the order book DataFrame and the best ask rate."""
        try:
            await asyncio.wait_for(self.book_data_ready.wait(), timeout=timeout)
            self.book_data_ready.clear()
        except asyncio.TimeoutError:
            logging.warning(f"Returning currently available data; timed out waiting for book data ({timeout}s)")
        
        async with self.book_lock:  # Acquire the lock only once
            book_rate = self.get_best_rate(self.book_df) if self.book_df is not None else 0
            return self.book_df.copy() if self.book_df is not None else pd.DataFrame(), book_rate
    
    
    async def handle_market_messages(self, market_queue) -> None:
        """Handle messages received from the WebSocket."""
        while True:
            data = await market_queue.get()
            
            try:
                if isinstance(data, dict) and data.get("event") == "subscribed":
                    await self.enroll_channel_id(data)

                # Exchange notices: 20051 server restart, 20060 maintenance started, 20061 maintenance ended
                elif isinstance(data, dict) and data.get("event") == "info" and data.get("code"):
                    logging.warning(f"Exchange notice {data.get('code')}: {data.get('msg')}")
                    # 20061 maintenance ended: the docs recommend resubscribing to all channels, done by reconnecting the public connection
                    if data.get("code") == 20061 and self.reconnect_queue:
                        await self.reconnect_queue.put("pub")

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
                                # data[1] == "cs" marks a checksum message
                                if data[1] == "cs":
                                    server_cs = data[2]
                                    client_cs = await self._calculate_book_checksum()
                                    
                                    if client_cs != server_cs:
                                        logging.error(f"Checksum mismatch; data may be corrupted, resync required.")
                                        if self.reconnect_queue:
                                            await self.reconnect_queue.put("pub")
                                            await asyncio.sleep(1.0)
                                    else:
                                        # logging.info(f"Checksum matches; data is in sync.")
                                        pass
                                else:
                                    # Otherwise it is an order book snapshot or update message
                                    await self.parse_book_message(data)
            finally:
                # Mark the queue task as done
                market_queue.task_done()
