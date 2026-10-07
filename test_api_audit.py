"""Read-only API safety checks with fake responses and a fake clock."""
import contextlib
import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import paper_trader
import price_reader as scanner

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440001"
QUOTE_TIME = 1791374400  # 7 October 2026, 12:00 UTC.


def competition_context():
    """The explicit tournament context returned by the documented API."""
    return [{"type": "tournament", "tournament": {
        "id": TOURNAMENT_ID, "slug": "midterm-elections", "name": "Midterm Elections",
        "currencyName": "SUSQies", "isOngoingPlay": False,
    }}]


def party_market(party, market_id, exchange_id):
    return {
        "id": str(market_id), "title": f"Will the {party} Party win the Test Senate race?",
        "status": "open", "isComposite": False, "isMultiOutcome": False,
        "exchanges": [{"id": str(exchange_id), "option": "YES"}],
        "contexts": competition_context(),
    }


def election_tree(party, market_id):
    """Only fields used by the strategy; these details are passed through by the API."""
    return {
        "market_id": str(market_id), "contexts": competition_context(),
        "root": {
            "node_id": str(market_id), "contract_id": str(market_id),
            "node_type": "contract", "contract_type": "Election Outcome",
            "title": f"{party} Party winner", "settled_with": None,
            "settlement_date": "2026-11-04T17:00:00Z",
            "contract_details": {
                "raceId": "62978", "stageId": "98108", "electionDate": "2026-11-03",
                "raceStage": "General", "resolutionType": "Party Winner",
                "winnerName": f"{party} Party",
            },
        },
    }


def page(items, more=False, cursor=None):
    return {"data": items, "pagination": {
        "total": len(items), "limit": 100, "hasMore": more, "nextCursor": cursor,
    }}


def pair_relationship(exhaustive=False, extra_member=False):
    nodes = [{"exchangeId": "11", "marketId": "1", "outcome": "YES", "role": "member"},
             {"exchangeId": "12", "marketId": "2", "outcome": "YES", "role": "member"}]
    if extra_member:
        nodes.append({"exchangeId": "13", "marketId": "3", "outcome": "YES", "role": "member"})
    return {"id": "22222222-2222-2222-2222-222222222222", "version": 1,
            "type": "mutually_exclusive", "status": "active",
            "isExhaustive": exhaustive, "nodes": nodes}


def exchange_book(market_id=1, exchange_id=11, bid=.6, ask=.65):
    return {"marketId": str(market_id), "exchangeId": str(exchange_id), "depth": 1,
            "asOf": {"sequence": 4, "at": "2026-10-07T12:00:00Z"},
            "bids": [] if bid is None else [{"price": bid, "quantity": 100}],
            "asks": [] if ask is None else [{"price": ask, "quantity": 100}],
            "bestBid": bid, "bestAsk": ask,
            "spread": None if bid is None or ask is None else ask - bid}


class ApiAuditTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("_last_request_started", None), ("_read_cooldown_until", 0)):
            patcher = patch.object(scanner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, status=200, retry_after=None, payload=None):
        response = requests.Response()
        response.status_code = status
        response.url = scanner.MARKETS_URL
        response._content = json.dumps({"data": []} if payload is None else payload).encode()
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return response

    def test_key_cannot_be_sent_to_other_origins(self):
        session = Mock()
        for url in ("https://www.thesuper.market/api/v1/markets",
                    "http://sig.thesuper.market/api/v1/markets",
                    "https://sig.thesuper.market.evil.test/api/v1/markets",
                    "https://sig.thesuper.market/api/private"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                scanner.fetch_json(session, url)
        session.get.assert_not_called()

    def test_redirect_is_rejected_instead_of_followed(self):
        session = Mock()
        session.get.return_value = self.response(302)
        with self.assertRaises(ValueError):
            scanner.fetch_json(session, scanner.MARKETS_URL)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_requests_are_paced_without_real_waiting(self):
        session = Mock()
        session.get.return_value = self.response()
        with patch.object(scanner.time, "monotonic", side_effect=[10, 10, 10.25, 10.75]), \
                patch.object(scanner.time, "sleep") as sleep:
            scanner.fetch_json(session, scanner.MARKETS_URL)
            scanner.fetch_json(session, scanner.MARKETS_URL)
        sleep.assert_called_once_with(.5)
        self.assertEqual(session.get.call_count, 2)

    def test_rate_limit_prevents_reads_until_retry_time(self):
        session = Mock()
        session.get.side_effect = [self.response(429, "30"), self.response()]
        with patch.object(scanner.time, "monotonic", return_value=10):
            with self.assertRaises(scanner.RateLimitError):
                scanner.fetch_json(session, scanner.MARKETS_URL)
        with patch.object(scanner.time, "monotonic", return_value=20):
            with self.assertRaises(scanner.RateLimitError):
                scanner.fetch_json(session, scanner.MARKETS_URL)
        self.assertEqual(session.get.call_count, 1)
        with patch.object(scanner.time, "monotonic", return_value=41):
            self.assertEqual(scanner.fetch_json(session, scanner.MARKETS_URL), {"data": []})
        self.assertEqual(session.get.call_count, 2)

    def test_invalid_retry_after_uses_conservative_default(self):
        session = Mock()
        session.get.return_value = self.response(429, "nan")
        with patch.object(scanner.time, "monotonic", return_value=10):
            with self.assertRaises(scanner.RateLimitError):
                scanner.fetch_json(session, scanner.MARKETS_URL)
        self.assertEqual(scanner._read_cooldown_until, 70)

    def test_rate_limited_scan_retains_all_previous_observations(self):
        previous = {("Race", "NO-PAIR"): {"profit_per_pair": .1},
                    ("Other race", "YES-PAIR"): {"profit_per_pair": .1}}
        markets = (party_market("Democratic", 1, 11), party_market("Republican", 2, 12))
        with patch.object(scanner, "get_races", return_value=({"Race": markets}, {"1", "2"})), \
                patch.object(scanner, "fetch_pages", return_value=[pair_relationship()]), \
                patch.object(scanner, "get_pair_rules", return_value=({"NO-PAIR"}, {})), \
                patch.object(scanner, "get_best_prices", side_effect=scanner.RateLimitError), \
                contextlib.redirect_stdout(io.StringIO()):
            scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
        self.assertEqual(previous, {("Race", "NO-PAIR"): {"profit_per_pair": .1},
                                    ("Other race", "YES-PAIR"): {"profit_per_pair": .1}})

    def test_cursor_pages_are_complete_and_keep_competition_scope(self):
        params = {"tournamentId": TOURNAMENT_ID, "limit": 100}
        with patch.object(scanner, "fetch_json", side_effect=[
                page([{"id": "1"}], more=True, cursor="second-page"), page([{"id": "2"}])]) as fetch:
            self.assertEqual(scanner.fetch_pages(Mock(), scanner.MARKETS_URL, params),
                             [{"id": "1"}, {"id": "2"}])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.kwargs["params"],
                         {"tournamentId": TOURNAMENT_ID, "limit": 100, "cursor": "second-page"})
        self.assertNotIn("cursor", params)

    def test_missing_or_repeated_cursor_never_returns_partial_discovery(self):
        for responses in ([page([{"id": "1"}], True, None)],
                          [page([{"id": "1"}], True, "loop"), page([{"id": "2"}], True, "loop")]):
            with self.subTest(responses=responses), \
                    patch.object(scanner, "fetch_json", side_effect=responses), self.assertRaises(ValueError):
                scanner.fetch_pages(Mock(), scanner.MARKETS_URL, {"tournamentId": TOURNAMENT_ID})

    def test_failed_later_page_retains_previous_observations(self):
        previous = {("Saved race", "NO-PAIR"): {"profit_per_pair": .1}}
        with patch.object(scanner, "fetch_json", side_effect=[
                page([party_market("Democratic", 1, 11)], True, "next"), requests.Timeout()]), \
                patch.object(scanner, "execute_paper_trade") as trade, \
                contextlib.redirect_stdout(io.StringIO()):
            scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
        trade.assert_not_called()
        self.assertIn(("Saved race", "NO-PAIR"), previous)

    def test_discovery_matches_parties_across_pages(self):
        democratic = party_market("Democratic", 1, 11)
        republican = party_market("Republican", 2, 12)
        with patch.object(scanner, "fetch_json", side_effect=[
                page([democratic], True, "page-2"), page([republican])]):
            self.assertEqual(scanner.get_races(Mock(), TOURNAMENT_ID),
                             ({"Test Senate race": (democratic, republican)}, {"1", "2"}))

    def test_discovery_rejects_public_wrong_or_ongoing_context(self):
        invalid_contexts = [[], [{"type": "public", "tournament": None}],
                            competition_context(), competition_context(),
                            competition_context() * 2]
        invalid_contexts[2][0]["tournament"]["id"] = OTHER_TOURNAMENT_ID
        invalid_contexts[3][0]["tournament"]["isOngoingPlay"] = True
        for contexts in invalid_contexts:
            market = party_market("Democratic", 1, 11)
            market["contexts"] = contexts
            with self.subTest(contexts=contexts), \
                    patch.object(scanner, "fetch_pages", return_value=[market]), self.assertRaises(ValueError):
                scanner.get_races(Mock(), TOURNAMENT_ID)

    def test_numeric_ids_reject_url_injection_and_boolean_values(self):
        for identifier in (True, False, 0, -1, "01", "1/../../orders", "1?other=2", "1.0"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                scanner.numeric_id(identifier)

    def test_tournament_identity_and_currency_must_match(self):
        valid = {"id": TOURNAMENT_ID, "slug": "midterm-elections", "status": "active",
                 "currencyName": "SUSQies"}
        with patch.object(scanner, "fetch_json", return_value=valid):
            self.assertEqual(scanner.get_tournament(Mock()), TOURNAMENT_ID)
        for changes in ({"id": "invalid"}, {"id": None}, {"id": 42}, {"id": True},
                        {"slug": "ongoing-play"},
                        {"status": "ended"}, {"currencyName": "Other Coins"}):
            with self.subTest(changes=changes), \
                    patch.object(scanner, "fetch_json", return_value={**valid, **changes}), \
                    self.assertRaises(ValueError):
                scanner.get_tournament(Mock())

    def test_multi_exchange_and_composite_candidates_are_rejected(self):
        for changes in ({"isMultiOutcome": True}, {"isComposite": True},
                        {"exchanges": []}, {"exchanges": [{"id": "11", "option": "YES"},
                                                             {"id": "12", "option": "Other"}]},
                        {"exchanges": [{"id": "11", "option": "Candidate"}]}):
            market = {**party_market("Democratic", 1, 11), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scanner.get_exchange_id(market)

    def test_freeform_and_operator_rules_are_rejected(self):
        for changes in ({"contract_type": "Freeform"}, {"node_type": "operator"},
                        {"settled_with": "YES"}):
            tree = election_tree("Democratic", 1)
            tree["root"].update(changes)
            with self.subTest(changes=changes), \
                    patch.object(scanner, "fetch_json", return_value=tree), self.assertRaises(ValueError):
                scanner.get_election_rule(Mock(), party_market("Democratic", 1, 11),
                                          "Democratic", TOURNAMENT_ID)

    def test_election_rule_identity_and_party_are_validated(self):
        trees = [election_tree("Democratic", 1) for _ in range(4)]
        trees[0]["market_id"] = "2"
        trees[1]["contexts"][0]["tournament"]["id"] = OTHER_TOURNAMENT_ID
        trees[2]["root"]["contract_details"]["winnerName"] = "Republican Party"
        trees[3]["root"]["contract_details"]["resolutionType"] = "Candidate Winner"
        for tree in trees:
            with self.subTest(tree=tree), patch.object(scanner, "fetch_json", return_value=tree), \
                    self.assertRaises(ValueError):
                scanner.get_election_rule(Mock(), party_market("Democratic", 1, 11),
                                          "Democratic", TOURNAMENT_ID)

    def rules_for(self, relationship, democratic_tree=None, republican_tree=None):
        """Run the real approval logic with ordinary stored metadata and engine evidence."""
        with patch.object(scanner, "fetch_json", side_effect=[
                page([relationship]),
                democratic_tree or election_tree("Democratic", 1),
                republican_tree or election_tree("Republican", 2)]):
            return scanner.get_pair_rules(Mock(), party_market("Democratic", 1, 11),
                                          party_market("Republican", 2, 12), TOURNAMENT_ID)

    def test_non_exhaustive_exclusivity_approves_only_no_pair(self):
        allowed, context = self.rules_for(pair_relationship())
        self.assertEqual(allowed, {"NO-PAIR"})
        self.assertEqual(context["market_ids"], ["1", "2"])
        self.assertEqual(context["exchange_ids"], ["11", "12"])
        self.assertEqual(context["tournament_id"], TOURNAMENT_ID)
        self.assertEqual(len(context["settlement_fingerprint"]), 64)

    def test_two_member_exhaustive_relationship_approves_both_pairs(self):
        allowed, _ = self.rules_for(pair_relationship(exhaustive=True))
        self.assertEqual(allowed, {"YES-PAIR", "NO-PAIR"})

    def test_exhaustive_larger_group_does_not_approve_two_party_yes_pair(self):
        allowed, _ = self.rules_for(pair_relationship(exhaustive=True, extra_member=True))
        self.assertEqual(allowed, {"NO-PAIR"})

    def test_inactive_unrelated_or_wrong_market_relationships_are_rejected(self):
        relationships = [pair_relationship() for _ in range(4)]
        relationships[0]["status"] = "stale"
        relationships[1]["type"] = "implication"
        relationships[2]["nodes"][1]["exchangeId"] = "99"
        relationships[3]["nodes"][1]["marketId"] = "99"
        for relationship in relationships:
            with self.subTest(relationship=relationship), self.assertRaises(ValueError):
                self.rules_for(relationship)

    def test_malformed_relationship_identity_version_and_exhaustiveness_fail_closed(self):
        for changes in ({"id": None}, {"id": 42}, {"id": "not-a-uuid"},
                        {"version": True}, {"version": 0}, {"version": -1},
                        {"version": "1"}, {"isExhaustive": 1}, {"isExhaustive": "true"},
                        {"nodes": None}):
            relationship = {**pair_relationship(), **changes}
            with self.subTest(changes=changes), \
                    patch.object(scanner, "fetch_json") as fetch, self.assertRaises(ValueError):
                scanner.get_pair_rules(Mock(), party_market("Democratic", 1, 11),
                                       party_market("Republican", 2, 12), TOURNAMENT_ID,
                                       relationships=[relationship])
            fetch.assert_not_called()

    def test_shared_empty_graph_rejects_pair_without_node_or_book_requests(self):
        with patch.object(scanner, "fetch_json") as fetch, self.assertRaises(ValueError):
            scanner.get_pair_rules(Mock(), party_market("Democratic", 1, 11),
                                   party_market("Republican", 2, 12), TOURNAMENT_ID,
                                   relationships=[])
        fetch.assert_not_called()

    def test_empty_graph_is_fetched_once_and_skips_per_pair_network_reads(self):
        markets = (party_market("Democratic", 1, 11), party_market("Republican", 2, 12))
        previous = {("Test Senate race", "NO-PAIR"): {"profit_per_pair": .1}}
        with patch.object(scanner, "get_races", return_value=({"Test Senate race": markets}, {"1", "2"})), \
                patch.object(scanner, "fetch_pages", return_value=[]) as relationships, \
                patch.object(scanner, "fetch_json") as fetch, \
                patch.object(scanner, "execute_paper_trade") as trade, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
        relationships.assert_called_once()
        self.assertEqual(relationships.call_args.kwargs["params"] if "params" in relationships.call_args.kwargs
                         else relationships.call_args.args[2], {"tournamentId": TOURNAMENT_ID, "limit": 200})
        fetch.assert_not_called()
        trade.assert_not_called()
        self.assertIn(("Test Senate race", "NO-PAIR"), previous)
        self.assertIn("No active relationship", output.getvalue())

    def test_matching_titles_cannot_override_different_race_stage_or_date(self):
        for field, value in (("raceId", "62979"), ("stageId", "98109"),
                             ("electionDate", "2026-11-04"), ("raceStage", "Runoff")):
            republican = election_tree("Republican", 2)
            republican["root"]["contract_details"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.rules_for(pair_relationship(), republican_tree=republican)

    def test_metadata_change_changes_fingerprint_but_quote_change_does_not(self):
        relationship = pair_relationship()
        _, baseline = self.rules_for(relationship)
        repriced = copy.deepcopy(relationship)
        repriced["nodes"][0]["currentPrice"] = .99
        _, unchanged = self.rules_for(repriced)
        self.assertEqual(baseline["settlement_fingerprint"], unchanged["settlement_fingerprint"])
        revised = copy.deepcopy(relationship)
        revised["version"] = 2
        _, changed = self.rules_for(revised)
        self.assertNotEqual(baseline["settlement_fingerprint"], changed["settlement_fingerprint"])

    def test_exchange_book_requests_carry_explicit_tournament_and_exchange(self):
        with patch.object(scanner, "fetch_json", return_value=exchange_book()) as fetch, \
                patch.object(scanner.time, "time", return_value=QUOTE_TIME):
            result = scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)
        self.assertEqual(fetch.call_args.args[1], f"{scanner.API_BASE_URL}/exchanges/11/orderbook")
        self.assertEqual(fetch.call_args.kwargs["params"], {"tournamentId": TOURNAMENT_ID, "depth": 1})
        self.assertEqual(result["bid"], .6)
        self.assertEqual(result["ask_quantity"], 100)

    def test_engine_fractional_timestamps_produce_fresh_authoritative_quotes(self):
        # The engine can emit seven fractional digits. Python 3.9's native
        # parser rejects that precision, so the API parser must normalize it.
        for fraction in ("5928", "59280", "5928007"):
            book = exchange_book()
            book["asOf"]["at"] = f"2026-10-07T12:00:00.{fraction}+00:00"
            with self.subTest(fraction=fraction), \
                    patch.object(scanner, "fetch_json", return_value=book), \
                    patch.object(scanner.time, "time", return_value=QUOTE_TIME + 1), \
                    patch.object(scanner.time, "monotonic", return_value=10):
                result = scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)
                opportunities = scanner.find_opportunities(result, result, {"NO-PAIR"})
            self.assertAlmostEqual(result["quoted_at"], QUOTE_TIME + .5928, delta=.000001)
            self.assertEqual(result["version"]["at"], book["asOf"]["at"])
            self.assertEqual(set(opportunities), {"NO-PAIR"})

    def test_fractional_settlement_timestamps_support_verified_pair_rules(self):
        for fraction in ("1234", "12345", "1234567"):
            democratic, republican = election_tree("Democratic", 1), election_tree("Republican", 2)
            timestamp = f"2026-11-04T17:00:00.{fraction}+00:00"
            democratic["root"]["settlement_date"] = timestamp
            republican["root"]["settlement_date"] = timestamp
            with self.subTest(fraction=fraction):
                allowed, context = self.rules_for(pair_relationship(exhaustive=True),
                                                  democratic_tree=democratic,
                                                  republican_tree=republican)
            self.assertEqual(allowed, {"YES-PAIR", "NO-PAIR"})
            self.assertEqual(context["race_key"], ["62978", "98108", "2026-11-03", "General", "Party Winner"])

    def test_wrong_book_identity_is_rejected(self):
        for book in (exchange_book(market_id=2), exchange_book(exchange_id=12)):
            with self.subTest(book=book), patch.object(scanner, "fetch_json", return_value=book), \
                    self.assertRaises(ValueError):
                scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)

    def test_null_stale_future_or_unversioned_book_cannot_open_trade(self):
        versions = [None, {"sequence": True, "at": "2026-10-07T12:00:00Z"},
                    {"sequence": -1, "at": "2026-10-07T12:00:00Z"},
                    {"sequence": 4, "at": None}, {"sequence": 4, "at": 42},
                    {"sequence": 4, "at": []},
                    {"sequence": 4, "at": "2026-10-07T11:59:00Z"},
                    {"sequence": 4, "at": "2026-10-07T12:01:00Z"},
                    {"sequence": 4, "at": "2026-10-07T12:00:00"}]
        for version in versions:
            book = exchange_book()
            book["asOf"] = version
            with self.subTest(version=version), patch.object(scanner, "fetch_json", return_value=book), \
                    patch.object(scanner.time, "time", return_value=QUOTE_TIME), self.assertRaises(ValueError):
                scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)

    def test_one_sided_books_can_support_only_the_available_pair(self):
        for bid, ask, expected in ((None, .4, "YES-PAIR"), (.6, None, "NO-PAIR")):
            with self.subTest(expected=expected), \
                    patch.object(scanner, "fetch_json", return_value=exchange_book(bid=bid, ask=ask)), \
                    patch.object(scanner.time, "time", return_value=QUOTE_TIME), \
                    patch.object(scanner.time, "monotonic", return_value=10):
                book = scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)
                opportunities = scanner.find_opportunities(book, book, {"YES-PAIR", "NO-PAIR"})
            self.assertEqual(set(opportunities), {expected})
            self.assertAlmostEqual(opportunities[expected]["cost_per_pair"], .8)

    def test_quotes_received_too_far_apart_are_rejected(self):
        book = {"bid": .6, "ask": .65, "bid_quantity": 100, "ask_quantity": 100, "received_at": 10}
        with patch.object(scanner.time, "monotonic", return_value=16), self.assertRaises(ValueError):
            scanner.find_opportunities(book, book, {"NO-PAIR"})

    def test_authoritative_timestamp_can_age_out_before_pair_evaluation(self):
        # The snapshot is four seconds old when received. Two seconds later,
        # receive-time freshness still passes but the snapshot itself is too old.
        book = exchange_book()
        book["asOf"]["at"] = "2026-10-07T11:59:56Z"
        with patch.object(scanner, "fetch_json", return_value=book), \
                patch.object(scanner.time, "time", side_effect=[QUOTE_TIME, QUOTE_TIME + 2]), \
                patch.object(scanner.time, "monotonic", side_effect=[10, 12]):
            received = scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)
            with self.assertRaises(ValueError):
                scanner.find_opportunities(received, received, {"NO-PAIR"})

    def test_quiet_book_same_sequence_remains_valid_with_new_read_timestamp(self):
        first, second = exchange_book(), exchange_book()
        second["asOf"]["at"] = "2026-10-07T12:00:30Z"
        with patch.object(scanner, "fetch_json", side_effect=[first, second]), \
                patch.object(scanner.time, "time", side_effect=[QUOTE_TIME, QUOTE_TIME + 30]):
            before = scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)
            after = scanner.get_best_prices(Mock(), party_market("Democratic", 1, 11), TOURNAMENT_ID)
        self.assertEqual(before["version"]["sequence"], after["version"]["sequence"])
        self.assertEqual(after["quoted_at"] - before["quoted_at"], 30)

    def test_renamed_or_unmatched_listed_markets_retain_old_observations(self):
        for listed in ([party_market("Democratic", 1, 11), party_market("Republican", 2, 12)],
                       [party_market("Democratic", 1, 11)]):
            listed[0]["title"] = "Renamed Democratic race contract"
            previous = {("Old race title", "NO-PAIR"): {
                "profit_per_pair": .1, "market_context": {"market_ids": ["1", "2"]}}}
            original = copy.deepcopy(previous)
            with self.subTest(listed=listed), \
                    patch.object(scanner, "fetch_pages", side_effect=[listed, []]), \
                    patch.object(scanner, "execute_paper_trade") as trade, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
            self.assertEqual(previous, original)
            self.assertNotIn("DISAPPEARED", output.getvalue())
            trade.assert_not_called()

    def test_both_old_market_ids_absent_from_complete_list_confirms_disappearance(self):
        previous = {("Old race title", "NO-PAIR"): {
            "profit_per_pair": .1, "market_context": {"market_ids": ["1", "2"]}}}
        with patch.object(scanner, "fetch_pages", side_effect=[[], []]), \
                patch.object(scanner, "execute_paper_trade") as trade, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
        self.assertEqual(previous, {})
        self.assertIn("DISAPPEARED", output.getvalue())
        trade.assert_not_called()

    def test_invalid_book_timestamp_retains_observations_without_crashing(self):
        markets = (party_market("Democratic", 1, 11), party_market("Republican", 2, 12))
        previous = {("Test Senate race", "NO-PAIR"): {"profit_per_pair": .1}}
        book = exchange_book()
        book["asOf"]["at"] = None
        with patch.object(scanner, "get_races", return_value=({"Test Senate race": markets}, {"1", "2"})), \
                patch.object(scanner, "fetch_pages", return_value=[pair_relationship()]), \
                patch.object(scanner, "get_pair_rules", return_value=({"NO-PAIR"}, {})), \
                patch.object(scanner, "fetch_json", return_value=book), \
                patch.object(scanner, "execute_paper_trade") as trade, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
        self.assertIn(("Test Senate race", "NO-PAIR"), previous)
        self.assertIn("UNVERIFIED / UNAVAILABLE", output.getvalue())
        trade.assert_not_called()

    def test_only_fresh_versioned_empty_book_can_confirm_disappearance(self):
        markets = (party_market("Democratic", 1, 11), party_market("Republican", 2, 12))
        cases = [(None, True), ({"sequence": 4, "at": "2026-10-07T11:59:00Z"}, True),
                 ({"sequence": 4, "at": "2026-10-07T12:00:00Z"}, False)]
        for version, should_retain in cases:
            previous = {("Test Senate race", "NO-PAIR"): {"profit_per_pair": .1}}
            democratic = exchange_book(bid=None, ask=None)
            republican = exchange_book(2, 12, bid=None, ask=None)
            democratic["asOf"] = republican["asOf"] = version
            with self.subTest(version=version), \
                    patch.object(scanner, "get_races", return_value=({"Test Senate race": markets}, {"1", "2"})), \
                    patch.object(scanner, "fetch_pages", return_value=[pair_relationship()]), \
                    patch.object(scanner, "get_pair_rules", return_value=({"NO-PAIR"}, {})), \
                    patch.object(scanner, "fetch_json", side_effect=[democratic, republican]), \
                    patch.object(scanner, "execute_paper_trade") as trade, \
                    patch.object(scanner.time, "time", return_value=QUOTE_TIME), \
                    patch.object(scanner.time, "monotonic", return_value=10), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
            self.assertEqual(("Test Senate race", "NO-PAIR") in previous, should_retain)
            self.assertEqual("DISAPPEARED" in output.getvalue(), not should_retain)
            trade.assert_not_called()

    def test_safe_failure_reasons_never_print_raw_request_secrets(self):
        secret = "credential-only-in-this-offline-test"
        markets = (party_market("Democratic", 1, 11), party_market("Republican", 2, 12))
        for error, expected in ((requests.HTTPError(f"Authorization: Bearer {secret}"), "API request failed"),
                                (ValueError(f"invalid timestamp {secret}"), "Malformed API data"),
                                (scanner.DataValidationError("Wrong market/exchange order book"),
                                 "Wrong market/exchange order book")):
            previous = {("Test Senate race", "NO-PAIR"): {"profit_per_pair": .1}}
            with self.subTest(error=type(error)), \
                    patch.object(scanner, "get_races", return_value=({"Test Senate race": markets}, {"1", "2"})), \
                    patch.object(scanner, "fetch_pages", return_value=[pair_relationship()]), \
                    patch.object(scanner, "get_pair_rules", side_effect=error), \
                    patch.object(scanner, "execute_paper_trade") as trade, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                scanner.scan_once(Mock(), previous, TOURNAMENT_ID)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn(secret, output.getvalue())
            self.assertIn(("Test Senate race", "NO-PAIR"), previous)
            trade.assert_not_called()

    def test_audit_only_uses_get_and_leaves_portfolio_and_log_untouched(self):
        """Exercise the complete CLI scan with fake GET responses and disposable files."""
        tournament = {"id": TOURNAMENT_ID, "slug": "midterm-elections", "status": "active",
                      "currencyName": "SUSQies"}
        payloads = [tournament, page([party_market("Democratic", 1, 11),
                                      party_market("Republican", 2, 12)]),
                    page([pair_relationship()]),
                    election_tree("Democratic", 1), election_tree("Republican", 2),
                    exchange_book(), exchange_book(2, 12)]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = [self.response(payload=payload) for payload in payloads]
        with tempfile.TemporaryDirectory() as directory:
            portfolio_path = Path(directory) / "paper_portfolio.json"
            log_path = Path(directory) / "paper_trades.csv"
            # Deliberately invalid sentinels also prove audit mode does not load them.
            portfolio_path.write_bytes(b"existing portfolio, unchanged")
            log_path.write_bytes(b"existing CSV, unchanged")
            with patch.object(paper_trader, "PORTFOLIO_PATH", portfolio_path), \
                    patch.object(paper_trader, "TRADE_LOG_PATH", log_path), \
                    patch.object(scanner, "load_dotenv") as dotenv, \
                    patch.object(scanner.os, "getenv", return_value="offline-test-key"), \
                    patch.object(scanner.requests, "Session", return_value=session), \
                    patch.object(scanner, "load_portfolio") as load, \
                    patch.object(scanner, "execute_paper_trade") as trade, \
                    patch.object(scanner, "print_portfolio_summary") as summary, \
                    patch.object(scanner.time, "sleep"), \
                    patch.object(scanner.time, "time", return_value=QUOTE_TIME), \
                    patch.object(scanner.time, "monotonic", return_value=10), \
                    patch("sys.argv", ["price_reader.py", "--audit-only"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(scanner.main(), 0)
            load.assert_not_called()
            trade.assert_not_called()
            summary.assert_not_called()
            dotenv.assert_called_once()
            session.post.assert_not_called()
            session.delete.assert_not_called()
            session.patch.assert_not_called()
            session.put.assert_not_called()
            self.assertEqual(session.get.call_count, len(payloads))
            self.assertIn("APPEARED", output.getvalue())
            self.assertEqual(portfolio_path.read_bytes(), b"existing portfolio, unchanged")
            self.assertEqual(log_path.read_bytes(), b"existing CSV, unchanged")

    def test_once_paper_cli_saves_and_restores_real_generated_market_context(self):
        """Use the real paper trader and persistence, with fake GETs and temporary files."""
        tournament = {"id": TOURNAMENT_ID, "slug": "midterm-elections", "status": "active",
                      "currencyName": "SUSQies"}
        payloads = [tournament, page([party_market("Democratic", 1, 11),
                                      party_market("Republican", 2, 12)]),
                    page([pair_relationship()]),
                    election_tree("Democratic", 1), election_tree("Republican", 2),
                    exchange_book(), exchange_book(2, 12)]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = [self.response(payload=payload) for payload in payloads]
        fake_key = "offline-paper-cli-key"
        with tempfile.TemporaryDirectory() as directory:
            portfolio_path = Path(directory) / "paper_portfolio.json"
            log_path = Path(directory) / "paper_trades.csv"
            with patch.object(paper_trader, "PORTFOLIO_PATH", portfolio_path), \
                    patch.object(paper_trader, "TRADE_LOG_PATH", log_path), \
                    patch.object(paper_trader, "open_positions", []), \
                    patch.object(paper_trader, "paper_balance", 5000), \
                    patch.object(scanner, "load_dotenv"), \
                    patch.object(scanner.os, "getenv", return_value=fake_key), \
                    patch.object(scanner.requests, "Session", return_value=session), \
                    patch.object(scanner.time, "sleep"), \
                    patch.object(scanner.time, "time", return_value=QUOTE_TIME), \
                    patch.object(scanner.time, "monotonic", return_value=10), \
                    patch("sys.argv", ["price_reader.py", "--once"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(scanner.main(), 0)
                self.assertEqual(len(paper_trader.open_positions), 1)
                position = paper_trader.open_positions[0]
                self.assertEqual(position["position_type"], "NO-PAIR")
                self.assertEqual(position["quantity"], 100)
                self.assertAlmostEqual(position["capital_used"], 80)
                self.assertAlmostEqual(paper_trader.paper_balance, 4920)
                self.assertEqual(paper_trader.MAX_CAPITAL_PER_TRADE, 250)
                self.assertEqual(paper_trader.MAX_CAPITAL_PER_RACE, 150)
                context = position["market_context"]
                self.assertEqual(context["tournament_id"], TOURNAMENT_ID)
                self.assertEqual(context["market_ids"], ["1", "2"])
                self.assertEqual(context["exchange_ids"], ["11", "12"])
                self.assertEqual(context["book_versions"], [payloads[-2]["asOf"], payloads[-1]["asOf"]])
                self.assertEqual(context["leg_prices"], [.4, .4])
                saved = json.loads(portfolio_path.read_text())
                # JSON serializes relationship member tuples as lists. Compare
                # that exact serialized representation after a real restart.
                self.assertEqual(saved["open_positions"], json.loads(json.dumps(paper_trader.open_positions)))
                paper_trader.open_positions.clear()
                paper_trader.paper_balance = 0
                self.assertTrue(paper_trader.load_portfolio())
                self.assertEqual(paper_trader.open_positions, saved["open_positions"])
                self.assertEqual(paper_trader.paper_balance, saved["paper_balance"])
                rows = list(csv.DictReader(io.StringIO(log_path.read_text())))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["trade_id"], paper_trader.open_positions[0]["trade_id"])
                self.assertNotIn(fake_key, portfolio_path.read_text())
                self.assertNotIn(fake_key, log_path.read_text())
            self.assertIn("PAPER TRADE", output.getvalue())
            self.assertEqual(session.get.call_count, len(payloads))
            session.post.assert_not_called()
            session.delete.assert_not_called()
            session.patch.assert_not_called()
            session.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
