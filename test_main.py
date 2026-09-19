import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import requests
import available
import checkstatus
import getorder
import main
import pto
from request_log import LoggedSession


class WorkflowTests(unittest.TestCase):
    def test_full_cycle_and_no_duplicate_close(self):
        for side, size, mode, target in (
            ("Open Long", 37, "dual_long", "104"),
            ("Open Short", -37, "dual_short", "96"),
        ):
            with self.subTest(side=side):
                self._full_cycle(side, size, mode, target)

    def test_full_cycle_replaces_existing_close(self):
        for side, size, mode, target in (
            ("Open Long", 37, "dual_long", "104"),
            ("Open Short", -37, "dual_short", "96"),
        ):
            with self.subTest(side=side):
                self._full_cycle(side, size, mode, target, replace=True)

    def test_full_cycle_stops_old_open_orders_before_fetch(self):
        for side, size, mode, target in (
            ("Open Long", 37, "dual_long", "104"),
            ("Open Short", -37, "dual_short", "96"),
        ):
            with self.subTest(side=side):
                self._full_cycle(side, size, mode, target, cleanup=True)

    def _full_cycle(self, side, size, mode, target, replace=False, cleanup=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory)
            orders = path / "orders.json"
            orders.write_text(json.dumps([{
                "last timestamp": 0, "raw orders": [], "pending open orders": [{
                    "id": "88", "Contract": "ETH_USDT",
                    "side": "Open Short" if size > 0 else "Open Long",
                    "tag": "pending open orders",
                }] if cleanup else [],
                "finished open orders": [], "pending close orders": [{
                    "id": "99", "Contract": "BTC_USDT", "side": side.replace("Open", "Close"),
                    "timestamp": "1",
                    "tag": "pending close orders.inverse 98", "status": "open",
                }] if replace else [], "finished close orders": [],
            }]), encoding="utf-8")
            for module in (getorder, pto, checkstatus):
                stack.enter_context(mock.patch.object(module, "ORDER_LIST_PATH", orders))
                stack.enter_context(mock.patch.object(module, "LOCK_PATH", path / "lock"))
            for module in (getorder, pto, checkstatus, available):
                stack.enter_context(mock.patch.object(module, "gen_sign", return_value={
                    "KEY": "private-key-value", "SIGN": "private-sign-value"}))
            calls = []

            def send(request, **kwargs):
                calls.append(request)
                response = requests.Response()
                response.status_code = 200
                response.request = request
                if request.url == getorder.ORDER_URL:
                    if cleanup:
                        current = json.loads(orders.read_text(encoding="utf-8"))[0]
                        self.assertFalse(any(o["id"] == "88" for o in current["pending open orders"]))
                    body = f"Orders Times: 1\nSymbol: BTC_USDT\nPrice: 100\nSide: {side}\nSize: {10 if size > 0 else -10}\nValue: 1000\n"
                elif request.url.endswith("/trail/stop"):
                    stopped_id = 88 if cleanup else 99
                    self.assertEqual(json.loads(request.body), {"id": stopped_id})
                    body = json.dumps({"id": str(stopped_id), "status": "finished"})
                elif request.method == "POST":
                    response.status_code = 201
                    payload = json.loads(request.body)
                    body = json.dumps({"code": 0, "data": {"id": 2 if payload["reduce_only"] else 1}, "timestamp": "2"})
                elif "/detail?" in request.url:
                    order_id = request.url.split("id=")[1]
                    body = json.dumps({"code": 0, "data": {"order": {
                        "id": order_id, "status_code": "ongoing" if order_id == "99" else "success", "trigger_price": "105",
                    }}})
                elif request.url.endswith("/positions/BTC_USDT"):
                    self.assertEqual(kwargs["timeout"], 20)
                    body = json.dumps([
                        {"contract": "BTC_USDT", "mode": "dual_short" if size > 0 else "dual_long", "size": 0},
                        {"contract": "BTC_USDT", "mode": mode, "size": size,
                         "entry_price": "100", "value": "1000", "initial_margin": "10"},
                    ])
                else:
                    raise AssertionError(request.url)
                response._content = body.encode()
                return response

            stack.enter_context(mock.patch.object(requests.Session, "send", side_effect=send))
            with LoggedSession(path / "logs.md") as session:
                result = main.process_once(session)
                self.assertEqual(result["发布开仓"], 1)
                self.assertEqual(result["发布平仓"], 1)
                saved = json.loads(orders.read_text(encoding="utf-8"))[0]
                self.assertEqual(saved["finished open orders"], [])
                # 多仓 104.131、空仓 95.931，按成交价的整数精度取整。
                self.assertEqual(saved["pending close orders"][0]["price"], target)
                close_payloads = [json.loads(r.body) for r in calls if r.method == "POST"
                                  and json.loads(r.body).get("reduce_only")]
                self.assertEqual(len(close_payloads), 1)
                self.assertEqual(close_payloads[0]["amount"], str(-size))
                self.assertEqual(close_payloads[0]["is_gte"], size > 0)
                self.assertEqual(saved["pending close orders"][0]["side"], side.replace("Open", "Close"))
                self.assertEqual(close_payloads[0]["activation_price"], target)
                self.assertEqual(saved["pending close orders"][0]["tag"], "pending close orders.inverse 1")
                self.assertEqual(main.process_once(session, 2)["发布平仓"], 0)
                saved = json.loads(orders.read_text(encoding="utf-8"))[0]
                self.assertEqual(saved["pending close orders"], [])
                self.assertEqual(len(saved["finished close orders"]), 1)
                self.assertEqual(saved["finished close orders"][0]["tag"], "pending close orders.inverse 1")
            self.assertEqual(sum(r.method == "POST" for r in calls), 2 + int(replace) + int(cleanup))
            if cleanup:
                self.assertTrue(calls[0].url.endswith("/trail/stop"))
                self.assertEqual(calls[1].url, getorder.ORDER_URL)
                self.assertEqual(sum(r.url.endswith("/trail/stop") for r in calls), 1)
            if replace:
                self.assertEqual([r.url.rsplit("/", 1)[-1] for r in calls if r.method == "POST"],
                                 ["create", "stop", "create"])
            self.assertEqual(sum("/positions/" in r.url for r in calls), 1)
            self.assertFalse(any(r.url.endswith("/accounts") for r in calls))
            log = (path / "logs.md").read_text(encoding="utf-8")
            for expected in ("第 1 轮", "第 2 轮", "reduce_only", "id=1", "HTTP 201", "[已脱敏]"):
                self.assertIn(expected, log)
            self.assertNotIn("private-key-value", log)
            self.assertNotIn("private-sign-value", log)

    def test_network_failure_is_logged_before_reraising(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.md"
            with LoggedSession(path) as session, mock.patch.object(
                requests.Session, "send", side_effect=requests.Timeout("timed out")
            ):
                with self.assertRaises(requests.Timeout):
                    session.get("https://example.test/orders?id=1")
            log = path.read_text(encoding="utf-8")
            self.assertIn("id=1", log)
            self.assertIn("网络失败", log)

    def test_fetch_failure_does_not_block_existing_orders(self):
        session = mock.Mock()
        with mock.patch.object(getorder, "process_once", side_effect=ValueError("bad data")), \
             mock.patch.object(pto, "pto", return_value=1) as opened, \
             mock.patch.object(checkstatus, "process_once", return_value={}) as checked, \
             mock.patch.object(pto, "inverse_pto", return_value=1) as closed:
            with self.assertLogs(level="ERROR"):
                result = main.process_once(session)
            self.assertEqual(result["拉取订单"], {"error": "ValueError"})
            for action in (opened, checked, closed):
                action.assert_called_once_with(session)

    def test_cleanup_failure_preserves_progress_and_continues_other_stages(self):
        for failure in ("fetch", "stop"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                path = Path(directory)
                orders = path / "orders.json"
                pending = [
                    {"id": "81", "tag": "pending open orders"},
                    {"id": "82", "tag": "pending open orders"},
                    {"id": "83", "tag": "ongoing open orders"},
                ]
                orders.write_text(json.dumps([{
                    "last timestamp": 7, "raw orders": [],
                    "pending open orders": pending, "finished open orders": [],
                    "pending close orders": [], "finished close orders": [],
                }]), encoding="utf-8")
                stack.enter_context(mock.patch.object(getorder, "ORDER_LIST_PATH", orders))
                stack.enter_context(mock.patch.object(getorder, "LOCK_PATH", path / "lock"))
                stack.enter_context(mock.patch.object(getorder, "gen_sign", return_value={}))
                session = mock.Mock()
                if failure == "stop":
                    failed = mock.Mock()
                    failed.raise_for_status.side_effect = requests.HTTPError("stop failed")
                    session.post.side_effect = [mock.Mock(), failed]
                else:
                    session.get.side_effect = requests.Timeout("fetch failed")
                stages = []

                def remaining_stage(name):
                    def run(actual_session):
                        self.assertIs(actual_session, session)
                        saved = json.loads(orders.read_text(encoding="utf-8"))[0]
                        self.assertEqual(saved["pending open orders"], pending[1:] if failure == "stop" else pending[2:])
                        self.assertEqual(saved["last timestamp"], 7)
                        self.assertEqual(saved["raw orders"], [])
                        stages.append(name)
                        return 0
                    return run

                for module, action in ((pto, "pto"), (checkstatus, "process_once"), (pto, "inverse_pto")):
                    stack.enter_context(mock.patch.object(module, action, side_effect=remaining_stage(action)))
                with self.assertLogs(level="ERROR"):
                    result = main.process_once(session)
                self.assertEqual(result["拉取订单"], {"error": "HTTPError" if failure == "stop" else "Timeout"})
                self.assertEqual(stages, ["pto", "process_once", "inverse_pto"])
                self.assertEqual([json.loads(call.kwargs["data"])["id"] for call in session.post.call_args_list], [81, 82])
                if failure == "stop":
                    session.get.assert_not_called()
                else:
                    session.get.assert_called_once_with(getorder.ORDER_URL, timeout=getorder.REQUEST_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
