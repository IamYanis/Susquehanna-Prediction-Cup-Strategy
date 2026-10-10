"""Only mutable display data may change during immutable history comparison."""
import copy
import unittest

import autonomous_pilot as auto
import pilot_account as account


class TransactionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.row = {"event_id": "engine-124491147", "event_type": "trade", "createdAt": "2026-10-10T21:34:06.127Z",
                    "tournamentId": "bda92870-621e-47b0-bc3c-3602c5c26f55", "marketId": "377", "exchangeId": "1066",
                    "orderId": 29739740, "fillId": 124491142, "orderType": "BUY", "side": "no", "quantity": -1,
                    "price": .295, "amount": None, "transactionType": None, "currentPrice": .710,
                    "componentId": None, "collateralDelta": None, "settlementOption": "YES"}

    def test_current_price_only_change_passes(self):
        later = dict(self.row, currentPrice=.705)
        self.assertTrue(account.transaction_histories_equal([self.row], [later]))

    def test_financial_identity_and_linkage_changes_fail(self):
        for field, value in (("price", .300), ("quantity", -2), ("amount", -.005),
                ("event_id", "different"), ("orderId", 29739741), ("fillId", 124491143),
                ("marketId", "378"), ("exchangeId", "1067"), ("orderType", "SELL"), ("side", "yes"),
                ("createdAt", "2026-10-10T21:34:06.128Z"), ("collateralDelta", .01),
                ("settlementOption", "NO"), ("undocumentedDebit", .01)):
            with self.subTest(field=field):
                self.assertFalse(account.transaction_histories_equal([self.row], [dict(self.row, **{field: value})]))

    def test_added_removed_or_duplicate_events_fail(self):
        other = dict(self.row, event_id="another")
        for left, right in (([self.row], [self.row, other]), ([self.row, other], [self.row]),
                            ([self.row], [self.row, self.row]), ([self.row, self.row], [self.row, self.row])):
            with self.subTest(left=len(left), right=len(right)):
                self.assertFalse(account.transaction_histories_equal(left, right))

    def test_order_is_canonical_but_reassigned_event_identity_fails(self):
        other = dict(self.row, event_id="another", price=.900)
        self.assertTrue(account.transaction_histories_equal([self.row, other], [other, self.row]))
        self.assertFalse(account.transaction_histories_equal([self.row, other],
                          [dict(other, event_id=self.row["event_id"]), dict(self.row, event_id=other["event_id"])]))

    def test_timestamp_representation_is_canonical(self):
        later = dict(self.row, createdAt="2026-10-10T22:34:06.127+01:00")
        self.assertTrue(account.transaction_histories_equal([self.row], [later]))

    def test_missing_required_fields_fail_closed(self):
        for field in ("event_id", "createdAt", "quantity", "price", "amount", "tournamentId"):
            later = dict(self.row)
            del later[field]
            with self.subTest(field=field):
                self.assertFalse(account.transaction_histories_equal([self.row], [later]))

    def test_position_marks_are_not_account_activity(self):
        before = {"account": {"tournament": {"myBalance": 20571.1}, "orders": [], "positions": [
            {"exchangeId": "1066", "marketId": "377", "quantity": -1, "costBasis": .3, "settled": False,
             "currentPrice": .704956, "marketValue": .3, "unrealizedPnl": 0, "lots": []}]},
            "recent_fills": [], "recent_transactions": [self.row]}
        after = copy.deepcopy(before)
        after["account"]["positions"][0].update(currentPrice=.70, marketValue=.31, unrealizedPnl=.01)
        after["recent_transactions"][0]["currentPrice"] = .705
        self.assertTrue(auto.account_unchanged_after_leg1(before, after))
        for field, value in (("quantity", -2), ("costBasis", .31), ("settled", True)):
            changed = copy.deepcopy(after)
            changed["account"]["positions"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(auto.account_unchanged_after_leg1(before, changed))

    def test_account_capture_ignores_marks_but_keeps_cost_cash_and_settlement(self):
        before = {"tournament": {"myBalance": 20571.1}, "orders": [], "quarantine_reserve": .125,
                  "summary": {"totalMarketValue": .30, "totalCostBasis": .30, "totalUnrealizedPnl": 0},
                  "positions": [{"exchangeId": "1066", "marketId": "377", "quantity": -1,
                    "costBasis": .30, "avgCost": .295, "settled": False, "currentPrice": .705,
                    "marketValue": .30, "unrealizedPnl": 0, "unrealizedPnlPct": 0, "lots": []}]}
        after = copy.deepcopy(before)
        after["positions"][0].update(currentPrice=.70, marketValue=.31, unrealizedPnl=.01, unrealizedPnlPct=1)
        after["summary"].update(totalMarketValue=.31, totalUnrealizedPnl=.01)
        self.assertEqual(account.account_execution_state(before), account.account_execution_state(after))
        for container, key, value in ((after["tournament"], "myBalance", 20571.09),
                (after["positions"][0], "avgCost", .300), (after["positions"][0], "costBasis", .31),
                (after["positions"][0], "settled", True)):
            old = container[key]
            container[key] = value
            with self.subTest(field=key):
                self.assertNotEqual(account.account_execution_state(before), account.account_execution_state(after))
            container[key] = old


if __name__ == "__main__":
    unittest.main()
