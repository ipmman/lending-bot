import asyncio

from websocket_client import WebSocketClient


async def test_stuck_watchdog_cancels_only_the_reconnection(monkeypatch):
    """看門狗取消卡住的重連時，呼叫者（reconnection_monitor / 接收器）要拿到 False 繼續重試，不能被一起取消"""
    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(WebSocketClient, "_execute_reconnection", hang)
    monkeypatch.setattr(WebSocketClient, "_MAX_RECONNECT_TIME", 0)
    caller = asyncio.create_task(WebSocketClient("wss://example", "pub").ensure_connection())
    await asyncio.sleep(0.01)

    await WebSocketClient.check_stuck_reconnecting()
    assert await asyncio.wait_for(caller, 1) is False
