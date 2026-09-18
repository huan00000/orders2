"""发布追踪委托，并在成功后更新 orderlist.js。
文件锁与 getorder.py 共用
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import requests

from available import get_cross_available as gavailable
from general import gen_sign

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"
CREATE_URL = "/futures/usdt/autoorder/v1/trail/create"
POLL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 20
PRICE_OFFSET = "1%"
POS_MARGIN_MODE = "cross"
POSITION_MODE = "dual_plus"
_BASE_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
ORDER_LIST_PATH = _BASE_DIR / "orderlist.js"
LOCK_PATH = _BASE_DIR / "orderlist.js.lock"
logger = logging.getLogger(__name__)


class PtoError(RuntimeError):
    """订单数据、API 响应或本地订单文件不符合预期。"""


@contextmanager
def _order_file_lock():
    """与 getorder.py 共用同一个跨进程锁。"""
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+b") as lock_file:
        if os.name == "nt":
            import msvcrt

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


def _load_order_list():
    try:
        with ORDER_LIST_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise PtoError(f"无法读取 {ORDER_LIST_PATH}: {exc}") from exc
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise PtoError("orderlist.js 顶层必须是只含一个对象的数组")
    required = ("raw orders", "pending open orders", "finished open orders",
                "pending close orders", "finished close orders")
    if any(not isinstance(data[0].get(name), list) for name in required):
        raise PtoError("orderlist.js 缺少必要的订单数组")
    return data


def _write_order_list(data):
    """写入同目录临时文件后原子替换。"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=ORDER_LIST_PATH.parent,
            prefix=f".{ORDER_LIST_PATH.name}.", suffix=".tmp", delete=False,
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


def _required_text(order, field):
    value = order.get(field)
    if value is None or str(value).strip() == "":
        raise PtoError(f"订单缺少 {field}")
    return str(value).strip()


