import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

import pto


class PtoResponseTests(unittest.TestCase):
    def session(self, status=200, code=0):
        response = Mock(status_code=status)
        response.json.return_value = {
            "code": code, "message": "ok", "data": {"id": "1100058"},
            "timestamp": 1788957279655,
        }
        response.text = pto.json.dumps(response.json.return_value)
        return Mock(post=Mock(return_value=response))

    @patch.object(pto, "gen_sign", return_value={})
    def test_success_statuses(self, sign):
        for status in (200, 201):
            with self.subTest(status=status):
                self.assertEqual(pto._create_trailing_order(self.session(status), {}),
                                 ("1100058", "1788957279655"))

    @patch.object(pto, "gen_sign", return_value={})
    def test_failure_responses(self, sign):
        for status, code in ((400, 0), (500, 0), (200, 1)):
            with self.subTest(status=status, code=code):
                with self.assertRaises(pto.PtoError):
                    pto._create_trailing_order(self.session(status, code), {})

    @patch.object(pto, "gen_sign", return_value={})
    def test_business_failure_without_data(self, sign):
        session = self.session()
        response = session.post.return_value
        response.json.return_value = {
            "code": -1, "message": "Invalid request", "timestamp": 1789737236268,
        }
        response.text = pto.json.dumps(response.json.return_value)
        with self.assertRaisesRegex(pto.PtoError, "code=-1, message=Invalid request") as caught:
            pto._create_trailing_order(session, {})
        self.assertIsNone(caught.exception.__cause__)
        session.post.assert_called_once()

    @patch.object(pto, "gen_sign", return_value={})
    def test_success_with_missing_fields_is_rejected(self, sign):
        for data in (None, {}, {"id": None}, {"id": " "}):
            with self.subTest(data=data):
                session = self.session()
                session.post.return_value.json.return_value["data"] = data
                with self.assertRaisesRegex(pto.PtoError, "响应格式无效"):
                    pto._create_trailing_order(session, {})

    @patch.object(pto, "gen_sign", return_value={})
    def test_inverse_rejection_does_not_write_pending_order(self, sign):
        order = {"id": "123", "Contract": "BTC_USDT", "price": "100",
                 "side": "Open Long", "size": "10", "value": "1000"}
        root = {"finished open orders": [order], "pending close orders": [],
                "finished close orders": []}
        session = self.session(code=-1)
        del session.post.return_value.json.return_value["data"]
        with patch.object(pto, "_order_file_lock"), \
                patch.object(pto, "_load_order_list", return_value=[root]), \
                patch.object(pto, "gavailable", return_value=10), \
                patch.object(pto, "_write_order_list") as write, \
                self.assertLogs(pto.logger, level="ERROR"):
            self.assertEqual(pto.inverse_pto(session), 0)
            self.assertEqual(root["pending close orders"], [])
            self.assertEqual(root["finished open orders"], [order])
            write.assert_not_called()
        payload = pto.json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(payload["position_mode"], "dual_plus")
        self.assertEqual(payload["pos_margin_mode"], "cross")
        self.assertTrue(payload["reduce_only"])

    @patch.object(pto, "gen_sign", return_value={})
    def test_http_200_moves_raw_order_to_pending(self, sign):
        order = {"Contract": "DOGE_USDT", "price": "0.1", "side": "Open Short",
                 "size": "-10", "value": "-1"}
        root = {"raw orders": [order], "pending open orders": []}
        session = self.session()
        with patch.object(pto, "_order_file_lock"), \
                patch.object(pto, "_load_order_list", return_value=[root]), \
                patch.object(pto, "_write_order_list") as write:
            self.assertEqual(pto.pto(session), 1)
            self.assertEqual(root["raw orders"], [])
            self.assertEqual(root["pending open orders"][0]["id"], "1100058")
            self.assertEqual(root["pending open orders"][0]["timestamp"], "1788957279655")
            write.assert_called_once_with([root])
            session.post.assert_called_once()


class PtoCalculationTests(unittest.TestCase):
    def test_open_payload_direction(self):
        order = {"Contract": "DOGE_USDT", "price": "0.1", "side": "Open Short", "size": "-10"}
        payload = pto._open_payload(order)
        self.assertTrue(payload["is_gte"])
        self.assertFalse(payload["reduce_only"])

    def test_short_close_is_inverse_and_uses_target_price(self):
        order = {"Contract": "DOGE_USDT", "price": "100", "side": "Open Short",
                 "size": "-10", "value": "-1000"}
        side, target, payload = pto._close_details(order, Decimal("10"))
        self.assertEqual((side, target), ("Close Short", "99.969"))
        self.assertEqual(payload["amount"], "10")
        self.assertFalse(payload["is_gte"])

    def test_long_close_is_inverse(self):
        order = {"Contract": "BTC_USDT", "price": "100", "side": "Open Long",
                 "size": "10", "value": "1000"}
        side, target, payload = pto._close_details(order, Decimal("10"))
        self.assertEqual((side, target), ("Close Long", "100.031"))
        self.assertEqual(payload["amount"], "-10")
        self.assertTrue(payload["is_gte"])

    def test_non_positive_target_is_rejected(self):
        with self.assertRaisesRegex(pto.PtoError, "必须大于 0"):
            pto._target_price({"price": "1", "value": "-1"}, 100)

    def test_pending_record_requires_value(self):
        source = {"Contract": "BTC_USDT"}
        with self.assertRaisesRegex(pto.PtoError, "value"):
            pto._pending_record(source, "pending open orders", "1", "2",
                                "Open Long", "1", "100")


if __name__ == "__main__":
    unittest.main()
