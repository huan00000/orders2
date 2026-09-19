import unittest
import copy
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from decimal import Decimal
from unittest.mock import Mock, patch

import pto


def position(**overrides):
    result = {"contract": "BTC_USDT", "mode": "dual_long", "entry_price": "100",
              "value": "1000", "initial_margin": "10", "size": 37}
    result.update(overrides)
    return result


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
                patch.object(pto, "_get_position", return_value=position()), \
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
    def test_position_failure_preserves_order_and_continues(self, sign):
        first = {"id": "1", "Contract": "ETH_USDT", "price": "100.00",
                 "side": "Open Long", "size": "10", "value": "1000"}
        second = dict(first, id="2", Contract="BTC_USDT")
        root = {"finished open orders": [first, second], "pending close orders": [],
                "finished close orders": []}
        session = self.session()
        response = Mock(status_code=200)
        response.json.return_value = [position()]
        session.get.side_effect = [pto.requests.Timeout("timed out"), response]
        with patch.object(pto, "_order_file_lock"), \
                patch.object(pto, "_load_order_list", return_value=[root]), \
                patch.object(pto, "_write_order_list") as write, \
                self.assertLogs(pto.logger, level="ERROR"):
            self.assertEqual(pto.inverse_pto(session), 1)
        self.assertEqual(root["finished open orders"], [first])
        self.assertEqual(len(root["pending close orders"]), 1)
        self.assertEqual(root["pending close orders"][0]["tag"], "pending close orders.inverse 2")
        self.assertEqual(root["pending close orders"][0]["size"], "-37")
        self.assertEqual(session.get.call_count, 2)
        session.post.assert_called_once()
        write.assert_called_once()
        session.close.assert_not_called()

    def test_missing_record_field_prevents_submission(self):
        order = {"id": "1", "Contract": "BTC_USDT", "price": "100", "side": "Open Long"}
        root = {"finished open orders": [order], "pending close orders": [], "finished close orders": []}
        session = Mock()
        with patch.object(pto, "_order_file_lock"), \
                patch.object(pto, "_load_order_list", return_value=[root]), \
                patch.object(pto, "_get_position", return_value=position()), \
                patch.object(pto, "_write_order_list") as write, \
                self.assertLogs(pto.logger, level="ERROR"):
            self.assertEqual(pto.inverse_pto(session), 0)
        session.post.assert_not_called()
        write.assert_not_called()

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


class InverseReplacementTests(unittest.TestCase):
    def test_replacement_and_failure_retry(self):
        for side in ("Open Long", "Open Short"):
            for failure in (None, "stop", "create", "write"):
                with self.subTest(side=side, failure=failure):
                    self.check_replacement(side, failure)

    def check_replacement(self, side, failure):
        source = {"id": "10", "Contract": "BTC_USDT", "price": "100.00",
                  "side": side, "size": "1", "value": "1000"}
        old = {"id": "20", "Contract": "BTC_USDT",
               "side": side.replace("Open", "Close"),
               "tag": "pending close orders.inverse 9"}
        other_side = dict(old, id="21", side="Close Short" if side == "Open Long" else "Close Long")
        other_contract = dict(old, id="22", Contract="ETH_USDT")
        another_old = dict(old, id="23")
        root = {"raw orders": [], "pending open orders": [],
                "finished open orders": [source],
                "pending close orders": [old, other_side, other_contract, another_old],
                "finished close orders": []}
        original = copy.deepcopy(root)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "orderlist.js"
            path.write_text(json.dumps([root]), encoding="utf-8")
            before = path.read_bytes()
            stack.enter_context(patch.object(pto, "ORDER_LIST_PATH", path))
            stack.enter_context(patch.object(pto, "LOCK_PATH", path.with_suffix(".lock")))
            stack.enter_context(patch.object(pto, "_get_position", return_value=position(
                size=37 if side == "Open Long" else -37)))
            events = Mock()
            stop = stack.enter_context(patch.object(pto, "_stop_trailing_order"))
            create = stack.enter_context(patch.object(pto, "_create_trailing_order", return_value=("30", "40")))
            events.attach_mock(stop, "stop")
            events.attach_mock(create, "create")
            session = Mock()
            if failure:
                if failure == "stop":
                    stop.side_effect = pto.requests.Timeout("stop failed")
                elif failure == "create":
                    create.side_effect = pto.PtoError("create failed")
                with ExitStack() as errors:
                    if failure == "write":
                        errors.enter_context(patch.object(pto, "_write_order_list", side_effect=OSError("disk full")))
                    errors.enter_context(self.assertLogs(pto.logger, level="ERROR"))
                    self.assertEqual(pto.inverse_pto(session), 0)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [original])
                if failure == "stop":
                    create.assert_not_called()
                stop.side_effect = None
                create.side_effect = None
                events.reset_mock()
            self.assertEqual(pto.inverse_pto(session), 1)
            self.assertEqual([call[0] for call in events.mock_calls], ["stop", "stop", "create"])
            self.assertEqual([call.args[1] for call in stop.call_args_list], ["20", "23"])
            saved = json.loads(path.read_text(encoding="utf-8"))[0]
            self.assertEqual(saved["finished open orders"], [])
            self.assertEqual(saved["pending close orders"][:2], [other_side, other_contract])
            self.assertEqual(saved["pending close orders"][2]["id"], "30")
            self.assertEqual(saved["pending close orders"][2]["tag"], "pending close orders.inverse 10")
            events.reset_mock()
            self.assertEqual(pto.inverse_pto(session), 0)
            self.assertEqual(events.mock_calls, [])


