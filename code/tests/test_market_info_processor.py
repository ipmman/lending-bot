import asyncio

from market_info_processor import MarketInfoProcessor


async def test_maintenance_end_triggers_resubscribe(caplog):
    """20061 維護結束：文件建議重新訂閱所有頻道，透過重連公開連線完成"""
    processor = MarketInfoProcessor()
    reconnect_queue = asyncio.Queue()
    processor.set_reconnect_queue(reconnect_queue)
    market_queue = asyncio.Queue()
    market_queue.put_nowait({"event": "info", "code": 20061, "msg": "Maintenance ended"})

    task = asyncio.create_task(processor.handle_market_messages(market_queue))
    await asyncio.sleep(0.01)
    task.cancel()

    assert reconnect_queue.get_nowait() == "pub"
    assert "Maintenance ended" in caplog.text
