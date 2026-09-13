import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import checkstatus


def order(order_id, status="open"):
    return {"tag": "pending", "id": order_id, "Contract": "BTC_USDT", "status": status}


class CheckStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.order_path = directory / "orderlist.js"
        self.lock_path = directory / "orderlist.js.lock"

    def _write(self, root):
        self.order_path.write_text(json.dumps([root]), encoding="utf-8")

    def _run(self, responses):
        session = mock.Mock()
        session.get.side_effect = responses
        with mock.patch.object(checkstatus, "ORDER_LIST_PATH", self.order_path), \
             mock.patch.object(checkstatus, "LOCK_PATH", self.lock_path), \
             mock.patch.object(checkstatus, "gen_sign", return_value={"SIGN": "test"}):
            result = checkstatus._process_once(session)
        saved = json.loads(self.order_path.read_text(encoding="utf-8"))[0]
        return result, saved, session

    @staticmethod
    def _response(order_id, status, http_status=200, code=0):
        response = mock.Mock(status_code=http_status, text="response")
        response.json.return_value = {
            "code": code, "message": "ok",
            "data": {"order": {"id": order_id, "status": status}},
        }
        return response

    def test_open_is_retained_and_finished_moves_to_matching_list(self):
        self._write({
            "pending open orders": [order("1"), order("2")],
            "finished open orders": [],
            "pending close orders": [order("3")],
            "finished close orders": [],
        })
        result, saved, session = self._run([
            self._response("1", "open"),
            self._response("2", "finished"),
            self._response("3", "finished"),
        ])

        self.assertEqual(result, {"open": 1, "finished": 2, "deleted": 0, "failed": 0})
        self.assertEqual([item["id"] for item in saved["pending open orders"]], ["1"])
        self.assertEqual(saved["finished open orders"][0]["status"], "finished")
        self.assertEqual(saved["finished close orders"][0]["id"], "3")
        self.assertIn("?id=1", session.get.call_args_list[0].args[0])

    def test_explicit_other_status_is_deleted(self):
        self._write({
            "pending open orders": [order("4")], "finished open orders": [],
            "pending close orders": [], "finished close orders": [],
        })
        result, saved, _ = self._run([self._response("4", "cancelled")])

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(saved["pending open orders"], [])

    def test_http_or_malformed_failure_retains_local_orders(self):
        self._write({
            "pending open orders": [order("5"), order("6")], "finished open orders": [],
            "pending close orders": [], "finished close orders": [],
        })
        malformed = mock.Mock(status_code=200, text="bad")
        malformed.json.return_value = {"code": 0, "data": {}}
        result, saved, _ = self._run([
            self._response("5", "unknown", http_status=500), malformed,
        ])

        self.assertEqual(result["failed"], 2)
        self.assertEqual([item["id"] for item in saved["pending open orders"]], ["5", "6"])


if __name__ == "__main__":
    unittest.main()
