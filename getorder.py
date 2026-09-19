'''
原工作流顺序为:
1. 去 GitHub 读取 size.txt，提取币种、价格、做多还是做空、数量等信息。
2. 看是不是新一批订单：根据时间戳判断，已经处理过的直接跳过。
3. 筛掉重复或暂时不能开的订单，例如同一个币、同一个方向：
    - 最近 7 天有已完成的开仓记录；
    - 最近 7 天有待平仓记录；
    - 已有标记为 ongoing open orders 的开仓订单。

4. 处理旧追踪单：新订单通过筛选后，如果还有同币种、同方向、非 ongoing 的待开仓单，就调用 Gate 接口停止旧追踪单。
5. 把新订单追加到本地 orderlist.js 的 raw orders，并更新“这批已经处理过”的时间戳。即使全部被筛掉，也更新时间戳。
新预期: 顺序修改为:
1. 处理旧追踪单：如果有待开仓单，就调用 Gate 接口停止旧追踪单。
2. 去 GitHub 读取 size.txt，提取币种、价格、做多还是做空、数量等信息。
3. 看是不是新一批订单：根据时间戳判断，已经处理过的直接跳过。
4. 筛掉重复或暂时不能开的订单，例如同一个币、同一个方向：
    - 最近 7 天有已完成的开仓记录；
    - 最近 7 天有待平仓记录；
    - 已有标记为 ongoing open orders 的开仓订单。

5. 把新订单追加到本地 orderlist.js 的 raw orders，并更新“这批已经处理过”的时间戳。即使全部被筛掉，也更新时间戳。


1. 第一步停止旧追踪单，是否停止 pending open orders 中所有非 ongoing 的订单，不再限制币种、方向？标记为 ongoing open orders 的订单是否仍保
     留？
答: 是. 是
2. 停止成功后，是否从本地 pending open orders 删除对应记录？如果保留，新流程会在每轮读取 GitHub 前再次请求停止同一订单。
答: 是. 不保留
'''

import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from general import gen_sign
import requests


ORDER_URL = "https://raw.githubusercontent.com/huan00000/price/refs/heads/main/size.txt"
POLL_INTERVAL_SECONDS = 60
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
REQUEST_TIMEOUT_SECONDS = 20
GATE_HOST = "https://api.gateio.ws"
GATE_PREFIX = "/api/v4"
STOP_TRAILING_URL = "/futures/usdt/autoorder/v1/trail/stop"

_BASE_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
ORDER_LIST_PATH = _BASE_DIR / "orderlist.js"
LOCK_PATH = _BASE_DIR / "orderlist.js.lock"

logger = logging.getLogger(__name__)


class OrderDataError(ValueError):
    """远程订单或本地订单文件格式不符合预期。"""


@contextmanager
def _order_file_lock():
    """跨进程独占锁；其他写入脚本也应使用同一个 lock 文件。"""
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            # msvcrt.locking 至少需要一个可锁定字节。
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _parse_remote_orders(text):
    """把 size.txt 内容转换为 (timestamp, orders)。"""
    timestamp_matches = re.findall(r"(?im)^\s*Orders\s+Times\s*:\s*(\d+)\s*$", text)
    if len(timestamp_matches) != 1:
        raise OrderDataError("远程内容必须且只能包含一个 Orders Times")

    field_pattern = re.compile(
        r"(?ims)"
        r"^\s*(?:✨\s*)?Symbol\s*:\s*([^\s]+)\s*$.*?"
        r"^\s*(?:💰\s*)?Price\s*:\s*([^\s]+)(?:\s+USDT)?\s*$.*?"
        r"^\s*(?:📉\s*)?Side\s*:\s*(Open\s+(?:Short|Long))\s*$.*?"
        r"^\s*(?:📦\s*)?Size\s*:\s*([+-]?\d+(?:\.\d+)?)(?:\s+Contracts?)?\s*$.*?"
        r"^\s*(?:📦\s*)?Value\s*:\s*([^\s]+)(?:\s+U)?\s*$.*?"
    )
    matches = list(field_pattern.finditer(text))
    if not matches:
        raise OrderDataError("远程内容中没有可识别的订单")

    timestamp = int(timestamp_matches[0])
    orders = []
    for match in matches:
        contract, price, side, size, value = match.groups()
        orders.append(
            {
                "tag": "raw orders",
                "id": "",
                "timestamp": str(timestamp),
                "Contract": contract,
                "price": price,
                "side": " ".join(side.split()),
                "size": size,
                "value": value,
                "status": "",
            }
        )
    return timestamp, orders


def _load_order_list():
    try:
        with ORDER_LIST_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise OrderDataError(f"无法读取 {ORDER_LIST_PATH}: {exc}") from exc

    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise OrderDataError("orderlist.js 顶层必须是只含一个对象的数组")

    root = data[0]
    required = ("last timestamp", "raw orders", "pending open orders", "finished open orders")
    if any(key not in root for key in required):
        raise OrderDataError("orderlist.js 缺少必要字段")
    list_fields = ("raw orders", "pending open orders", "finished open orders")
    if any(not isinstance(root[field], list) for field in list_fields):
        raise OrderDataError("raw orders、pending open orders 和 finished open orders 必须是数组")
    if not isinstance(root.get("pending close orders", []), list):
        raise OrderDataError("pending close orders 必须是数组")
    try:
        int(root["last timestamp"])
    except (TypeError, ValueError) as exc:
        raise OrderDataError("last timestamp 必须是毫秒时间戳") from exc
    return data


