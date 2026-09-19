[{'id': 2, 'value': '0', 'leverage': '0', 'mode': 'dual_long', 'realised_point': '0', 'contract': 'HYPE_USDT', 'entry_price': '0', 'mark_price': '92.702', 'history_point': '0', 'realised_pnl': '0', 'close_order': None, 'size': 0, 'cross_leverage_limit': '75', 'pending_orders': 0, 'adl_ranking': 6, 'maintenance_rate': '0.008333', 'unrealised_pnl': '0', 'pnl_pnl': '0', 'pnl_fee': '0', 'pnl_fund': '0', 'pnl_dividend': '0', 'user': 54673455, 'leverage_max': '75', 'history_pnl': '0', 'risk_limit': '10000', 'margin': '0', 'last_close_pnl': '0', 'liq_price': '0', 'update_time': 1788014623, 'update_id': 5, 'initial_margin': '0', 'maintenance_margin': '0', 'trade_long_size': 0, 'trade_short_size': 0, 'open_time': 0, 'risk_limit_table': 'HYPE_USDT_202509030753', 'average_maintenance_rate': '0', 'voucher_size': '0', 'voucher_margin': '0', 'voucher_id': 0, 'pos_margin_mode': 'cross', 'lever': '75', 'liq_lock': False}, 

{'id': 3, 
'value': '342.9974', 
'leverage': '0', 'mode': 'dual_short', 'realised_point': '0', 

'contract': 'HYPE_USDT', 

'entry_price': '88.077351351351', 'mark_price': '92.702', 'history_point': '0', 'realised_pnl': '-0.0944542929', 'close_order': None, 

'size': -37, 'cross_leverage_limit': '75', 'pending_orders': 0, 'adl_ranking': 3, 'maintenance_rate': '0.008333', 'unrealised_pnl': '-17.111200000001', 'pnl_pnl': '0', 'pnl_fee': '-0.1629431', 'pnl_fund': '0.0684888071', 'pnl_dividend': '0', 'user': 54673455, 'leverage_max': '75', 'history_pnl': '0.7062842514', 'risk_limit': '10000', 'margin': '0', 'last_close_pnl': '0.7062842514', 'liq_price': '152.873', 'update_time': 1789729595, 'update_id': 17, 

'initial_margin': '4.833976690667', 'maintenance_margin': '3.1154453842', 'trade_long_size': 0, 'trade_short_size': 0, 'open_time': 1789698484, 'risk_limit_table': 'HYPE_USDT_202509030753', 'average_maintenance_rate': '0.0083', 'voucher_size': '0', 'voucher_margin': '0', 'voucher_id': 0, 'pos_margin_mode': 'cross', 'lever': '75', 'liq_lock': False}]


[{
'value': '342.9974', 
'mode': 'dual_short', 
'contract': 'HYPE_USDT', 
'entry_price': '88.077351351351',
'size': -37,  
'initial_margin': '4.833976690667', 
}]
"finished open orders": [{
                "tag": "finished open orders",
                "id": "1111838",
                "timestamp": "1789721353576",
                "Contract": "HYPE_USDT",
                "price": "90.407",
                "side": "Open Short",
                "size": "-18",
                "value": "-163.13",
                "status": "finished"
            }],
预期:新的def _close_details(order, available). def _target_price(order, available). 应该调用request.url = '/futures/usdt/positions/XXX_USDT(这里的contracts需要被传参.对应的是 finished open orders数组的"Contract": "")'
根据返回的 JSON 数据,原close_size,_target_price分别填入对应的 value,entry_price,size,initial_margin_
示例:
JSON 数据:
[{
'value': '342.9974', 
'mode': 'dual_short', 
'contract': 'HYPE_USDT', 
'entry_price': '88.077351351351',
'size': -37,  
'initial_margin': '4.833976690667', 
}]
原close_size = -Decimal(_required_text(order, "size"))
原def _target_price(order, available):
    '''需要修改'''
    try:
        price = Decimal(_required_text(order, "price"))
        value = Decimal(_required_text(order, "value"))
        available = Decimal("0.01") * Decimal(str(available))
    except (InvalidOperation, ValueError) as exc:
        raise PtoError("price、value 和 available 必须是有效数字") from exc
    if price <= 0 or value == 0:
        raise PtoError("price 必须大于 0，value 不能为 0")
    target = price * (Decimal("1") + available * Decimal("3.1") / value)
    if target <= 0:
        raise PtoError(f"计算出的 target price 必须大于 0，实际为 {target}")
    return _decimal_text(target)
