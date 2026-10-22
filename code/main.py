import asyncio
import logging
import config
import traceback

from datetime import datetime
from websocket_client import WebSocketClient, MessageDispatcher, LOCK_ORDER
from authenticator import Authenticator
from market_info_processor import MarketInfoProcessor
from order_manager import OrderManager
from wallet_manager import WalletManager
from utils.time import get_elapsed_time, format_elapsed_time
from utils.logging_config import setup_logging
from utils.backoff import ExponentialBackoff
from rate_calculator import calculate_rate




# Timeout constants
RECONNECTION_REQUEST_TIMEOUT = 5.0  # Lock acquisition timeout when requesting a reconnect

# Initialize logging
filename = "main_dev.log" if config.ENVIRONMENT == "development" else "main.log"
setup_logging(log_level=config.LOGGING_LEVEL, 
              log_file=fr"./logs/{filename}")

# Get a child logger
logger = logging.getLogger(__name__)


"""
TODO:
        1. Laddered offers instead of using all funds at once -- not that much money yet :)
        2. The smart candles strategy is intentionally disabled/parked for now;
           do not wire candles back into the main flow until the FRR/Active flow is stable and tested.
"""




async def reconnection_monitor(reconnect_queue, pub_client, auth_client, market_info_processor):
    """Background task - monitor and handle reconnect requests via a queue"""
    
    # Exponential backoff per connection type
    reconnect_backoffs = {
        "pub": ExponentialBackoff(base_delay=5, max_delay=120, max_retries=10, jitter=True),
        "auth": ExponentialBackoff(base_delay=5, max_delay=120, max_retries=10, jitter=True)
    }
    
    while True:
        # Take reconnect requests from the queue
        client_type = await reconnect_queue.get()
        # Drain duplicate requests of the same type at once to avoid back-to-back reconnects after recovery; requeue other types
        other_requests = []
        while not reconnect_queue.empty():
            pending = reconnect_queue.get_nowait()
            if pending != client_type:
                other_requests.append(pending)
        for pending in other_requests:
            reconnect_queue.put_nowait(pending)

        client = pub_client if client_type == "pub" else auth_client
        instance_id = client.instance_id
        backoff = reconnect_backoffs[client_type]
        
        # Check whether a reconnect task is already in progress
        should_attempt_reconnect = False
        existing_task = None
        async with WebSocketClient._reconnect_locks[client_type]:
            if not WebSocketClient._reconnecting.get(client_type, False):
                should_attempt_reconnect = True
            else:
                existing_task = WebSocketClient._reconnection_tasks.get(client_type)
        
        if existing_task and not existing_task.done():
            logger.warning(f"{client_type} client already has a reconnect task in progress, skipping duplicate request (instance ID: {instance_id})")
            continue
        
        if should_attempt_reconnect and client:
            logger.warning(f"Handling reconnect request: {client_type} client (instance ID: {instance_id})")
            try:          
                success = await client.ensure_connection(
                    authenticator=client.authenticator if client_type == "auth" else None,
                    auth_filter=client.auth_filter if client_type == "auth" else None,
                    dispatcher=client.dispatcher if client_type == "auth" else None,
                    processor=market_info_processor if client_type == "pub" else None
                )
                
                if success:
                    logger.info(f"{client_type} reconnected successfully (instance ID: {instance_id})")
                    # Reconnected; reset the backoff counter
                    backoff.success()
                    await asyncio.sleep(10.0)  # Brief pause after success
                else:
                    logger.error(f"{client_type} reconnect failed... (instance ID: {instance_id})")
                    # Reconnect failed; apply exponential backoff
                    await backoff.wait(f"{client_type} reconnect failed, ")
                    
            except Exception as e:
                logger.error(f"{client_type} error during reconnect: {e} (instance ID: {instance_id})")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                # On exception, also apply exponential backoff
                await backoff.wait(f"{client_type} reconnect error, ")
        else:
            logger.debug(f"{client_type} client does not meet reconnect conditions, skipping this request (instance ID: {instance_id})")
            # Use a shorter wait when conditions are not met
            await asyncio.sleep(2.0)

            
