from order_manager import OrderManager


def _offer(offer_id):
    return [offer_id, "fUST", 0, 0, 500.0, 500.0, "LIMIT", None, None, 0, "ACTIVE",
            None, None, None, 0.0003, 2, False, False, None, False, None]


async def test_empty_snapshot_clears_stale_offers():
    """重連後交易所回 [0,"fos",[]]：斷線期間已成交/取消的掛單不能殘留"""
    om = OrderManager(None, None, None)
    await om._handle_snapshot([_offer(111)])

    await om._handle_snapshot([])
    assert await om.get_active_order_ids("UST") == ([], [])

    await om.update_offer(om._parse_single_offer(_offer(222)))
    assert await om.get_active_order_ids("UST") == ([], [222])