新close_size = -Decimal(-37('size':''的传参))
新def _target_price(order, available):
    '''需要修改'''
    try:
        price = 88.077351351351('entry_price')
        value = 342.9974('value')
        initial_margin
    except (InvalidOperation, ValueError) as exc:
        raise PtoError("price、value 和 available 必须是有效数字") from exc
    if price <= 0 or value == 0:
        raise PtoError("price 必须大于 0，value 不能为 0")
    target_raw = price * (Decimal("1") + initial_margin * Decimal("3.1") / value)
    if size < 0('size': -37.被json数据传参进而分类讨论):
        target = 0.99 * target_raw
        else:
        target = 1.01 * target_raw
    if target <= 0:
        raise PtoError(f"计算出的 target price 必须大于 0，实际为 {target}")
    return _decimal_text(target)
激活价格是否继续按 finished open orders 中 price 的小数位数四舍五入？
是

完成e:\git\0AAimportant\gate_bot\ago\v3\auto_orders\tests\orders2\pto.py的修改.使得符合预期.然后完成E:\git\0AAimportant\gate_bot\ago\v3\auto_orders\tests\orders2\test_pto.py. E:\git\0AAimportant\gate_bot\ago\v3\auto_orders\tests\orders2\test_main.py的测试.最后.如有
没说清楚的地方.请向我提问

"finished open orders": [
            {
                "tag": "pending open orders",
                "id": "1111838",
                "timestamp": "1789721353576",
                "Contract": "HYPE_USDT",
                "price": "90.407",
                "side": "Open Short",
                "size": "-18",
                "value": "-163.13",
                "status": "finished"
            },
            {
                "tag": "finished open orders",
                "id": "1112111",
                "timestamp": "1789736880717",
                "Contract": "SOL_USDT",
                "price": "108.27",
                "side": "Open Short",
                "size": "-1",
                "value": "-201.35",
                "status": "finished"
            }
        ]


1.
        "finished open orders": [{
                "tag": "finished open orders",
                "id": "1111838",
                "timestamp": "1789721353576",
                "Contract": "HYPE_USDT",
                "price": "90.407",
                "side": "Open Short",
                "size": "-18",
                "value": "-163.13",
                "status": "finished"
            }],
        "pending close orders": [],
---

pto轮询orderlist.js.看到了"finished open orders"有订单.准备发布inverse_pto前.先对"pending close orders"检查.查看是否有同币种同方向(这里的'同方向'有对应关系.规则: Open Short对应Close Short. Open Long对应Close Long)的订单.分类讨论.如果有:则使用def _stop_trailing_order对"pending close orders"对应的"id": "1111839",向 Gate 发出停止追踪订单请求.如果无:发布inverse_pto.发布成功.把"finished open orders"订单移入到"pending close orders".发布失败.orderlist不改变.
2.
        "finished open orders": [],
        "pending close orders": [{
                "tag": "pending close orders.inverse 1111838",
                "id": "1111839",
                "timestamp": "1789721354576",
                "Contract": "HYPE_USDT",
                "price": "88.500",
                "side": "Close Short",
                "size": "18",
                "value": "-163.13",
                "status": "open"
            }],
---

pto轮询orderlist.js.看到了"finished open orders"有订单.准备发布inverse_pto前.先对"pending close orders"检查.查看是否有同币种同方向(这里的'同方向'有对应关系.规则: Open Short对应Close Short. Open Long对应Close Long)的订单.分类讨论.如果有:则使用def _stop_trailing_order对"pending close orders"对应的"id": "1111839",向 Gate 发出停止追踪订单请求.如果无:发布inverse_pto.发布成功.把"finished open orders"订单移入到"pending close orders".发布失败.orderlist不改变.
3.      "finished open orders": [{
                "tag": "finished open orders",
                "id": "1111840",
                "timestamp": "1789721353576",
                "Contract": "HYPE_USDT",
                "price": "99.507",
                "side": "Open Short",
                "size": "-18",
                "value": "-163.13",
                "status": "finished"
            }],
        "pending close orders": [{
                "tag": "pending close orders.inverse 1111838",
                "id": "1111841",
                "timestamp": "1789721354577",
                "Contract": "HYPE_USDT",
                "price": "78.600",
                "side": "Close Short",
                "size": "30",
                "value": "-363.13",
                "status": "open"
            }],

发现同币种同方向的旧平仓追踪单后，停止成功时应如何继续？
答: 本轮立即发布新平仓单，成功后删除旧单并移走 finished open 原单

如果旧单已停止成功，但新平仓单发布失败，你写的“orderlist 不改变”是否也适用？
答: 适用：文件完全保持原样，后续轮询重试 