def _write_order_list(data):
    """同目录临时文件写入后原子替换，避免留下半截 JSON。"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=ORDER_LIST_PATH.parent,
            prefix=f".{ORDER_LIST_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=4)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, ORDER_LIST_PATH)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _same_contract_and_side(order, local_order):
    return (
        isinstance(local_order, dict)
        and local_order.get("Contract") == order["Contract"]
        and local_order.get("side") == order["side"]
    )


def _recent_finished_blocks(order, finished_order, now_ms):
    """相同币种和方向、且完成时间处于最近七天内时过滤远程订单。"""
    if not _same_contract_and_side(order, finished_order):
        return False
    try:
        finished_timestamp = int(finished_order["timestamp"])
    except (KeyError, TypeError, ValueError):
        raise OrderDataError("finished open orders 中存在无效 timestamp")
    age_ms = now_ms - finished_timestamp
    return 0 <= age_ms <= SEVEN_DAYS_MS


def _recent_pending_close_blocks(order, pending_order, now_ms):
    """最近七天的同币种平仓记录按 Long/Short 匹配，不区分 Open/Close 或 tag。"""
    if not isinstance(pending_order, dict) or pending_order.get("Contract") != order["Contract"]:
        return False
    directions = {
        "Open Long": "Long", "Close Long": "Long",
        "Open Short": "Short", "Close Short": "Short",
    }
    direction = directions.get(order["side"])
    side = pending_order.get("side")
    if direction is None or not isinstance(side, str) or directions.get(side) != direction:
        return False
    try:
        timestamp = int(pending_order["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OrderDataError("pending close orders 中存在无效 timestamp") from exc
    return 0 <= now_ms - timestamp <= SEVEN_DAYS_MS


def _stop_trailing_order(session, order_id):
    """向 Gate 发出停止追踪订单请求；失败时由调用方中止本轮写入。"""
    try:
        numeric_id = int(str(order_id).strip())
    except (TypeError, ValueError) as exc:
        raise OrderDataError(f"pending open orders 中存在无效 id: {order_id!r}") from exc

    body = json.dumps({"id": numeric_id}, separators=(",", ":"))
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(gen_sign("POST", GATE_PREFIX + STOP_TRAILING_URL, "", body))
    response = session.post(
        GATE_HOST + GATE_PREFIX + STOP_TRAILING_URL,
        headers=headers,
        data=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _process_once(session):
    # 清理独立于远程批次；逐个保存成功结果，后续失败也不会恢复已停订单。
    with _order_file_lock():
        data = _load_order_list()
        root = data[0]
        for pending in list(root["pending open orders"]):
            if pending.get("tag") == "ongoing open orders":
                continue
            order_id = str(pending.get("id", "")).strip()
            if not any(item is pending for item in root["pending open orders"]):
                continue
            _stop_trailing_order(session, order_id)
            root["pending open orders"] = [
                item for item in root["pending open orders"]
                if item.get("tag") == "ongoing open orders"
                or str(item.get("id", "")).strip() != order_id
            ]
            _write_order_list(data)

    response = session.get(ORDER_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    remote_timestamp, remote_orders = _parse_remote_orders(response.text)

    with _order_file_lock():
        data = _load_order_list()
        root = data[0]
        if int(root["last timestamp"]) >= remote_timestamp:
            return 0

        now_ms = int(time.time() * 1000)
        accepted = [
            order
            for order in remote_orders
            if not any(
                _recent_finished_blocks(order, finished, now_ms)
                for finished in root["finished open orders"]
            )
            # 同币种同方向已有 ongoing 订单时，保留旧单并拦截新单。
            and not any(
                _recent_pending_close_blocks(order, pending, now_ms)
                for pending in root.get("pending close orders", [])
            )
            and not any(
                _same_contract_and_side(order, pending)
                and pending.get("tag") == "ongoing open orders"
                for pending in root["pending open orders"]
            )
        ]

        root["raw orders"].extend(accepted)
        # 即使全部订单被过滤，也消费这次远程时间戳。
        root["last timestamp"] = remote_timestamp
        _write_order_list(data)
        return len(accepted)

def process_once(session):
    """供 main.py 调用：处理一轮订单，返回新增数量。"""
    return _process_once(session)


def get_order():
    '''
    '''
    with requests.Session() as session:
        while True:
            try:
                added = _process_once(session)
                logger.info("订单轮询完成，新增 %d 条 raw orders", added)
            except Exception:
                # 单次网络、解析或文件错误不能终止永久轮询。
                logger.exception("订单轮询失败；已保存的停止订单结果保留，下轮重试")
            time.sleep(POLL_INTERVAL_SECONDS)
