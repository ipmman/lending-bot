import asyncio
import websockets
import json
import logging
import time
import math
import config
import uuid
import traceback


from typing import Dict, DefaultDict, Any
from collections import deque
from market_info_processor import MarketInfoProcessor
from utils.backoff import ExponentialBackoff

# Lock acquisition order, to avoid deadlocks
LOCK_ORDER = ["auth", "pub"]
RECONNECTION_TIMEOUT = 10.0  # Timeout for acquiring the reconnect lock
RECONNECTION_WAIT_TIMEOUT = 60.0  # Timeout for waiting on another reconnect to finish

class MessageDispatcher:
    def __init__(self, auth_ws_client, pub_ws_client):
        self.auth_ws_client = auth_ws_client
        self.pub_ws_client = pub_ws_client
        self.fo_queue = asyncio.Queue()      # For handle_fo_messages
        self.wallet_queue = asyncio.Queue()  # For handle_wallet_messages
        self.auth_queue = asyncio.Queue()
        self.market_queue = asyncio.Queue()

    async def dispatch_messages(self):
        async def dispatch_auth_messages():
            while True:
                try:
                    data = await self.auth_ws_client.get_message()
                    # Auth responses are dicts: {"event": "auth", "status": "OK" | "FAILED", ...}
                    if isinstance(data, dict) and data.get("event") == "auth":
                        await self.auth_queue.put(data)

                    elif isinstance(data, list) and len(data) >= 3:
                        channel_id, event_type, payload = data[0], data[1], data[2]
                        # Dispatch by message type
                        if channel_id == 0:  # Messages on the authenticated channel
                            if event_type in {"fon", "foc", "fou", "fos"}:
                                await self.fo_queue.put(data)  # Funding offer messages

                            elif event_type in {"ws", "wu"}:
                                await self.wallet_queue.put(data)  # Wallet messages

                            # Notification format: [MTS, TYPE, MESSAGE_ID, null, NOTIFY_INFO, CODE, STATUS, TEXT]
                            elif event_type == "n" and len(payload) > 7 and payload[6] in {"ERROR", "FAILURE"}:
                                info = payload[4]
                                offer_id = info[0] if isinstance(info, list) and info else None
                                logging.warning(f"Exchange rejected {payload[1]} (ID: {offer_id}): {payload[7] or 'no reason given'}")
                except Exception as e:
                    logging.error(f"Error in dispatch_auth_messages: {e}")
                    await asyncio.sleep(1)
                    
        async def dispatch_public_messages():
            """Handle public WebSocket messages."""
            while True:
                try:
                    data = await self.pub_ws_client.get_message()
                    # All public messages go into market_queue
                    await self.market_queue.put(data)
                except Exception as e:
                    logging.error(f"Error in dispatch_public_messages: {e}")
                    await asyncio.sleep(1)
        
        # Run both dispatch tasks concurrently
        await asyncio.gather(
            dispatch_auth_messages(),
            dispatch_public_messages()
        )          
                