async def market_info_task(market_info_processor, market_queue):
    """Background task - keep market data updated without blocking the main flow"""
    logger.info("Starting market data background task...")
    
    while True:
        try:
            await market_info_processor.handle_market_messages(market_queue)
                
        except Exception as e:
            logger.error(f"Error handling market messages: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            await asyncio.sleep(5.0)


# Background task for funding offers
async def order_management_task(order_manager, fo_queue):
    """Background task - handle offer-related messages"""
    logger.info("Starting offer management background task...")
    
    while True:
        try:
            await order_manager.handle_fo_messages(fo_queue)
            
        except Exception as e:
            logger.error(f"Offer management task error: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            await asyncio.sleep(5.0)
        
        
# Background task for balances
async def wallet_manager_task(wallet_manager, wallet_queue):
    """Background task - handle wallet-related messages"""
    logger.info("Starting wallet management background task...")
    
    while True:
        try:
            await wallet_manager.handle_wallet_messages(wallet_queue)
        
        except Exception as e:
            logger.error(f"Wallet management task error: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            await asyncio.sleep(5)


# Offer placement logic
async def place_order(order_manager,
                      ticker_stats,
                      funding_balance,
                      symbol,
                      candles_stats=None,
                      days=2,
                      book_rate=None,
                      mode="smart"):
    """Execute order: calculate rate, log, and submit."""
    try:
        desired_rate = calculate_rate(
            mode=mode,
            min_rate_yearly=config.MINIMUM_RATE,
            frr_rate_discount=config.FRR_RATE_DISCOUNT,
            active_rate_discount=config.ACTIVE_RATE_DISCOUNT,
            ticker_stats=ticker_stats,
            candles_stats=candles_stats,
            book_rate=book_rate,
        )

        logger.info(f"Placing offer: {funding_balance:.2f} {symbol} | Annual rate: {desired_rate*365*100:.4f}% | Daily rate: {desired_rate*100:.6f}% | Period: {days} days | Strategy: {mode}")

        await order_manager.new_funding_offer(
            symbol,
            funding_balance,
            desired_rate,
            days
        )
        logger.info("**Offer placed**")
        return True

    except Exception as e:
        logger.error(f"Failed to place offer: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return False


async def handle_cancel_order_timeout(order_manager, active_id, counter_cancel, timeout_seconds):
    """
    Handle an offer timeout; returns (new_counter_cancel, success or not)
    """
    instance_id = order_manager.websocket_client.instance_id
    try:
        await order_manager.cancel_funding_offer(active_id)
        logger.info(f"Offer unfilled for over {timeout_seconds:.0f}s; offer cancelled")
        return 0, True
    
    except Exception as e:
        logger.error(f"Error cancelling offer, continuing: {e}")
        # Don't count errors during a reconnect
        # Check the lock state first to avoid deadlock
        is_reconnecting = False
        async with WebSocketClient._reconnect_locks["auth"]:
            is_reconnecting = WebSocketClient._reconnecting.get("auth", False)

        # Proceed only after checking the lock state
        if not is_reconnecting:
            counter_cancel += 1
            # More than 5 consecutive failures triggers a reconnect
            if counter_cancel > 5:
                logger.error(f"`cancel offer` task failed more than 5 times in a row, requesting reconnect... (instance ID: {instance_id})")
                raise
        else:
            logger.warning(f"Offer cancel error occurred during reconnect; not counted as an error this time. (instance ID: {instance_id})")
            
        return counter_cancel, False  # Skip to the next loop iteration


async def lending_engine(order_manager, 
                         market_info_processor, 
                         wallet_manager, 
                         reconnect_queue,
                         pub_client, auth_client):
    """Advanced version: FRR → Active → circuit breaker"""
    
    # NOTE: Bitfinex production uses UST for USDT; config.SYMBOL normalizes it.
    symbol = f"TEST{config.SYMBOL}" if config.ENVIRONMENT == "development" else config.SYMBOL
    counter_cancel = 0  # Error count for offer cancellation
    
    # Offer strategy state
    current_mode = "frr"  # One of: frr, active
    frr_failure_count = 0  # FRR offer failure count
    active_attempt_count = 0  # Active mode attempt count
    circuit_breaker_triggered = False
    circuit_breaker_start_time = None
    # Mode each order was placed in, so a mode switch doesn't apply the wrong timeout
    order_modes: dict[int, str] = {}
    # Partially filled offers already counted as a success, so strategy state isn't reset every iteration
    partially_filled_ids: set[int] = set()
    last_heartbeat = datetime.now()
    
    # Strategy parameters - read from config
    FRR_FAILURE_THRESHOLD = config.FRR_FAILURE_THRESHOLD
    ACTIVE_MAX_ATTEMPTS = config.ACTIVE_MAX_ATTEMPTS
    ACTIVE_ORDER_TIMEOUT = config.ACTIVE_ORDER_TIMEOUT
    CIRCUIT_BREAKER_DURATION = config.CIRCUIT_BREAKER_DURATION
    
    # Exponential backoff instance
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
            
            # Check circuit breaker state
            if circuit_breaker_triggered:
                if circuit_breaker_start_time:
                    elapsed = (datetime.now() - circuit_breaker_start_time).total_seconds()
                    if elapsed >= CIRCUIT_BREAKER_DURATION:
                        # Circuit breaker over; reset all state
                        circuit_breaker_triggered = False
                        circuit_breaker_start_time = None
                        current_mode = "frr"
                        frr_failure_count = 0
                        active_attempt_count = 0
                        logger.info("Circuit breaker ended; strategy state reset, back to FRR mode")
                    else:
                        remaining = CIRCUIT_BREAKER_DURATION - elapsed
                        logger.debug(f"Circuit breaker active, {remaining:.1f}s remaining")
                        await asyncio.sleep(config.MAIN_LOOP_DELAY)
                        continue
            
            # Get the IDs of the currently active offers (asks)
            _, ask_ids = await order_manager.get_active_order_ids(symbol)
            
            # Flags tracking the state of this iteration
            any_order_executed = False
            stop_processing_orders = False
            
            if ask_ids:
                # Drop records for orders no longer in the active list
                active_set = set(ask_ids)
                for stale_id in [oid for oid in order_modes if oid not in active_set]:
                    order_modes.pop(stale_id, None)
                partially_filled_ids &= active_set

                for ask_id in ask_ids:
                    # Check whether the offer has been filled
                    is_executed, execution_status, offer_info = await order_manager.check_order_executed(ask_id)

                    if is_executed and 'EXECUTED' in execution_status:
                        # Offer fully filled! Mark success, but keep checking the other offers
                        logger.info(f"Offer filled | Status: {execution_status} | ID: {ask_id}")
                        order_modes.pop(ask_id, None)
                        any_order_executed = True
                        continue  # Check the next ask_id

                    if is_executed and ask_id not in partially_filled_ids:
                        # Partially filled: each offer counts as a success only once; the remainder goes through the timeout cancel below and is re-placed next iteration
                        logger.info(f"Offer partially filled | Status: {execution_status} | ID: {ask_id}")
                        partially_filled_ids.add(ask_id)
                        any_order_executed = True

                    # ----- Only if not fully filled, proceed with timeout handling -----
                    last_update_time = await order_manager.get_order_update_time(ask_id)
                    if last_update_time:
                        time_diff = get_elapsed_time(last_update_time)
                        logger.debug(f"Offer ID: {ask_id} | Unfilled for: {format_elapsed_time(time_diff)}")
                    else:
                        logger.warning(f"Offer ID: {ask_id} | No update time found")
                        continue

                    # Pick the timeout by the mode the offer was placed in, so a mode switch doesn't cancel it by mistake
                    # Orders of unknown origin (e.g. left over from before a restart, or missed by the post-placement snapshot)
                    # conservatively fall back to the longer FRR timeout, and log a WARNING to track how often this happens
                    placed_mode = order_modes.get(ask_id)
                    if placed_mode is None:
                        logger.warning(f"Offer {ask_id} has no recorded mode, falling back to frr (timeout={config.FRR_ORDER_TIMEOUT} min)")
                        placed_mode = "frr"
                    if placed_mode == "active":
                        timeout_seconds = ACTIVE_ORDER_TIMEOUT
                    else:
                        timeout_seconds = 60.0 * config.FRR_ORDER_TIMEOUT
                    timeout_condition = time_diff and time_diff["total_seconds"] > timeout_seconds

                    # Handle offer timeout
                    if timeout_condition:
                        counter_cancel, cancel_success = await handle_cancel_order_timeout(
                            order_manager, ask_id, counter_cancel, timeout_seconds
                        )

                        if not cancel_success:
                            # Cancel failed (system error); stop processing orders this iteration
                            logger.warning(f"Failed to cancel offer {ask_id}; pausing order processing until the next main loop iteration.")
                            stop_processing_orders = True
                            break  # Break out of the for loop
                        else:
                            # Order cancelled on timeout (strategy failure); update the failure count for the mode it was placed in
                            order_modes.pop(ask_id, None)
                            if placed_mode == "frr":
                                frr_failure_count += 1
                            elif placed_mode == "active":
                                active_attempt_count += 1
                
                # Check the flags after the loop
                if stop_processing_orders:
                    await asyncio.sleep(config.MAIN_LOOP_DELAY)
                    continue  # Continue the main while loop

                if any_order_executed:
                    logger.info("Offer fill detected")
                    frr_failure_count = 0
                    active_attempt_count = 0
                    current_mode = "frr"

            # Mode switching: FRR → Active → circuit breaker
            if current_mode == "frr":
                if frr_failure_count >= FRR_FAILURE_THRESHOLD:
                    current_mode = "active"
                    active_attempt_count = 0
                    logger.info(f"FRR mode failed {FRR_FAILURE_THRESHOLD} times, switching to Active mode")
                    
            elif current_mode == "active":
                if active_attempt_count >= ACTIVE_MAX_ATTEMPTS:
                    # Trip the circuit breaker
                    circuit_breaker_triggered = True
                    circuit_breaker_start_time = datetime.now()
                    logger.warning(f"Active mode failed {ACTIVE_MAX_ATTEMPTS} times, circuit breaker tripped! Pausing for {CIRCUIT_BREAKER_DURATION}s")
                    await asyncio.sleep(config.MAIN_LOOP_DELAY)
                    continue

                    
            # Fetch the latest balance
            balances_available = await wallet_manager.get_wallet_balances(symbol, wait_for_update=True, timeout=2.0)
            funding_balance = balances_available.get("funding", 0.0)
            
            if funding_balance is None:
                funding_balance = 0.0
            
            logger.debug(f"Available balance: {funding_balance:.4f} {symbol}")

            # Decide whether market data is needed based on the strategy mode
            ticker_data = None
            ticker_stats = None
            last_update = None
            
            if current_mode == "active":
                # Active mode only needs book_rate; fetch it directly
                logger.debug("Active mode: fetching book data directly, skipping the wait for market data")
                book_data, book_rate = await market_info_processor.get_book_info(timeout=5)
            else:
                # FRR and other modes need full market data
                ticker_data, ticker_stats = await market_info_processor.get_ticker_info(timeout=config.MARKET_DATA_FETCH_TIMEOUT * 60)
                
                last_update = ticker_data.get("LAST_UPDATE_TIME", None)
                book_data, book_rate = await market_info_processor.get_book_info(timeout=5)
                # _, candles_stats = await market_info_processor.get_candles_info(timeout=30)

                # Check data freshness
                if last_update:
                    age = (datetime.now() - last_update).total_seconds()
                    
                    if age > config.MARKET_DATA_CRITICAL_STALE_MINUTES * 60:
                        instance_id = pub_client.instance_id
                        logger.warning(f"Market data critically stale ({age/60:.1f} min), requesting reconnect... (instance ID: {instance_id})")
                        # Trigger the reconnect via the queue to avoid deadlock risk
                        try:
                            async with asyncio.timeout(RECONNECTION_REQUEST_TIMEOUT):
                                async with WebSocketClient._reconnect_locks["pub"]:
                                    if not WebSocketClient._reconnecting["pub"]:
                                        await reconnect_queue.put("pub")
                                        logger.info(f"Triggered pub client reconnect (instance ID: {instance_id})")
                        except asyncio.TimeoutError:
                            logger.warning(f"Timed out triggering reconnect request, skipping this request (instance ID: {instance_id})")
                        # Continue the loop and wait for the next check  
                        continue
                
                    if age <= config.MARKET_INFO_MAX_AGE_MINUTES * 60:
                        logger.debug(f"Market data is fresh, age: {age/60:.1f} min")
                    else:
                        logger.debug(f"Market data (age: {age/60:.1f} min) not updated for over {config.MARKET_INFO_MAX_AGE_MINUTES} min, waiting for an update...")
                        continue
                else:
                    logger.debug("No valid market data yet, waiting for the next iteration...")
                    continue
            
            # Offer placement
            if funding_balance and funding_balance >= 150.0:
                if current_mode == "frr":
                    logger.debug(f"Current strategy mode: {current_mode.upper()} - failure count: {frr_failure_count}/{FRR_FAILURE_THRESHOLD}")
                elif current_mode == "active":
                    logger.debug(f"Current strategy mode: {current_mode.upper()} - failure count: {active_attempt_count}/{ACTIVE_MAX_ATTEMPTS}")

                # Snapshot existing orders before placing, so new ones can be identified and tagged with the mode afterwards
                _, ask_ids_before = await order_manager.get_active_order_ids(symbol)

                # Guard: skip placing this iteration if an offer is already active, to avoid duplicate offers when wallet/offer state is out of sync
                if ask_ids_before:
                    logger.debug(f"Found {len(ask_ids_before)} active offer(s), skipping placement this iteration")
                    # Waiting for a fill counts as normal operation, so reset the error counters before continuing
                    counter_cancel = 0
                    error_backoff.success()
                    await asyncio.sleep(config.MAIN_LOOP_DELAY)
                    continue

                place_success = await place_order(order_manager, ticker_stats, funding_balance, symbol,
                    candles_stats=None, book_rate=book_rate, mode=current_mode)

                if not place_success:
                    logger.warning(f"Offer submission failed, will retry next iteration.")
                else:
                    logger.info(f"Offer submitted! Waiting for it to fill...")
                    _, ask_ids_after = await order_manager.get_active_order_ids(symbol)
                    for new_id in set(ask_ids_after) - set(ask_ids_before):
                        order_modes[new_id] = current_mode
            else:
                logger.debug("Insufficient balance; the minimum offer is 150.0 USD or the USDT equivalent, waiting for the next iteration...")

            
            # Current market data (shown only outside active mode)
            if ticker_stats:
                logger.debug(f"{'USDT' if symbol == 'UST' else symbol} market data:")
                logger.debug(f"  FRR (Year): {ticker_stats['frr_yearly']:.4f}%")
                logger.debug(f"  Low Rate (Year): {ticker_stats['low_rate_yearly']:.4f}%")
                logger.debug(f"  High Rate (Year): {ticker_stats['high_rate_yearly']:.4f}%")
            else:
                logger.debug(f"{current_mode.upper()} mode: skipping market data display")
            
            # success, reset counters
            counter_cancel = 0
            error_backoff.success()  # Reset the backoff counter

            # Heartbeat: log a one-line INFO summary every HEARTBEAT_INTERVAL seconds instead of flooding DEBUG
            if config.HEARTBEAT_INTERVAL > 0:
                now = datetime.now()
                if (now - last_heartbeat).total_seconds() >= config.HEARTBEAT_INTERVAL:
                    parts = [
                        f"mode={current_mode}",
                        f"balance={funding_balance:.2f}",
                        f"orders={len(ask_ids) if ask_ids else 0}",
                    ]
                    if ticker_stats:
                        parts.append(f"frr={ticker_stats['frr_yearly']:.4f}%")
                        parts.append(f"low={ticker_stats['low_rate_yearly']:.4f}%")
                    if book_rate is not None:
                        parts.append(f"book={book_rate*365*100:.4f}%")
                    if last_update:
                        parts.append(f"age={(now - last_update).total_seconds():.0f}s")
                    logger.info("heartbeat | " + " | ".join(parts))
                    last_heartbeat = now

            # Sleep briefly before checking again
            await asyncio.sleep(config.MAIN_LOOP_DELAY)
            
        except Exception as e:
            logger.error(f"`lending_engine` error: {type(e).__name__}: {e}\n{traceback.format_exc()}")    
            
            # Check clients in lock order; an engine error may mean either one is broken
            reconnection_triggered = False
            for client_type in LOCK_ORDER:  # ["auth", "pub"]
                try:
                    client = auth_client if client_type == "auth" else pub_client
                    instance_id = client.instance_id
                    
                    # Use a timeout to avoid waiting forever
                    async with asyncio.timeout(RECONNECTION_REQUEST_TIMEOUT):
                        async with WebSocketClient._reconnect_locks[client_type]:
                            if not WebSocketClient._reconnecting[client_type]:
                                logger.error(f"Triggering reconnect: {client_type} client (instance ID: {instance_id})")
                                await reconnect_queue.put(client_type)
                                reconnection_triggered = True
                                # No break here, so both clients get checked
                                
                except asyncio.TimeoutError:
                    logger.warning(f"Timed out acquiring {client_type} lock, skipping (instance ID: {instance_id})")
                    continue
                
                except Exception as lock_error:
                    logger.error(f"Error checking {client_type} reconnect state: {lock_error} (instance ID: {instance_id})")
                    continue
            
            if not reconnection_triggered:
                logger.warning("Could not trigger any reconnect; all clients may already be reconnecting")
            
            # Sleep briefly, then keep trying
            await asyncio.sleep(config.MAIN_LOOP_DELAY * 2)
            logger.error("`lending_engine` error, entering exponential backoff...")
            # Exponential backoff instead of a fixed wait
            await error_backoff.wait("`lending_engine` ")
  

async def reconnection_state_monitor():
    """Background task - periodically check for and reset stuck reconnect state"""
    logger.info("Starting reconnect state monitor task...")
    
    while True:
        try:
            # Check every 60 seconds
            await asyncio.sleep(60)
            await WebSocketClient.check_stuck_reconnecting()
        except Exception as e:
            # This task has no WebSocket instance, so keep using the task ID
            task_id = id(asyncio.current_task())
            logger.error(f"Reconnect state monitor task error: {e} (task ID: {task_id})")
            await asyncio.sleep(60)  # Wait a while after an error before continuing


async def main():
    # Initialize reconnect state so all reconnect flags start clean
    task_id = id(asyncio.current_task())
    for conn_type in ["pub", "auth"]:
        async with WebSocketClient._reconnect_locks[conn_type]:
            if WebSocketClient._reconnecting[conn_type]:
                logger.warning(f"At startup, {conn_type} reconnect state was True; resetting to False (task ID: {task_id})")
                WebSocketClient._reconnecting[conn_type] = False
    
    # Create WebSocket clients
    auth_ws_client = WebSocketClient(config.AUTH_URI, uri_type="auth")
    public_ws_client = WebSocketClient(config.PUBLIC_URI, uri_type="pub")
    # Reconnect requests go through a queue
    reconnect_queue = asyncio.Queue()
    
    # Create the MessageDispatcher
    dispatcher = MessageDispatcher(auth_ws_client, public_ws_client)
    
    # Create the other module instances
    authenticator = Authenticator(config.API_KEY, config.API_SECRET)
    market_info_processor = MarketInfoProcessor()
    order_manager = OrderManager(auth_ws_client, authenticator, dispatcher)
    wallet_manager = WalletManager(auth_ws_client, authenticator, dispatcher)
    
    # Wire up dependencies
    auth_ws_client.set_dispatcher(dispatcher)
    public_ws_client.set_processor(market_info_processor)
    market_info_processor.set_reconnect_queue(reconnect_queue)
    
    # Open the auth connection; if that fails at startup, back off and retry until connected
    auth_filter = list(config.AUTH_FILTER) if config.AUTH_FILTER else None
    startup_backoff = ExponentialBackoff(base_delay=5, max_delay=120, max_retries=10, jitter=True)
    while not await auth_ws_client.connect(authenticator, auth_filter=auth_filter):
        await startup_backoff.wait("Auth connection failed at startup, ")

    # Open the public connection
    startup_backoff.reset()
    while not await public_ws_client.connect():
        await startup_backoff.wait("Public connection failed at startup, ")
    await public_ws_client.subscribe(config.CHANNELS, market_info_processor)  # Pass the processor instance
    
    # Start listener tasks
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
    
    # Add the reconnect state monitor task
    reconnect_state_monitor_task = asyncio.create_task(
        reconnection_state_monitor()
    )
    
    # Authenticate after dispatch_task starts: the auth reply reaches
    # authenticate() through the dispatcher's auth_queue
    await authenticator.authenticate(auth_ws_client, auth_filter=auth_filter, dispatcher=dispatcher)
   
    tasks = [
        market_task,
        dispatch_task,
        order_task,
        wallet_task,
        lending_engine_task,
        reconnect_task,
        reconnect_state_monitor_task  # Watchdog for stuck reconnects
    ]
    # Main loop
    try:
        
        await asyncio.gather(*tasks)
        
    except Exception as e:
        logger.error(f"Main loop error: {e}")
            
    finally:
        # Clean up resources
        # Cancel the business logic first, then the reconnect tasks, then the
        # queue consumers, and finally the dispatcher and the market data task
        cleanup_order = [
            lending_engine_task,           # Cancel core business logic first
            reconnect_state_monitor_task, # Cancel the reconnect state monitor
            reconnect_task,             # Cancel reconnect handling
            order_task,                 # Cancel order processing
            wallet_task,                # Cancel wallet processing
            dispatch_task,              # Cancel message dispatch
            market_task,                # Cancel market data last
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
                    logger.error(f"Error cleaning up task: {e}")
        
        await public_ws_client.disconnect()
        await auth_ws_client.disconnect()
    

if __name__ == "__main__":
    config.validate_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() has already cancelled the tasks and run main()'s cleanup
        logger.info("Stopped by user (Ctrl+C)")