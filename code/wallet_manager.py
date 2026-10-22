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
        self.wallets = {}  # Wallet balances
        self.wallets_updated = asyncio.Event()  # Event signaling wallet updates
        self.wallets_lock = asyncio.Lock()  # Lock protecting data access
        self.update_timer = None  # Timer
        self.update_delay = 1.5  # wallet_update timeout (seconds)


    async def parse_wallet_snapshot(self, wallets):
        """Handle a wallet snapshot message."""
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
            
            logging.debug("Wallet snapshot updated")


    async def parse_wallet_update(self, wallet_update, verbose=False):
        """Handle a wallet update message."""
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

        # Reset the timer
        if self.update_timer:
            self.update_timer.cancel()
            
        self.update_timer = asyncio.get_event_loop().call_later(
            self.update_delay,
            self.set_wallets_updated
        )


    def set_wallets_updated(self):
        """Set the wallets_updated event."""
        self.wallets_updated.set()
        # logging.info("All wallet info updated!")
    
    
    async def get_wallet_balances(self, currency: str, wait_for_update: bool = False, timeout: float = None) -> Dict[str, float]:
        """Get the available balance of every wallet type for a currency, with an optional wait timeout.
        
        Args:
            currency: Currency code
            timeout: Timeout for waiting on an update; 0 means do not wait
        """
        if wait_for_update:
            if timeout != 0:
                # Add the timer delay so there is enough time for the timer to fire the event
                timeout += self.update_delay
                try:
                    # Wait with a timeout
                    await asyncio.wait_for(self.wallets_updated.wait(), timeout=timeout)
                    self.wallets_updated.clear()
                except asyncio.TimeoutError:
                    logging.debug(f"Timed out waiting for update ({timeout}s); returning current wallet data")
                except Exception as e:
                    logging.error(f"Error during wallet update: {e}")
                    raise
        else:
            await self.wallets_updated.wait()
            
        self.wallets_updated.clear()
            
        async with self.wallets_lock:
            return {wallet_type: info.get("available", 0.0) for wallet_type, info in self.wallets.get(currency, {}).items()}


    async def handle_wallet_messages(self, wallet_queue):
        """Background task: continuously receive and handle wallet messages."""
        while True:
            # Get messages from the dedicated queue instead of reading from the WebSocket directly
            data = await wallet_queue.get()
            
            try:
                if isinstance(data, list) and len(data) >= 3:
                    channel_id, event_type, payload = data[0], data[1], data[2]
                    if channel_id == 0:  # Authenticated channel message
                        if event_type == "ws":  # Wallet snapshot
                            await self.parse_wallet_snapshot(payload)
                        elif event_type == "wu":  # Wallet update
                            await self.parse_wallet_update(payload, verbose=config.VERBOSE)
                            
            except Exception as e:
                logging.error(f"Error in handle_wallet_messages: {e}")
                if self.update_timer:
                    self.update_timer.cancel()
                await asyncio.sleep(5)  # Wait 5 seconds after an error, then continue
            
            finally:
                # Important: mark the queue task as done
                wallet_queue.task_done()
                