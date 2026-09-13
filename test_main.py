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
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory)
            orders = path / "orders.json"
            orders.write_text(json.dumps([{
                "last timestamp": 0, "raw orders": [], "pending open orders": [],
                "finished open orders": [], "pending close orders": [], "finished close orders": [],
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
                    body = "Orders Times: 1\nSymbol: BTC_USDT\nPrice: 100\nSide: Open Long\nSize: 10\nValue: 1000\n"
                elif request.method == "POST":
                    response.status_code = 201
                    payload = json.loads(request.body)
                    body = json.dumps({"code": 0, "data": {"id": 2 if payload["reduce_only"] else 1}, "timestamp": "2"})
                elif "/detail?" in request.url:
                    order_id = request.url.split("id=")[1]
                    body = json.dumps({"code": 0, "data": {"order": {"id": order_id, "status": "finished"}}})
                elif request.url.endswith("/accounts"):
                    self.assertEqual(kwargs["timeout"], 20)
                    body = '{"cross_available": "10"}'
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
                self.assertEqual(len(saved["finished open orders"]), 1)
                self.assertEqual(saved["pending close orders"][0]["tag"], "pending close orders.inverse 1")
                self.assertEqual(main.process_once(session, 2)["发布平仓"], 0)
            self.assertEqual(sum(r.method == "POST" for r in calls), 2)
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


if __name__ == "__main__":
    unittest.main()
