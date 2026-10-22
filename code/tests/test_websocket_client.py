import asyncio

import pytest

from authenticator import Authenticator
from websocket_client import MessageDispatcher, WebSocketClient


class _FakeClient:
    def __init__(self, messages):
        self.queue = asyncio.Queue()
        for message in messages:
            self.queue.put_nowait(message)

    async def get_message(self):
        return await self.queue.get()

    async def send(self, message):
        pass


async def _authenticate(reply):
    # Bitfinex 實際順序：auth 回應(dict) 之後才是各種 channel 0 快照
    auth_client = _FakeClient([reply, [0, "ps", []], [0, "n", [0, "fon-req"]], [0, "bu", [0, 0]]])
    dispatcher = MessageDispatcher(auth_client, _FakeClient([]))
    dispatch_task = asyncio.create_task(dispatcher.dispatch_messages())
    try:
        return await asyncio.wait_for(
            Authenticator("key", "secret").authenticate(auth_client, dispatcher=dispatcher), 1)
    finally:
        await asyncio.sleep(0)
        assert dispatcher.auth_queue.empty()
        dispatch_task.cancel()


async def test_authenticate_returns_true_on_ok():
    assert await _authenticate({"event": "auth", "status": "OK", "chanId": 0}) is True


async def test_authenticate_raises_on_failed():
    with pytest.raises(Exception, match="Cannot authenticate"):
        await _authenticate({"event": "auth", "status": "FAILED", "code": 10100})


async def test_rejected_request_reason_is_logged(caplog):
    rejected = [0, "n", [0, "fon-req", None, None, [], None, "ERROR", "Invalid offer: not enough balance"]]
    dispatch_task = asyncio.create_task(MessageDispatcher(_FakeClient([rejected]), _FakeClient([])).dispatch_messages())
    await asyncio.sleep(0.01)
    dispatch_task.cancel()
    assert "Invalid offer: not enough balance" in caplog.text


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
