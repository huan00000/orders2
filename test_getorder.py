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

    def test_stops_all_non_ongoing_pending_once_before_fetch(self):
        self._write_orders({
            "last timestamp": 0,
            "raw orders": [],
            "pending open orders": [{
                "id": "10282989", "timestamp": "1788869000000",
                "Contract": "DOGE_USDT", "side": "Open Short",
            }, {
                "id": "10282990", "Contract": "BTC_USDT", "side": "Open Short",
            }, {
                "id": "10282991", "Contract": "DOGE_USDT", "side": "Open Long",
            }, {
                "id": "10282989", "Contract": "DOGE_USDT", "side": "Open Short",
            }],
            "finished open orders": [],
        })
        session = self._session()

        with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
             mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
             mock.patch.object(getorder, "gen_sign", return_value={"SIGN": "test"}):
            added = getorder._process_once(session)

        self.assertEqual(added, 1)
        self.assertEqual(
            [json.loads(call.kwargs["data"])["id"] for call in session.post.call_args_list],
            [10282989, 10282990, 10282991],
        )
        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(len(saved["raw orders"]), 1)
        self.assertEqual(saved["pending open orders"], [])
        methods = [call[0] for call in session.method_calls]
        self.assertLess(max(i for i, name in enumerate(methods) if name == "post"), methods.index("get"))

    def test_recent_finished_open_filters_after_stopping_pending(self):
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
             mock.patch.object(getorder.time, "time", return_value=now_ms / 1000), \
             mock.patch.object(getorder, "gen_sign", return_value={"SIGN": "test"}):
            added = getorder._process_once(session)

        self.assertEqual(added, 0)
        session.post.assert_called_once()
        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["raw orders"], [])
        self.assertEqual(saved["last timestamp"], 1788869865421)

    def test_cleanup_survives_old_batch_or_fetch_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self._write_orders({
                    "last timestamp": 1788869865421, "raw orders": [],
                    "pending open orders": [{"id": "101"}],
                    "finished open orders": [],
                })
                session = self._session()
                if failure:
                    session.get.side_effect = getorder.requests.ConnectionError("offline")
                with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
                     mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
                     mock.patch.object(getorder, "gen_sign", return_value={}):
                    if failure:
                        with self.assertRaises(getorder.requests.ConnectionError):
                            getorder.process_once(session)
                    else:
                        self.assertEqual(getorder.process_once(session), 0)
                    session.get.side_effect = None
                    self.assertEqual(getorder.process_once(session), 0)
                session.post.assert_called_once()
                saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
                self.assertEqual(saved["pending open orders"], [])
                self.assertEqual(saved["last timestamp"], 1788869865421)
                self.assertEqual(saved["raw orders"], [])

    def test_partial_stop_failure_keeps_only_unstopped_records(self):
        self._write_orders({
            "last timestamp": 0, "raw orders": [],
            "pending open orders": [{"id": "101"}, {"id": "102"}],
            "finished open orders": [],
        })
        session = self._session()
        ok = mock.Mock()
        failed = mock.Mock()
        failed.raise_for_status.side_effect = getorder.requests.HTTPError("failed")
        session.post.side_effect = [ok, failed]
        with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
             mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
             mock.patch.object(getorder, "gen_sign", return_value={}):
            with self.assertRaises(getorder.requests.HTTPError):
                getorder.process_once(session)
        session.get.assert_not_called()
        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["pending open orders"], [{"id": "102"}])
        self.assertEqual(saved["last timestamp"], 0)
        self.assertEqual(saved["raw orders"], [])

    def test_ongoing_blocks_only_same_contract_and_side(self):
        for contract, side, blocked in [
            ("DOGE_USDT", "Open Short", True),
            ("DOGE_USDT", "Open Long", False),
            ("BTC_USDT", "Open Short", False),
        ]:
            with self.subTest(contract=contract, side=side):
                pending = [
                    {"id": "101", "Contract": "DOGE_USDT", "side": "Open Short",
                     "tag": "pending open orders"},
                    {"id": "102", "Contract": contract, "side": side,
                     "tag": "ongoing open orders"},
                ]
                self._write_orders({
                    "last timestamp": 0, "raw orders": [],
                    "pending open orders": pending, "finished open orders": [],
                })
                session = self._session()
                with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
                     mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
                     mock.patch.object(getorder, "gen_sign", return_value={}):
                    added = getorder._process_once(session)

                self.assertEqual(added, 0 if blocked else 1)
                session.post.assert_called_once()
                self.assertEqual(json.loads(session.post.call_args.kwargs["data"]), {"id": 101})
                saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
                self.assertEqual(len(saved["raw orders"]), added)
                self.assertEqual(saved["pending open orders"], [pending[1]])
                self.assertEqual(saved["last timestamp"], 1788869865421)

    def test_pending_close_seven_day_rule(self):
        now_ms = 1788869900000
        cases = [
            ("DOGE_USDT", "Close Short", 0, True),
            ("DOGE_USDT", "Open Short", 1000, True),
            ("DOGE_USDT", "Close Short", getorder.SEVEN_DAYS_MS, True),
            ("DOGE_USDT", "Close Short", getorder.SEVEN_DAYS_MS + 1, False),
            ("DOGE_USDT", "Close Short", -1, False),
            ("DOGE_USDT", "Close Long", 1000, False),
            ("BTC_USDT", "Close Short", 1000, False),
        ]
        for contract, side, age, blocked in cases:
            for tag in (None, "pending close orders.inverse 1", "ongoing close orders"):
                for direction in ("Short", "Long"):
                    with self.subTest(contract=contract, side=side, age=age, tag=tag, direction=direction):
                        local_side = side if direction == "Short" else side.replace("Short", "TEMP").replace("Long", "Short").replace("TEMP", "Long")
                        pending_close = {
                            "Contract": contract, "side": local_side,
                            "timestamp": str(now_ms - age), "price": "999", "size": "999",
                        }
                        if tag is not None:
                            pending_close["tag"] = tag
                        pending_open = [{"id": "101", "Contract": "DOGE_USDT", "side": f"Open {direction}"}]
                        self._write_orders({
                            "last timestamp": 0, "raw orders": [],
                            "pending open orders": pending_open, "finished open orders": [],
                            "pending close orders": [pending_close],
                        })
                        session = self._session()
                        session.get.return_value.text = VALID_REMOTE_TEXT.replace("Open Short", f"Open {direction}")
                        with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
                             mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
                             mock.patch.object(getorder.time, "time", return_value=now_ms / 1000), \
                             mock.patch.object(getorder, "gen_sign", return_value={}):
                            added = getorder.process_once(session)
                        self.assertEqual(added, 0 if blocked else 1)
                        self.assertEqual(session.post.call_count, 1)
                        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
                        self.assertEqual(len(saved["raw orders"]), added)
                        self.assertEqual(saved["pending close orders"], [pending_close])
                        self.assertEqual(saved["pending open orders"], [])
                        self.assertEqual(saved["last timestamp"], 1788869865421)

    def test_close_side_does_not_match_old_open_rules_and_finished_close_is_ignored(self):
        now_ms = 1788869900000
        close = {"Contract": "DOGE_USDT", "side": "Close Short", "timestamp": str(now_ms)}
        self._write_orders({
            "last timestamp": 0, "raw orders": [],
            "pending open orders": [dict(close, tag="ongoing open orders")],
            "finished open orders": [close], "finished close orders": [close],
        })
        session = self._session()
        with mock.patch.object(getorder, "ORDER_LIST_PATH", self.order_path), \
             mock.patch.object(getorder, "LOCK_PATH", self.lock_path), \
             mock.patch.object(getorder.time, "time", return_value=now_ms / 1000):
            self.assertEqual(getorder.process_once(session), 1)
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
