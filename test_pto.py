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
        self.assertEqual((side, target), ("Close Short", "96.9"))
        self.assertEqual(payload["amount"], "10")
        self.assertFalse(payload["is_gte"])

    def test_long_close_is_inverse(self):
        order = {"Contract": "BTC_USDT", "price": "100", "side": "Open Long",
                 "size": "10", "value": "1000"}
        side, target, payload = pto._close_details(order, Decimal("10"))
        self.assertEqual((side, target), ("Close Long", "103.1"))
        self.assertEqual(payload["amount"], "-10")
        self.assertTrue(payload["is_gte"])

    def test_non_positive_target_is_rejected(self):
        with self.assertRaisesRegex(pto.PtoError, "必须大于 0"):
            pto._target_price({"price": "1", "value": "-1"}, 1)

    def test_pending_record_requires_value(self):
        source = {"Contract": "BTC_USDT"}
        with self.assertRaisesRegex(pto.PtoError, "value"):
            pto._pending_record(source, "pending open orders", "1", "2",
                                "Open Long", "1", "100")


if __name__ == "__main__":
    unittest.main()
