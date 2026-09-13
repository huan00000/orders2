"""轮询 Gate 追踪委托状态，并同步更新 orderlist.js。"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import requests

from general import gen_sign


HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"
DETAIL_URL = "/futures/usdt/autoorder/v1/trail/detail"
POLL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 20

_BASE_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
ORDER_LIST_PATH = _BASE_DIR / "orderlist.js"
LOCK_PATH = _BASE_DIR / "orderlist.js.lock"

PENDING_TO_FINISHED = {
    "pending open orders": "finished open orders",
    "pending close orders": "finished close orders",
}

logger = logging.getLogger(__name__)


class CheckStatusError(RuntimeError):
    """订单文件或 Gate API 响应不符合预期。"""


@contextmanager
def _order_file_lock():
    """与 getorder.py、pto.py 共用同一个跨进程独占锁。"""
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
        raise CheckStatusError(f"无法读取 {ORDER_LIST_PATH}: {exc}") from exc

    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise CheckStatusError("orderlist.js 顶层必须是只含一个对象的数组")
    for pending_field, finished_field in PENDING_TO_FINISHED.items():
        if not isinstance(data[0].get(pending_field), list):
            raise CheckStatusError(f"orderlist.js 缺少数组字段: {pending_field}")
        if not isinstance(data[0].get(finished_field), list):
            raise CheckStatusError(f"orderlist.js 缺少数组字段: {finished_field}")
    return data


def _write_order_list(data):
    """先完整写入同目录临时文件，再原子替换订单文件。"""
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


def _required_order_id(order):
    if not isinstance(order, dict):
        raise CheckStatusError(f"pending orders 中存在非对象数据: {order!r}")
    order_id = str(order.get("id", "")).strip()
    if not order_id:
        raise CheckStatusError(f"pending order 缺少 id: {order!r}")
    return order_id


def _fetch_status(session, order_id):
    """查询单个委托；只接受明确且成功的 API 响应。"""
    query = urlencode({"id": order_id})
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(gen_sign("GET", PREFIX + DETAIL_URL, query))
    response = session.get(
        f"{HOST}{PREFIX}{DETAIL_URL}?{query}", headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise CheckStatusError(
            f"查询订单 {order_id} 失败：HTTP {response.status_code}: {response.text}"
        )
    try:
        result = response.json()
        returned_order = result["data"]["order"]
        returned_id = str(returned_order["id"]).strip()
        status = returned_order["status"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CheckStatusError(f"订单 {order_id} 的响应格式无效: {response.text}") from exc

    if not isinstance(result, dict) or result.get("code") != 0:
        raise CheckStatusError(f"订单 {order_id} 的 API 响应未表示成功: {result!r}")
    if returned_id != order_id:
        raise CheckStatusError(f"订单 ID 不匹配：请求 {order_id}，返回 {returned_id}")
    if not isinstance(status, str) or not status.strip():
        raise CheckStatusError(f"订单 {order_id} 的 status 无效: {status!r}")
    return status.strip().lower()


def _process_once(session):
    """处理一轮状态同步，返回 (仍为 open, 已完成, 已删除, 查询失败)。"""
    counts = {"open": 0, "finished": 0, "deleted": 0, "failed": 0}
    changed = False

    # 查询期间保持文件锁，避免其他脚本移动订单后，本轮用旧快照覆盖新数据。
    with _order_file_lock():
        data = _load_order_list()
        root = data[0]
        for pending_field, finished_field in PENDING_TO_FINISHED.items():
            for order in list(root[pending_field]):
                try:
                    order_id = _required_order_id(order)
                    status = _fetch_status(session, order_id)
                except Exception:
                    counts["failed"] += 1
                    logger.exception("查询 %s 中的订单失败，保留本地数据: %r", pending_field, order)
                    continue

                if status == "open":
                    counts["open"] += 1
                elif status == "finished":
                    order["status"] = "finished"
                    root[pending_field].remove(order)
                    root[finished_field].append(order)
                    counts["finished"] += 1
                    changed = True
                else:
                    root[pending_field].remove(order)
                    counts["deleted"] += 1
                    changed = True
                    logger.warning("订单 %s 状态为 %s，已从 %s 删除", order_id, status, pending_field)

        if changed:
            _write_order_list(data)
    return counts


def process_once(session):
    """供 main.py 调用：同步一轮状态并返回分类计数。"""
    return _process_once(session)


def checkstatus(session=None):
    """每 60 秒持续同步订单状态；单轮失败不会终止进程。"""
    owns_session = session is None
    session = session or requests.Session()
    try:
        while True:
            try:
                counts = _process_once(session)
                logger.info(
                    "状态轮询完成：open=%d，finished=%d，deleted=%d，failed=%d",
                    counts["open"], counts["finished"], counts["deleted"], counts["failed"],
                )
            except Exception:
                logger.exception("订单状态轮询失败，本轮未完成")
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        if owns_session:
            session.close()


def main():
    logging.basicConfig(level=logging.INFO)
    checkstatus()


if __name__ == "__main__":
    main()
