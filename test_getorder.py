import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock

import getorder


VALID_REMOTE_TEXT = """\
🚀 ====== Order Alert ======
✨  Symbol     : DOGE_USDT
💰  Price      : 0.09707 USDT
📉  Side       : Open Short
📦  Size       : -142 Contracts
📦  Value      : 138.55 U
   ======================

Orders Times: 1788869865421
"""


class ParseRemoteOrdersTests(unittest.TestCase):
    def test_parses_order_with_required_value_on_following_line(self):
        timestamp, orders = getorder._parse_remote_orders(VALID_REMOTE_TEXT)

        self.assertEqual(timestamp, 1788869865421)
        self.assertEqual(
            orders,
            [
                {
                    "tag": "raw orders",
                    "id": "",
                    "timestamp": "1788869865421",
                    "Contract": "DOGE_USDT",
                    "price": "0.09707",
                    "side": "Open Short",
                    "size": "-142",
                    "value": "138.55",
                    "status": "",
                }
            ],
        )

    def test_rejects_order_without_required_value(self):
        text = VALID_REMOTE_TEXT.replace("📦  Value      : 138.55 U\n", "")

        with self.assertRaisesRegex(getorder.OrderDataError, "没有可识别的订单"):
            getorder._parse_remote_orders(text)


class ProcessOnceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        temp_path = Path(self.temp_dir.name)
        self.order_path = temp_path / "orderlist.js"
        self.lock_path = temp_path / "orderlist.js.lock"

    def _write_orders(self, root):
        self.order_path.write_text(json.dumps([root]), encoding="utf-8")

    def _session(self):
        response = mock.Mock(text=VALID_REMOTE_TEXT)
        response.raise_for_status.return_value = None
        session = mock.Mock()
        session.get.return_value = response
        session.post.return_value.raise_for_status.return_value = None
        return session

    def test_stops_matching_pending_open_before_adding_raw_order(self):
        self._write_orders({
            "last timestamp": 0,
            "raw orders": [],
            "pending open orders": [{
                "id": "10282989", "timestamp": "1788869000000",
                "Contract": "DOGE_USDT", "side": "Open Short",
            }],
            "finished open orders": [],
        })
        session = self._session()

        with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
             mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
             mock.patch.object(getorder, "gen_sign", return_value={"SIGN": "test"}):
            added = getorder._process_once(session)

        self.assertEqual(added, 1)
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.kwargs["data"], '{"id":10282989}')
        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(len(saved["raw orders"]), 1)

    def test_recent_finished_open_filters_order_without_stopping_open(self):
        now_ms = 1788869900000
        self._write_orders({
            "last timestamp": 0,
            "raw orders": [],
            "pending open orders": [{
                "id": "10282989", "timestamp": str(now_ms),
                "Contract": "DOGE_USDT", "side": "Open Short",
            }],
            "finished open orders": [{
                "id": "9", "timestamp": str(now_ms - 1000),
                "Contract": "DOGE_USDT", "side": "Open Short",
            }],
        })
        session = self._session()

        with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
             mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
             mock.patch.object(getorder.time, "time", return_value=now_ms / 1000):
            added = getorder._process_once(session)

        self.assertEqual(added, 0)
        session.post.assert_not_called()
        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["raw orders"], [])
        self.assertEqual(saved["last timestamp"], 1788869865421)


if __name__ == "__main__":
    unittest.main()