class WebSocketClient:
    _reconnecting = {
        "pub": False,
        "auth": False
    }
    # One lock per connection type instead of a single shared lock
    _reconnect_locks = {
        "pub": asyncio.Lock(),
        "auth": asyncio.Lock()
    }
    # When each reconnect started
    _reconnecting_timestamps = {
        "pub": time.monotonic(),
        "auth": time.monotonic()
    }
    # Track reconnect tasks
    _reconnection_tasks = {
        "pub": None,
        "auth": None
    }
    # Maximum time allowed for a reconnect (seconds)
    _MAX_RECONNECT_TIME = 600  # 10 minutes, to give reconnects more time
    
    # Class-level rate limit config and tracking
    _rate_limits = {
        "pub": {
            "interval": 60,  # 60-second window
            "max_connections": config.MAX_CONNECTIONS_PER_MINUTE,
            "timestamps": deque(),
            "lock": asyncio.Lock(),
        },
        "auth": {
            "interval": 15,  # 15-second window
            "max_connections": config.MAX_CONNECTIONS_PER_15_SECONDS,
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
        
        # Exponential backoff instance
        self.reconnect_backoff = ExponentialBackoff(
            base_delay=30, 
            max_delay=300, 
            max_retries=10, 
            jitter=True
        )
        
        # Pick the rate limit type from the URI type
        if uri_type not in WebSocketClient._rate_limits:
            raise ValueError(f"Invalid URI type: {uri_type}")
        
        self.rate_limit_config = WebSocketClient._rate_limits[uri_type]

    
    def set_dispatcher(self, dispatcher: MessageDispatcher):
        self.dispatcher = dispatcher
        return self

    
    def set_processor(self, processor: MarketInfoProcessor):
        self.processor = processor
        return self


    async def start_receiver(self):
        """Start a single receiver that puts incoming messages on the queue."""
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
        """Background task: receive WebSocket messages and put them on the queue."""
        while True:
            try:
                if not self.websocket or self.websocket.closed:
                    raise websockets.exceptions.ConnectionClosed(None, None)
                
                raw_message = await self.websocket.recv()
                data = await self.receive(raw_message)
                await self.message_queue.put(data)
                
            except websockets.exceptions.ConnectionClosed:
                logging.error(f"Connection closed while receiving messages: {self.uri}")
                # Check whether a reconnect is already in progress
                reconnecting = False
                async with WebSocketClient._reconnect_locks[self.uri_type]:
                    reconnecting = WebSocketClient._reconnecting[self.uri_type]
                
                if not reconnecting:
                    success = await self.ensure_connection(authenticator=self.authenticator, 
                                                           auth_filter=self.auth_filter,
                                                           dispatcher=self.dispatcher,
                                                           processor=self.processor)
                    
                    # A failed reconnect shouldn't exit; enter wait mode instead
                    if not success:
                        logging.error(f"Reconnect failed, entering exponential backoff ({self.uri_type})")
                        # Use the exponential backoff instance
                        await self.reconnect_backoff.wait(f"Reconnect ({self.uri_type}) ")
                        continue  # Keep retrying instead of breaking out
                    
                    # Reconnected; reset the backoff counter
                    self.reconnect_backoff.success()
                    # On success, ensure_connection starts a new receiver task, so this one should exit
                    return
                else:
                    logging.info(f"Connection closed, but a reconnect is already in progress; waiting for it to finish ({self.uri_type})")
                    await asyncio.sleep(5)
                    continue
                
            except Exception as e:
                logging.error(f"Error while receiving message: {e}")
                await asyncio.sleep(1)  # Brief delay before retrying
                continue
            
    # Read messages from the queue
    async def get_message(self):
        """Get one message from the message queue."""
        return await self.message_queue.get()
    
    
    async def _check_rate_limit(self) -> None:
        """Enforce the rate limit with a sliding window."""
        async with self.rate_limit_config["lock"]:
            now = time.monotonic()
            timestamps = self.rate_limit_config["timestamps"]
            interval = self.rate_limit_config["interval"]
            max_conn = self.rate_limit_config["max_connections"]

            # Drop stale timestamps
            while timestamps and (now - timestamps[0]) > interval:
                timestamps.popleft()

            # If over the limit, work out how long to wait
            if len(timestamps) >= max_conn:
                oldest = timestamps[0]
                wait_time = interval - (now - oldest)
                # Add a 30-second buffer
                wait_time = math.ceil(wait_time * 1000) / 1000 + 30.0 
                logging.warning(
                    f"Rate limit reached. Waiting {wait_time:.2f} seconds."
                )
                await asyncio.sleep(wait_time)  # Wait (in seconds)
                # Refresh the time and drop stale timestamps again
                now = time.monotonic()
                while timestamps and (now - timestamps[0]) > interval:
                    timestamps.popleft()

            # Record the current timestamp
            timestamps.append(now)
            
            
    async def connect(self, authenticator=None, auth_filter=None, verbose=False) -> bool:
        """Open the WebSocket connection.
        
        Args:
            authenticator: Authenticator used for authentication
            auth_filter: Authentication filter
            verbose (bool): Whether to log connection events
            
        Returns:
            bool: Whether the connection succeeded
        """        
        self.authenticator = authenticator
        self.auth_filter = auth_filter
        
        try:
            await self._check_rate_limit()
            self.websocket = await websockets.connect(
                                        self.uri, 
                                        ping_interval=config.PING_INTERVAL,
                                        ping_timeout=config.PING_TIMEOUT)
            # Automatically start the receiver task
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
        """Reconnect to the WebSocket server.
        
        Args:
            verbose (bool): Whether to log connection events
            
        Returns:
            bool: Whether the reconnect succeeded
        """
        if verbose:
            logging.debug(f"Attempting to reconnect to {self.uri}...")
        
        # Safely close the current connection and cancel the existing receiver task
        await self.disconnect(verbose=verbose)
        
        # Retry the connection with exponential backoff
        self.reconnect_backoff.reset()

        while True:
            try:
                await self._check_rate_limit()
                self.websocket = await websockets.connect(
                                    self.uri, 
                                    ping_interval=config.PING_INTERVAL,
                                    ping_timeout=config.PING_TIMEOUT)
                # Automatically start the receiver task
                await self.start_receiver()

                if verbose:
                    logging.debug(f"Successfully reconnected to {self.uri}")
                
                self.reconnect_backoff.success()
                return True
            
            except Exception as e:
                if verbose:
                    logging.error(f"Reconnection failed (instance ID: {self.instance_id}):\n{traceback.format_exc()}")
                
                # wait() handles the retry count and reset logic itself, and logs accordingly
                was_reset = await self.reconnect_backoff.wait(f"Reconnection ({self.uri_type}) ")
                
                # A reset counter means max retries was reached; exit the loop
                if was_reset:
                    break
        if verbose:
            logging.critical(f"Failed to reconnect after {self.reconnect_backoff.max_retries} attempts (instance ID: {self.instance_id})")
        
        return False
    
    
    @classmethod
    async def check_stuck_reconnecting(cls):
        """Detect and clear stuck reconnect state."""
        now = time.monotonic()
        for conn_type in reversed(LOCK_ORDER):  # ["pub", "auth"]: clean up in reverse order
            async with cls._reconnect_locks[conn_type]:
                if (cls._reconnecting[conn_type] and 
                    now - cls._reconnecting_timestamps[conn_type] > cls._MAX_RECONNECT_TIME):
                    
                    stuck_duration = now - cls._reconnecting_timestamps[conn_type]
                    logging.warning(f"Reconnect state for {conn_type} connection is stuck ({stuck_duration:.1f}s); forcing reset")
                    
                    # Cancel the stuck reconnect task
                    stuck_task = cls._reconnection_tasks[conn_type]
                    if stuck_task and not stuck_task.done():
                        logging.warning(f"Cancelling stuck reconnect task {conn_type}")
                        stuck_task.cancel()
                        try:
                            await asyncio.wait_for(stuck_task, timeout=5.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass
                    
                    # Reset state
                    cls._reconnecting[conn_type] = False
                    cls._reconnection_tasks[conn_type] = None
                    cls._reconnecting_timestamps[conn_type] = now
                    
                    logging.info(f"{conn_type} reconnect state reset; timestamp updated")
    
    
    async def ensure_connection(self, authenticator=None, auth_filter=None, dispatcher=None, processor=None) -> bool:
        """Reconnect, avoiding duplicate reconnect tasks."""
        
        instance_id = self.instance_id
        should_reconnect = False
        
        try:
            async with asyncio.timeout(RECONNECTION_TIMEOUT):
                async with WebSocketClient._reconnect_locks[self.uri_type]:
                    if not WebSocketClient._reconnecting[self.uri_type]:
                        # Cancel any previous reconnect task that may still be running
                        old_task = WebSocketClient._reconnection_tasks[self.uri_type]
                        if old_task and not old_task.done():
                            logging.warning(f"Cancelling previous reconnect task {self.uri_type}")
                            old_task.cancel()
                            try:
                                await asyncio.wait_for(old_task, timeout=5.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass
                        
                        WebSocketClient._reconnecting[self.uri_type] = True
                        WebSocketClient._reconnecting_timestamps[self.uri_type] = time.monotonic()
                        should_reconnect = True
                        logging.debug(f"Reconnect started {self.uri_type} (instance ID: {instance_id})")
                    else:
                        logging.debug(f"Concurrent reconnect request; waiting for the in-progress reconnect to finish {self.uri_type} (instance ID: {instance_id})")
        except asyncio.TimeoutError:
            logging.error(f"Could not acquire reconnect lock {self.uri_type}: timed out")
            return False
        
        # Handle concurrent reconnects
        if not should_reconnect:
            # Wait for the other task to finish reconnecting, then check the connection
            wait_result = await self._wait_for_reconnection_with_timeout()
            if wait_result:
                # Check that the connection is actually usable
                if self.websocket and not self.websocket.closed:
                    try:
                        pong = await asyncio.wait_for(self.websocket.ping(), timeout=3)
                        await pong
                        logging.debug(f"Reconnect finished; connection is usable {self.uri_type} (instance ID: {instance_id})")
                        return True
                    except Exception:
                        logging.warning(f"Reconnect finished, but connection is unusable {self.uri_type} (instance ID: {instance_id})")
                        return False
                else:
                    logging.warning(f"Reconnect finished, but there is no connection {self.uri_type} (instance ID: {instance_id})")
                    return False
            else:
                logging.error(f"Timed out waiting for reconnect {self.uri_type} (instance ID: {instance_id})")
                return False
        
        # Run the actual reconnect in its own task, registered with the watchdog (check_stuck_reconnecting): if it hangs, only this task is cancelled;
        # the caller (reconnection_monitor / receiver) gets False and backs off and retries as usual
        caller = asyncio.current_task()
        caller_is_receiver = self.receiver_task is caller
        if caller_is_receiver:
            self.receiver_task = None  # Keep disconnect() from cancelling the receiver that is waiting on this reconnect
        reconnection = asyncio.create_task(
            self._execute_reconnection(instance_id, authenticator, auth_filter, dispatcher, processor))
        WebSocketClient._reconnection_tasks[self.uri_type] = reconnection
        try:
            await asyncio.wait({reconnection})
            return not reconnection.cancelled() and reconnection.result()
        finally:
            reconnection.cancel()  # If the caller itself is cancelled (e.g. on shutdown), stop the reconnect too
            if caller_is_receiver and self.receiver_task is None:
                self.receiver_task = caller  # Reconnect failed; the receiver resumes its own retry loop
            # Quick state cleanup
            async with WebSocketClient._reconnect_locks[self.uri_type]:
                WebSocketClient._reconnecting[self.uri_type] = False
                WebSocketClient._reconnection_tasks[self.uri_type] = None
                logging.debug(f"Reconnect state cleaned up {self.uri_type} (instance ID: {instance_id})")
    
    async def _wait_for_reconnection_with_timeout(self, timeout=RECONNECTION_WAIT_TIMEOUT):
        """Wait for another reconnect to finish, with a timeout."""
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            try:
                async with asyncio.timeout(1.0):  # At most 1 second per check
                    async with WebSocketClient._reconnect_locks[self.uri_type]:
                        if not WebSocketClient._reconnecting[self.uri_type]:
                            return True
            
            except asyncio.TimeoutError:
                logging.warning(f"Timed out checking reconnect state; still waiting {self.uri_type}")
            await asyncio.sleep(1)
        
        logging.warning(f"Timed out waiting for reconnect to finish {self.uri_type}")
        return False
    
    async def _execute_reconnection(self, instance_id, authenticator=None, auth_filter=None, dispatcher=None, processor=None):
        """Run the actual reconnect logic (without holding the lock)."""
        try:
            # Check whether the connection is still alive
            if self.websocket and not self.websocket.closed:
                try:
                    # Send a ping to test the connection
                    pong = await asyncio.wait_for(self.websocket.ping(), timeout=5)
                    await pong
                    
                    # Check whether the receiver is running
                    if not self.receiver_task or self.receiver_task.done():
                        await self.start_receiver()
                        logging.debug(f"Restarted message receiver {self.uri_type} (instance ID: {instance_id})")
                    
                    # Connection is alive, but the data stream may be broken; reconnect anyway
                    logging.debug(f"Connection still alive, but reconnecting to make sure data is flowing {self.uri_type} (instance ID: {instance_id})")
                    
                except Exception as e:
                    logging.warning(f"Connection not alive: {e}. Will reconnect {self.uri_type} (instance ID: {instance_id})")

            if not await self.reconnect():
                return False
            
            logging.info(f"Reconnected; waiting for the system to stabilize {self.uri_type} (instance ID: {instance_id})")
            await asyncio.sleep(5)
            
            if self.uri_type == "pub" and processor:
                logging.debug(f"Resetting market data processor state (instance ID: {instance_id})")
                processor.reset_channel_state()
                processor.reset_instance_data()
                
                await self.subscribe(config.CHANNELS, processor)
                logging.debug(f"Resubscribed to channels {self.uri_type} (instance ID: {instance_id})")
                await asyncio.sleep(3)
                
            # Re-authenticate
            if self.uri_type == "auth" and authenticator:
                try:
                    await authenticator.authenticate(self, 
                                                     auth_filter=auth_filter, 
                                                     dispatcher=dispatcher)
                    logging.debug(f"Re-authenticated {self.uri_type} (instance ID: {instance_id})")
                    return True
                except Exception as e:
                    logging.error(f"Authentication failed after reconnect: {e} {self.uri_type} (instance ID: {instance_id})")
                    return False
            
            return True
            
        except Exception as e:
            logging.error(f"Error during reconnect: {e} {self.uri_type} (instance ID: {instance_id})")
            return False


    async def send(self, message: Dict[str, Any] | list, retries=3) -> None:
        """
        Serialize the message (dict or list) to a JSON string and send it to the WebSocket server.
        
        Args:
            message (Dict[str, Any] | list)
        """
        for attempt in range(retries):
            try:
                await self.websocket.send(json.dumps(message))
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise Exception(f"Failed to send message after {retries} attempts: {e}")
                await asyncio.sleep(min(30, 2 ** attempt))
    
    
    async def receive(self, message) -> Dict[str, Any] | list:
        """
        Parse the server's JSON string into a dict or list.
        
        Args:
            message (string)

        Returns:
            Dict[str, Any] | list
        """
        return json.loads(message)


    async def subscribe(self, channels: list[Dict[str, Any]], processor: MarketInfoProcessor | None = None) -> None:
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
    