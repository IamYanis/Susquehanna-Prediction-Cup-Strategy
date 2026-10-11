"""Exact semantics, signal gates, safe integration and research-only persistence."""
import ast
import copy
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import external_value_scanner as scanner
import polymarket_client as poly


class ExternalScannerTests(unittest.TestCase):
    def setUp(self):
        self.mapping = copy.deepcopy(scanner.load_mappings()[0])
        self.market = {"id": self.mapping["sig_market_id"], "status": "open", "isComposite": False,
                       "isMultiOutcome": False, "exchanges": [{"id": self.mapping["sig_exchange_id"], "option": "YES"}],
                       "currentPrice": .99}
        self.root = {"contract_details": {"resolutionType": "Party Winner", "winnerName": "Democratic Party"}}
        self.mapping["sig_root_sha256"] = scanner.digest(self.root)
        self.raw = {"id": self.mapping["polymarket_market_id"], "question": self.mapping["polymarket_question"],
                    "slug": self.mapping["polymarket_slug"], "description": "The party nominee wins this general election.",
                    "resolutionSource": None, "endDate": "2026-11-04T04:59:00Z", "updatedAt": "2026-10-11T11:00:00Z"}
        self.mapping["polymarket_rules_sha256"] = scanner.digest(scanner.external_rules(self.raw))
        self.external = {"raw": self.raw, "market_id": self.mapping["polymarket_market_id"],
                         "condition_id": self.mapping["polymarket_condition_id"], "tokens": self.mapping["polymarket_tokens"].copy(),
                         "headline": {"Yes": Decimal(".99"), "No": Decimal(".01")},
                         "liquidity": Decimal(10000), "volume": Decimal(10000), "open": True}
        self.books = {"Yes": self.book(".58", ".62"), "No": self.book(".38", ".42")}
        self.sig_book = {"bid": .50, "ask": .55, "bid_quantity": 100, "ask_quantity": 100, "quoted_at": 1000}

    def book(self, bid, ask):
        return {"bid": Decimal(bid), "ask": Decimal(ask), "midpoint": (Decimal(bid) + Decimal(ask)) / 2,
                "bid_depth": Decimal(100), "ask_depth": Decimal(100), "spread": Decimal(ask) - Decimal(bid),
                "book_at": 1000, "fetched_at": 1000}

    def rows(self, **changes):
        inputs = dict(mapping=self.mapping, market=self.market, root=self.root, external=self.external,
                      books=self.books, sig_book=self.sig_book, tournament_id=self.mapping["tournament_id"], now=1000)
        inputs.update(changes)
        return scanner.compare(**inputs)

    def test_config_has_only_six_exact_reviewed_instruments(self):
        mappings = scanner.load_mappings()
        self.assertEqual({m["sig_market_id"] for m in mappings}, {"256", "257", "377", "378", "381", "382"})
        approved = [m for m in mappings if m["mapping_status"] == "APPROVED_RESEARCH_ONLY"]
        self.assertEqual(len(approved), 4)
        self.assertEqual({m["polymarket_market_id"] for m in approved}, {"630666", "630667", "630844", "630845"})
        self.assertTrue(all(m["source_urls"] and m["sig_root_sha256"] and m["polymarket_rules_sha256"] for m in approved))

    def test_unmatched_alaska_rejected_without_manufactured_mapping(self):
        unmatched = next(m for m in scanner.load_mappings() if m["sig_market_id"] == "377")
        self.assertIsNone(unmatched["polymarket_market_id"])
        rows = self.rows(mapping=unmatched, external=None, books={})
        self.assertTrue(all(r["signal"] == "BLOCKED" and r["external_edge"] is None for r in rows))
        self.assertIn("NO_EQUIVALENT_EXTERNAL_MARKET", rows[0]["flags"])

    def test_yes_and_no_semantics_normalize_independently(self):
        yes, no = self.rows()
        self.assertEqual(yes["external_fair"], Decimal(".60"))
        self.assertEqual(no["external_fair"], Decimal(".40"))
        self.assertEqual(yes["external_edge"], Decimal(".05"))
        self.assertEqual(no["external_edge"], Decimal("-.10"))
        self.assertEqual(yes["signal"], "BUY_SIG_YES")
        self.assertEqual(no["signal"], "NO_SIGNAL")

    def test_explicit_inverse_semantics_uses_corresponding_no_token(self):
        self.mapping["polymarket_yes_represents"] = "SIG_NO"
        yes, no = self.rows()
        self.assertEqual(yes["polymarket_reference_outcome"], "No")
        self.assertEqual(yes["external_fair"], Decimal(".40"))
        self.assertEqual(no["external_fair"], Decimal(".60"))
        self.assertEqual(no["signal"], "BUY_SIG_NO")

    def test_sig_uses_executable_prices_not_currentprice(self):
        yes, no = self.rows()
        self.assertEqual(yes["sig_buy"], Decimal(".550"))
        self.assertEqual(no["sig_buy"], Decimal(".500"))
        self.assertEqual(yes["sig_sell"], Decimal(".500"))
        self.assertEqual(no["sig_sell"], Decimal(".450"))
        self.market["currentPrice"] = .01
        self.assertEqual(self.rows()[0]["external_edge"], Decimal(".05"))

    def test_sig_tick_rounding_precedes_threshold(self):
        self.books["Yes"] = self.book(".5781", ".5821")  # fair .5801
        self.sig_book["ask"] = .55001  # raw edge .03009 passes; tick-rounded .555 fails.
        self.assertEqual(self.rows()[0]["sig_buy"], Decimal(".555"))
        self.assertEqual(self.rows()[0]["external_edge"], Decimal(".0251"))
        self.assertEqual(self.rows()[0]["signal"], "NO_SIGNAL")

    def test_polymarket_book_midpoint_not_headline_probability(self):
        self.external["headline"]["Yes"] = Decimal(".01")
        yes = self.rows()[0]
        self.assertEqual(yes["external_fair"], Decimal(".60"))
        self.assertEqual(yes["reference_type"], "BOOK_MIDPOINT")
        self.assertEqual(yes["absolute_probability_difference"], Decimal(".075"))

    def test_three_percentage_point_boundary_is_decimal(self):
        for fair, signal in [(".58", "BUY_SIG_YES"), (".579999999", "NO_SIGNAL"), (".55", "NO_SIGNAL"), (".54", "NO_SIGNAL")]:
            with self.subTest(fair=fair):
                self.books["Yes"] = self.book(str(Decimal(fair) - Decimal(".01")), str(Decimal(fair) + Decimal(".01")))
                self.assertEqual(self.rows()[0]["signal"], signal)

    def test_missing_book_lowers_confidence_and_blocks_signal(self):
        for books in [{}, {"Yes": dict(self.books["Yes"], bid=None, midpoint=None, bid_depth=Decimal(0))}]:
            yes = self.rows(books=books)[0]
            self.assertEqual(yes["data_confidence"], "LOW")
            self.assertEqual(yes["signal"], "BLOCKED")
            self.assertIn("LOW_CONFIDENCE_REFERENCE", yes["flags"])

    def test_stale_external_or_sig_data_blocks(self):
        self.books["Yes"]["book_at"] = 939
        self.assertIn("STALE_OR_MISSING_EXTERNAL_BOOK", self.rows()[0]["flags"])
        self.assertEqual(self.rows()[0]["signal"], "BLOCKED")
        self.books["Yes"]["book_at"] = 1000
        self.sig_book["quoted_at"] = 994
        self.assertIn("STALE_OR_MISSING_SIG_BOOK", self.rows()[0]["flags"])

    def test_ids_exchanges_conditions_tokens_and_tournament_must_match(self):
        changes = [dict(mapping=dict(self.mapping, sig_market_id="999")),
                   dict(mapping=dict(self.mapping, sig_exchange_id="999")),
                   dict(external=dict(self.external, market_id="999")),
                   dict(external=dict(self.external, condition_id="other")),
                   dict(external=dict(self.external, tokens={"Yes": "wrong", "No": "other"})),
                   dict(tournament_id="wrong")]
        for inputs in changes:
            with self.subTest(inputs=list(inputs)):
                self.assertEqual(self.rows(**inputs)[0]["signal"], "BLOCKED")
                self.assertIn("MAPPING_NEEDS_REVALIDATION", self.rows(**inputs)[0]["flags"])

    def test_changed_rules_root_source_date_or_mismatch_flag_blocks(self):
        for field, value in [("description", "Now first-round only."), ("resolutionSource", "different"), ("endDate", "2027-01-01")]:
            raw = dict(self.raw, **{field: value})
            yes = self.rows(external=dict(self.external, raw=raw))[0]
            self.assertEqual(yes["signal"], "BLOCKED")
            self.assertIn("SETTLEMENT_RULE_MISMATCH", yes["flags"])
        self.assertIn("SETTLEMENT_RULE_MISMATCH", self.rows(root={"different": True})[0]["flags"])
        self.assertIn("SETTLEMENT_RULE_MISMATCH", self.rows(mapping=dict(self.mapping, settlement_mismatch=True))[0]["flags"])

    def test_rules_whitespace_refresh_is_harmless(self):
        raw = dict(self.raw, description="The party nominee\n\n wins this general election.  ")
        self.assertEqual(self.rows(external=dict(self.external, raw=raw))[0]["signal"], "BUY_SIG_YES")

    def test_closed_venues_and_low_depth_block(self):
        for inputs in [dict(market=dict(self.market, status="closed")), dict(external=dict(self.external, open=False)),
                       dict(sig_book=dict(self.sig_book, ask_quantity=0)),
                       dict(books={"Yes": dict(self.books["Yes"], bid_depth=Decimal(49))})]:
            self.assertEqual(self.rows(**inputs)[0]["signal"], "BLOCKED")

    def test_ranking_positive_external_edges_not_absolute_negative_edges(self):
        rows = [{"external_edge": edge, "race": "Race", "sig_market_id": "1", "side": "YES"}
                for edge in [Decimal("-.50"), Decimal(".04"), None, Decimal(".10"), Decimal(".03")]]
        self.assertEqual([r["external_edge"] for r in scanner.rank_rows(rows)][:3], [Decimal(".10"), Decimal(".04"), Decimal(".03")])

    def test_snapshots_append_and_preserve_exact_decimal_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "research.sqlite3"
            scanner.save_snapshots(self.rows(), path)
            scanner.save_snapshots(self.rows(), path)
            # The same timestamped snapshot is a no-op, not four observations.
            with sqlite3.connect(str(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM comparisons").fetchone()[0], 2)
            scanner.save_snapshots(self.rows(now=1001), path)
            with sqlite3.connect(str(path)) as db:
                rows = db.execute("SELECT external_edge,record_json FROM comparisons ORDER BY id").fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual(Decimal(rows[0][0]), Decimal("0.050"))
            payload = json.loads(rows[0][1])
            self.assertEqual(payload["mapping_version"], 1)
            self.assertEqual(payload["polymarket_tokens"], self.mapping["polymarket_tokens"])
            self.assertEqual(Decimal(payload["external_edge"]), Decimal(".05"))
            self.assertNotIn("balance", payload)
            self.assertNotIn("Authorization", rows[0][1])

    def test_default_cli_does_not_persist_or_load_live_state(self):
        with patch.object(scanner, "load_dotenv"), patch.object(scanner.os, "getenv", return_value="FAKE_TEST_KEY"), \
                patch.object(scanner, "scan", return_value=self.rows()), patch.object(scanner, "print_report"), \
                patch.object(scanner, "save_snapshots") as persist, patch("sys.argv", ["external_value_scanner.py"]):
            self.assertEqual(scanner.main(), 0)
            persist.assert_not_called()

    def test_loop_recovers_read_failure_and_ctrl_c_without_trading_state(self):
        with patch.object(scanner, "load_dotenv"), patch.object(scanner.os, "getenv", return_value="FAKE_TEST_KEY"), \
                patch.object(scanner, "scan", side_effect=[poly.ExternalDataError("UNAVAILABLE"), self.rows()]) as scan, \
                patch.object(scanner, "print_report"), patch.object(scanner, "save_snapshots", return_value={}) as persist, \
                patch.object(scanner.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                patch("sys.argv", ["external_value_scanner.py", "--snapshots", "--loop"]):
            self.assertEqual(scanner.main(), 0)
            self.assertEqual(scan.call_count, 2)
            persist.assert_called_once()

    def test_scan_get_only_integration_sig_book_requested_last(self):
        calls = []
        tid = self.mapping["tournament_id"]
        contexts = [{"type": "tournament", "tournament": {"id": tid, "isOngoingPlay": False}}]
        market = dict(self.market, contexts=contexts)
        tree = {"market_id": self.market["id"], "contexts": contexts, "root": self.root}
        def sig_get(session, url, params):
            calls.append("SIG_METADATA")
            return tree if url.endswith("/nodes") else market
        def external_book(session, token, condition):
            calls.append("POLY_BOOK")
            return self.books["Yes" if token == self.mapping["polymarket_tokens"]["Yes"] else "No"]
        def sig_book(*args):
            calls.append("SIG_BOOK")
            return self.sig_book
        with patch.object(scanner.sig, "get_tournament", return_value=tid), patch.object(scanner.sig, "fetch_json", side_effect=sig_get), \
                patch.object(scanner.poly, "read_market", return_value=self.external), patch.object(scanner.poly, "read_book", side_effect=external_book), \
                patch.object(scanner.sig, "get_best_prices", side_effect=sig_book), patch.object(scanner.time, "time", return_value=1000):
            result = scanner.scan(Mock(), Mock(), [self.mapping])
        self.assertEqual(len(result), 2)
        self.assertEqual(calls[-1], "SIG_BOOK")
        self.assertEqual(result[0]["signal"], "BUY_SIG_YES")

    def test_no_trading_import_or_order_write_call(self):
        for name in ["external_value_scanner.py", "polymarket_client.py"]:
            tree = ast.parse((scanner.ROOT / name).read_text())
            calls = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
            self.assertFalse(set(calls) & {"post", "delete", "put", "patch", "submit", "prepare"})
            imports = [n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)]
            self.assertFalse(set(imports) & {"autonomous_pilot", "live_pilot", "supervised_accounting_probe", "account_test"})
        self.assertEqual(scanner.MIN_EXTERNAL_EDGE, Decimal(".03"))
        import config
        import autonomous_pilot
        self.assertEqual(config.LIVE_ALLOCATION, 5000)
        self.assertEqual(config.MAX_LIVE_QUANTITY_PER_LEG, 1)
        self.assertEqual(autonomous_pilot.MIN_EDGE, Decimal(".005"))


if __name__ == "__main__":
    unittest.main()
