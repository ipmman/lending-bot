from typing import Dict, Any, Union, Optional, Tuple
from websocket_client import WebSocketClient, MessageDispatcher
from authenticator import Authenticator

import logging
import asyncio
import config

import pandas as pd
import numpy as np



class OrderManager:
    def __init__(self, websocket_client: WebSocketClient, authenticator: Authenticator, dispatcher: MessageDispatcher):
        self.websocket_client = websocket_client
        self.authenticator = authenticator
        self.dispatcher = dispatcher
        
        # Use a pandas DataFrame instead of a dict
        self.offers = pd.DataFrame(columns=[
            "OFFER_ID", "SYMBOL", "MTS_CREATED", "MTS_UPDATED", "AMOUNT", "AMOUNT_ORIG",
            "OFFER_TYPE", "_PLACEHOLDER1", "_PLACEHOLDER2", "FLAGS", "OFFER_STATUS",
            "_PLACEHOLDER3", "_PLACEHOLDER4", "_PLACEHOLDER5", "RATE", "PERIOD",
            "NOTIFY", "HIDDEN", "_PLACEHOLDER6", "RENEW", "_PLACEHOLDER7"
        ])
        # Use a single index to keep operations simple
        self.offers = self.offers.set_index('OFFER_ID')
        
        self.offers_updated = asyncio.Event()  # Event mechanism
        self.offers_lock = asyncio.Lock()  # Lock to protect data access
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
            # Message to create an offer
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
            # Message to cancel an offer
            message = [0, "foc", None, {"id": offer_id}]
        
        return message 

    async def new_funding_offer(self, 
                                symbol: str, 
                                amount: float, 
                                rate: float, 
                                period: int,
                                verbose: bool = False):
        """
        Create a funding offer.
        
        Args:
            symbol (str): Currency symbol
            amount (float): Offer amount
            rate (float): Daily rate
            period (int): Offer period
        """
        if rate < config.MINIMUM_RATE / 365 / 100:
            raise ValueError(f"Rate {rate*100*365:.2f}% is below the minimum rate of {config.MINIMUM_RATE:.2f}%")
        
        try:
            self.offers_updated.clear()
            offer_message = self._build_offer_message(symbol, amount, rate, period)
            await self.websocket_client.send(offer_message)
            if verbose:
                logging.info(f"Sent new funding offer request: {amount} {symbol} @ {rate*100:.4f}% (annual rate {rate*365*100:.2f}%), period {period} days")
            
            # Wait for the update, up to 60 seconds
            try:
                await asyncio.wait_for(self.offers_updated.wait(), 60.0)  # Wait for the offer update
                self.offers_updated.clear()
            except asyncio.TimeoutError:
                logging.warning("Timed out waiting for offer update confirmation")
                raise
        
        except Exception as e:
            logging.error(f"Error creating new funding offer: {e}")
            raise


    async def cancel_funding_offer(self, offer_id: int, verbose: bool = False) -> None:
        """
        Cancel a funding offer.
        
        Args:
            offer_id (int): Offer ID
        """
        try:
            self.offers_updated.clear()
            message = self._build_offer_message(offer_id=offer_id)
            await self.websocket_client.send(message)

            if verbose:
                logging.info(f"Sent funding offer cancel request, offer ID: {offer_id}")
            
            # Wait for the update
            try:
                await asyncio.wait_for(self.offers_updated.wait(), 30.0)
                self.offers_updated.clear()
                
            except asyncio.TimeoutError:
                logging.warning("Timed out waiting for offer cancellation confirmation")
                raise
        
        except Exception as e:
            logging.error(f"Error canceling funding offer: {e}")
            raise


    async def get_funding_offer(self, wait_for_update: bool = False, timeout: Optional[float] = None) -> pd.DataFrame:
        """Get funding offers."""
        if wait_for_update:
            try:
                if timeout is not None:
                    await asyncio.wait_for(self.offers_updated.wait(), timeout=timeout)
                self.offers_updated.clear()
                
            except asyncio.TimeoutError:
                logging.debug(f"Timed out waiting for update ({timeout}s); returning current offer data")
        
        async with self.offers_lock:
            return self.offers.copy()


    async def get_active_order_ids(self, symbol: str) -> Tuple[list, list]:
        """Get the IDs of all currently active offers."""
        # Check whether offers belong to the target currency
        symbol = f"f{symbol}"
        async with self.offers_lock:
            # Use pandas to select offers for the given symbol
            symbol_offers = self.offers[self.offers['SYMBOL'] == symbol]
            
            if symbol_offers.empty:
                return [], []
            
            offer_bids = symbol_offers[(symbol_offers['AMOUNT'] < 0) | (symbol_offers['AMOUNT_ORIG'] < 0)]
            offer_asks = symbol_offers[(symbol_offers['AMOUNT'] > 0) | (symbol_offers['AMOUNT_ORIG'] > 0)]
            
            return offer_bids.index.tolist(), offer_asks.index.tolist()
    
    
    async def get_order_update_time(self, offer_id: int) -> Union[int, None]:
        """Get the latest update time of an offer."""
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
        """Get all offers for the given symbol."""
        symbol = f"f{symbol}"
        async with self.offers_lock:
            symbol_offers = self.offers[self.offers['SYMBOL'] == symbol]
            
            if as_dataframe:
                return symbol_offers.copy()
            else:
                # Convert to a dict to stay compatible with the original API
                return {offer_id: row.to_dict() for offer_id, row in symbol_offers.iterrows()}
            
    
    async def get_symbol_stats(self, symbol: str) -> Dict[str, Any]:
        """Get offer statistics for the given symbol."""
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
            
            # Compute statistics
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
        """Safely update the offers DataFrame."""
        async with self.offers_lock:
            offer_id = offer["OFFER_ID"]

            self.offers.loc[offer_id] = offer
            self.offers_updated.set()  # Fire the event to signal an update
    
    
    async def remove_offer(self, offer: pd.Series):
        """Safely remove an entry from the offers DataFrame."""
        async with self.offers_lock:
            offer_id = offer.name.item() if isinstance(offer.name, np.int64) else offer.name
            if offer_id in self.offers.index:
                self.offers = self.offers.drop(offer_id)
            self.offers_updated.set()  # Fire the event to signal an update
    

    async def _handle_snapshot(self, infos) -> None:
        """Handle a snapshot."""
        async with self.offers_lock:
            # Apply empty snapshots too: they mean the exchange has no offers left (e.g. filled/canceled while disconnected)
            self.offers = pd.DataFrame(infos, columns=self.response_keys).set_index('OFFER_ID')
            self.offers_updated.set()

    def _parse_single_offer(self, infos) -> pd.Series:
        """Parse a single offer update and return a pandas Series."""
        return pd.Series(infos, index=self.response_keys, name=infos[0])  # name is set to OFFER_ID
    
    
    async def handle_fo_messages(self, fo_queue, verbose=False):
        """
        Background task that receives funding offer messages.
        """
        while True:
            # Get messages from the dedicated queue instead of reading from the WebSocket directly
            data = await fo_queue.get()
            
            try:
                curr_event = data[1]
                infos = data[-1]
                
                if curr_event == "fos":  # funding offer snapshot
                    await self._handle_snapshot(infos)
                    if verbose:
                        logging.debug("Received funding offer snapshot")
                
                
                elif curr_event in {"fou", "fon"}:  # new or updated funding offer
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
                            logging.info(f"Funding offer {status}: {offer_series['OFFER_ID']} | Status: {status}")
                        await self.update_offer(offer_series)
                    else:
                        if verbose:
                            logging.info(f"Funding offer canceled: {offer_series['OFFER_ID']} | Status: {status}")
                        await self.remove_offer(offer_series)
                
            
            except Exception as e:
                logging.error(f"Error in handle_fo_messages: {e}")
                await asyncio.sleep(5)
            
            finally:
                fo_queue.task_done()
    