class PtoCalculationTests(unittest.TestCase):
    def test_target_price_applies_value_sign_by_size(self):
        for size, value, expected in (
            (37, "1000", "104.131"),
            (-37, "1000", "95.931"),
            (37, "-1000", "97.869"),
            (-37, "-1000", "102.069"),
        ):
            with self.subTest(size=size, value=value):
                current = position(size=str(size), value=value)
                original = current.copy()
                self.assertEqual(pto._target_price(current), expected)
                self.assertEqual(current, original)

    def test_short_non_positive_target_is_rejected(self):
        for value in ("31", "30"):
            with self.subTest(value=value), self.assertRaisesRegex(pto.PtoError, "必须大于 0"):
                pto._target_price(position(size=-37, value=value))

    def test_zero_margin_retains_direction_adjustment(self):
        for size, expected in ((37, "101"), (-37, "99")):
            with self.subTest(size=size):
                self.assertEqual(pto._target_price(position(size=size, initial_margin="0")), expected)

    def test_open_payload_direction(self):
        order = {"Contract": "DOGE_USDT", "price": "0.1", "side": "Open Short", "size": "-10"}
        payload = pto._open_payload(order)
        self.assertTrue(payload["is_gte"])
        self.assertFalse(payload["reduce_only"])

    def test_close_uses_position_instead_of_source_values(self):
        for side, size, expected, is_gte in (
            ("Open Long", 37, "104.131", True),
            ("Open Short", -37, "95.931", False),
        ):
            with self.subTest(side=side):
                order = {"Contract": "BTC_USDT", "price": "90.407", "side": side,
                         "size": "999", "value": "999"}
                close_side, target, payload = pto._close_details(order, position(size=size))
                self.assertEqual(close_side, side.replace("Open", "Close"))
                self.assertEqual(target, expected)
                self.assertEqual(payload["activation_price"], expected)
                self.assertEqual(payload["amount"], str(-size))
                self.assertEqual(payload["is_gte"], is_gte)
                self.assertTrue(payload["reduce_only"])

    def test_documented_hype_example(self):
        order = {"Contract": "HYPE_USDT", "price": "90.407", "side": "Open Short"}
        current = position(entry_price="88.077351351351", value="342.9974",
                           initial_margin="4.833976690667", size=-37)
        _, target, payload = pto._close_details(order, current)
        self.assertEqual(target, "83.387")
        self.assertEqual(payload["amount"], "37")

    def test_close_target_matches_source_price_precision(self):
        for source, entry, expected in (
            ("90.407", "100", "104.131"),
            ("90.40", "100", "104.13"),
            ("90", "100", "104"),
            ("0.00100", "0.001", "0.00104"),
            ("1.00", "0.5", "0.51"),
        ):
            with self.subTest(source=source):
                order = {"Contract": "BTC_USDT", "price": source, "side": "Open Long"}
                current = position(entry_price=entry, initial_margin="0" if entry == "0.5" else "10")
                _, target, _ = pto._close_details(order, current)
                self.assertEqual(target, expected)

    def test_invalid_position_numbers_are_rejected(self):
        for field in ("entry_price", "value", "initial_margin", "size"):
            for invalid in (None, "", "bad", "NaN", "Infinity", "-Infinity"):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(pto.PtoError):
                    pto._target_price(position(**{field: invalid}))
        for values in ({"entry_price": "0"}, {"value": "0"}, {"size": 0},
                       {"initial_margin": "-1"}, {"value": "-1"}):
            with self.subTest(values=values), self.assertRaises(pto.PtoError):
                pto._target_price(position(**values))

    def test_close_target_rounded_to_zero_is_rejected(self):
        order = {"Contract": "BTC_USDT", "price": "1", "side": "Open Short"}
        with self.assertRaisesRegex(pto.PtoError, "必须大于 0"):
            pto._close_details(order, position(entry_price="0.1", size=-1))

    def test_invalid_source_precision_and_direction(self):
        for price, side in (("NaN", "Open Long"), ("0", "Open Long"),
                            ("bad", "Open Long"), ("1", "unknown"), ("1", "Open Short")):
            with self.subTest(price=price, side=side), self.assertRaises(pto.PtoError):
                pto._close_details({"Contract": "BTC_USDT", "price": price, "side": side}, position())

    def test_pending_record_requires_value(self):
        source = {"Contract": "BTC_USDT"}
        with self.assertRaisesRegex(pto.PtoError, "value"):
            pto._pending_record(source, "pending open orders", "1", "2",
                                "Open Long", "1", "100")


