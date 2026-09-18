'''
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
        ]

        stopped_ids = set()
        for order in accepted:
            for pending in root["pending open orders"]:
                if not _same_contract_and_side(order, pending):
                    continue
                if pending.get("tag") != "ongoing open orders":
                    continue
                order_id = str(pending.get("id", "")).strip()
                if order_id not in stopped_ids:
                    _stop_trailing_order(session, order_id)
                    stopped_ids.add(order_id)

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
                logger.exception("订单轮询失败；本地订单文件未被本次操作覆盖")
            time.sleep(POLL_INTERVAL_SECONDS)