def _create_trailing_order(session, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(gen_sign("POST", PREFIX + CREATE_URL, "", body))
    response = session.post(HOST + PREFIX + CREATE_URL, headers=headers, data=body,
                            timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code not in (200, 201):
        raise PtoError(f"创建订单失败：HTTP {response.status_code}: {response.text}")
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("响应 JSON 不是对象")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PtoError(f"创建订单响应格式无效: {response.text}") from exc
    # HTTP 成功不代表业务成功；失败响应通常不包含 data。
    if result.get("code") != 0:
        raise PtoError(
            f"创建订单被接口拒绝: code={result.get('code')}, "
            f"message={result.get('message')}, timestamp={result.get('timestamp')}; "
            f"请求参数={body}"
        )
    try:
        order_id = _required_text(result["data"], "id")
        timestamp = _required_text(result, "timestamp")
    except (KeyError, TypeError, AttributeError, PtoError) as exc:
        raise PtoError(f"创建订单响应格式无效: {response.text}") from exc
    return order_id, timestamp


def _open_payload(order):
    side = _required_text(order, "side")
    if side not in ("Open Long", "Open Short"):
        raise PtoError(f"不支持的开仓方向: {side}")
    return {
        "reduce_only": False, "contract": _required_text(order, "Contract"),
        "amount": _required_text(order, "size"),
        "activation_price": _required_text(order, "price"),
        "is_gte": side == "Open Short", "price_type": 3,
        "price_offset": PRICE_OFFSET, "text": "apiv4",
        "pos_margin_mode": POS_MARGIN_MODE, "position_mode": POSITION_MODE
    }


def _decimal_text(number):
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _target_price(order, available):
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


def _close_details(order, available):
    mapping = {"Open Short": ("Close Short", False), "Open Long": ("Close Long", True)}
    side = _required_text(order, "side")
    if side not in mapping:
        raise PtoError(f"不支持的已完成开仓方向: {side}")
    try:
        close_size = -Decimal(_required_text(order, "size"))
    except InvalidOperation as exc:
        raise PtoError("size 必须是有效数字") from exc
    if close_size == 0:
        raise PtoError("size 不能为 0")
    close_side, is_gte = mapping[side]
    target = Decimal(_target_price(order, available))
    # 按对应 finished open order 的 price 小数位数四舍五入，并保留末尾零。
    price = Decimal(_required_text(order, "price"))
    step = Decimal("1").scaleb(min(price.as_tuple().exponent, 0))
    target = target.quantize(step, rounding=ROUND_HALF_UP)
    if target <= 0:
        raise PtoError(f"按 price 精度取整后的 target price 必须大于 0，实际为 {target}")
    target = format(target, "f")
    payload = {
        "reduce_only": True, "contract": _required_text(order, "Contract"),
        "amount": _decimal_text(close_size), "activation_price": target,
        "is_gte": is_gte, "price_type": 3, "price_offset": PRICE_OFFSET, "text": "apiv4",
        "pos_margin_mode": POS_MARGIN_MODE, "position_mode": POSITION_MODE,
    }
    return close_side, target, payload


def _pending_record(source, tag, order_id, timestamp, side, size, price):
    return {
        "tag": tag, "id": order_id, "timestamp": timestamp,
        "Contract": _required_text(source, "Contract"), "price": price,
        "side": side, "size": size, "value": _required_text(source, "value"),
        "status": "open",
    }


def pto(session=None):
    """发布一轮 raw orders，成功后才移至 pending open orders。"""
    owns_session = session is None
    session = session or requests.Session()
    published = 0
    try:
        with _order_file_lock():
            data = _load_order_list()
            root = data[0]
            for order in list(root["raw orders"]):
                try:
                    payload = _open_payload(order)
                    # 所有写入 pending 所需字段必须在真实下单前完成校验。
                    source_side = _required_text(order, "side")
                    source_size = _required_text(order, "size")
                    source_price = _required_text(order, "price")
                    _required_text(order, "value")
                    order_id, timestamp = _create_trailing_order(session, payload)
                    pending = _pending_record(
                        order, "pending open orders", order_id, timestamp,
                        source_side, source_size, source_price,
                    )
                    root["raw orders"].remove(order)
                    root["pending open orders"].append(pending)
                    _write_order_list(data)
                    published += 1
                except Exception:
                    logger.exception("开仓订单发布失败，原始订单保留: %r", order)
        return published
    finally:
        if owns_session:
            session.close()


def _has_inverse(root, source_id):
    expected_tag = f"pending close orders.inverse {source_id}"
    return any(isinstance(order, dict) and order.get("tag") == expected_tag
               for name in ("pending close orders", "finished close orders")
               for order in root[name])


def inverse_pto(session=None):
    """为尚无对应平仓单的 finished open orders 发布一轮反向委托。"""
    owns_session = session is None
    session = session or requests.Session()
    published = 0
    try:
        with _order_file_lock():
            data = _load_order_list()
            root = data[0]
            candidates = []
            for order in root["finished open orders"]:
                if not isinstance(order, dict):
                    logger.error("忽略无效的 finished open order: %r", order)
                    continue
                try:
                    source_id = _required_text(order, "id")
                except PtoError:
                    logger.exception("忽略缺少 id 的 finished open order: %r", order)
                    continue
                if not _has_inverse(root, source_id):
                    candidates.append(order)
            if not candidates:
                return 0
            available = gavailable(session)
            for order in candidates:
                try:
                    source_id = _required_text(order, "id")
                    close_side, target, payload = _close_details(order, available)
                    order_id, timestamp = _create_trailing_order(session, payload)
                    pending = _pending_record(
                        order, f"pending close orders.inverse {source_id}", order_id,
                        timestamp, close_side, payload["amount"], target,
                    )
                    root["pending close orders"].append(pending)
                    _write_order_list(data)
                    published += 1
                except Exception:
                    logger.exception("平仓订单发布失败，已完成开仓订单保留: %r", order)
        return published
    finally:
        if owns_session:
            session.close()


def process_once(session):
    """按顺序完成一轮开仓发布和平仓发布。"""
    return pto(session), inverse_pto(session)


def run_forever():
    """每 60 秒处理一轮；单轮异常不会终止进程。"""
    with requests.Session() as session:
        while True:
            try:
                opened, closed = process_once(session)
                logger.info("订单发布轮询完成：开仓 %d，平仓 %d", opened, closed)
            except Exception:
                logger.exception("订单发布轮询失败")
            time.sleep(POLL_INTERVAL_SECONDS)

def main():
    logging.basicConfig(level=logging.INFO)
    run_forever()
    


if __name__ == "__main__":
    main()