class PositionTests(unittest.TestCase):
    @patch.object(pto, "gen_sign", return_value={"SIGN": "test"})
    def test_select_direction_and_sign_request(self, sign):
        session = Mock()
        session.get.return_value.status_code = 200
        short = position(mode="dual_short", size=-37)
        session.get.return_value.json.return_value = [position(size=0), short]
        order = {"Contract": "BTC_USDT", "side": "Open Short"}
        self.assertEqual(pto._get_position(session, order), short)
        sign.assert_called_once_with("GET", "/api/v4/futures/usdt/positions/BTC_USDT", "")
        session.get.assert_called_once_with(
            pto.HOST + "/api/v4/futures/usdt/positions/BTC_USDT",
            headers={"Accept": "application/json", "Content-Type": "application/json", "SIGN": "test"},
            timeout=20)

    @patch.object(pto, "gen_sign", return_value={})
    def test_invalid_responses(self, sign):
        order = {"Contract": "BTC_USDT", "side": "Open Long"}
        for result in ([], None, "bad", [None], {"label": "error"},
                       [position(), position()], position(contract="ETH_USDT"),
                       position(mode="dual_short"), position(size=0), position(size=-1)):
            with self.subTest(result=result):
                session = Mock()
                session.get.return_value.status_code = 200
                session.get.return_value.json.return_value = result
                with self.assertRaises(pto.PtoError):
                    pto._get_position(session, order)
        session.get.return_value.status_code = 500
        with self.assertRaisesRegex(pto.PtoError, "HTTP 500"):
            pto._get_position(session, order)
        session.get.return_value.status_code = 200
        session.get.return_value.json.side_effect = ValueError("bad JSON")
        with self.assertRaisesRegex(pto.PtoError, "JSON"):
            pto._get_position(session, order)

    @patch.object(pto, "gen_sign", return_value={})
    def test_single_object_response(self, sign):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = position()
        self.assertEqual(pto._get_position(session, {"Contract": "BTC_USDT", "side": "Open Long"}), position())


if __name__ == "__main__":
    unittest.main()
