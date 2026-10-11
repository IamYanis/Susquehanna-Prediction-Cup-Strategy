"""Read-only SIG/Polymarket research: executable quotes and optional shadow history."""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from dotenv import load_dotenv

import price_reader as sig
import polymarket_client as poly
from order_preview import PRICE_TICK

ROOT = Path(__file__).resolve().parent
MAPPING_PATH = ROOT / "external_market_mappings.json"
SNAPSHOT_PATH = ROOT / "external_research" / "comparisons.sqlite3"
MIN_EXTERNAL_EDGE = Decimal("0.03")
MIN_SIG_DEPTH = Decimal("1")  # Research signal for one SIG share, not an order.
MIN_EXTERNAL_DEPTH = Decimal("50")  # Both sides of the reference outcome's best book.
ONE = Decimal("1")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def external_rules(raw):
    """Bind substantive description/source/date, excluding prices and deployment metadata."""
    description = raw.get("description")
    poly.require(isinstance(description, str) and bool(description.strip()), "EXTERNAL_RULES_UNAVAILABLE")
    return {"description": " ".join(description.split()), "resolution_source": raw.get("resolutionSource"),
            "end_date": raw.get("endDate")}


def load_mappings(path=MAPPING_PATH):
    payload = json.loads(Path(path).read_text())
    poly.require(payload["version"] == 1 and payload["allowed_use"] == "READ_ONLY_RESEARCH", "INVALID_MAPPING_CONFIG")
    mappings = payload["mappings"]
    poly.require(isinstance(mappings, list) and len(mappings) == 6, "INVALID_MAPPING_CONFIG")
    seen = set()
    for m in mappings:
        mid = sig.numeric_id(m["sig_market_id"])
        sig.numeric_id(m["sig_exchange_id"])
        poly.require(mid not in seen and m["mapping_status"] in ("APPROVED_RESEARCH_ONLY", "NO_EQUIVALENT_EXTERNAL_MARKET")
                     and m["allowed_use"] == "READ_ONLY_RESEARCH",
                     "INVALID_MAPPING_CONFIG")
        seen.add(mid)
        poly.require(m["normalized_outcome"] in ("Democratic Party wins", "Republican Party wins")
                     and m["election_stage"] == "General" and m["approval_note"] and m["settlement_notes"], "INVALID_MAPPING_CONFIG")
        if m["mapping_status"] == "APPROVED_RESEARCH_ONLY":
            poly.require(m["polymarket_yes_represents"] in ("SIG_YES", "SIG_NO")
                         and m["polymarket_market_id"] and m["polymarket_condition_id"]
                         and m["polymarket_tokens"] and m["polymarket_rules_sha256"]
                         and m["sig_root_sha256"] and m["source_urls"], "INVALID_MAPPING_CONFIG")
        else:
            poly.require(m["polymarket_market_id"] is None and m["polymarket_condition_id"] is None
                         and m["polymarket_tokens"] is None, "UNMATCHED_MAPPING_MUST_NOT_HAVE_A_CONTRACT")
    return mappings


def validate_mapping(mapping, market, root, external, tournament_id):
    """No search/fuzzy/title inference. Compare reviewed IDs, rules and semantics."""
    if mapping["mapping_status"] != "APPROVED_RESEARCH_ONLY":
        return "NO_EQUIVALENT_EXTERNAL_MARKET"
    if (tournament_id != mapping["tournament_id"] or str(market["id"]) != mapping["sig_market_id"]
            or sig.get_exchange_id(market) != mapping["sig_exchange_id"]):
        return "MAPPING_NEEDS_REVALIDATION"
    if digest(root) != mapping["sig_root_sha256"]:
        return "SETTLEMENT_RULE_MISMATCH"
    if external is None:
        return "EXTERNAL_DATA_UNAVAILABLE"
    raw = external["raw"]
    if (external["market_id"] != mapping["polymarket_market_id"]
            or external["condition_id"] != mapping["polymarket_condition_id"]
            or external["tokens"] != mapping["polymarket_tokens"]
            or raw.get("question") != mapping["polymarket_question"]
            or raw.get("slug") != mapping["polymarket_slug"]):
        return "MAPPING_NEEDS_REVALIDATION"
    if digest(external_rules(raw)) != mapping["polymarket_rules_sha256"]:
        return "SETTLEMENT_RULE_MISMATCH"
    if mapping.get("settlement_mismatch") is not False:
        return "SETTLEMENT_RULE_MISMATCH"
    return "PASS"


def sig_prices(book):
    """A SIG book is in YES terms: buying NO executes against the YES bid."""
    def rounded(value, direction):
        if value is None:
            return None
        return (value / PRICE_TICK).to_integral_value(rounding=direction) * PRICE_TICK

    bid = Decimal(str(book["bid"])) if book["bid"] is not None else None
    ask = Decimal(str(book["ask"])) if book["ask"] is not None else None
    return {"buy_yes": rounded(ask, ROUND_CEILING),
            "buy_no": rounded(ONE - bid if bid is not None else None, ROUND_CEILING),
            "sell_yes": rounded(bid, ROUND_FLOOR),
            "sell_no": rounded(ONE - ask if ask is not None else None, ROUND_FLOOR),
            "yes_midpoint": (bid + ask) / 2 if bid is not None and ask is not None else None,
            "spread": ask - bid if bid is not None and ask is not None else None}


def compare(mapping, market, root, external, books, sig_book, tournament_id, now=None, errors=()):
    """Produce BOTH semantic sides independently; a headline never becomes a buy price."""
    now = time.time() if now is None else now
    status = validate_mapping(mapping, market, root, external, tournament_id)
    reference_side = "No" if mapping.get("polymarket_yes_represents") == "SIG_NO" else "Yes"
    reference_book = books.get(reference_side)
    headline = external["headline"][reference_side] if external else None
    fair_yes, reference_type, confidence = poly.reference(reference_book, headline)
    if status != "PASS":
        # Quotes can be displayed for review, but invalid semantics have no fair
        # value/edge. Never compute an opposite or unapproved proposition's edge.
        fair_yes = None
    prices = sig_prices(sig_book) if sig_book else {}
    flags = list(errors)
    if status != "PASS":
        flags.append(status)
    if market.get("status") != "open" or (external is not None and not external["open"]):
        flags.append("MARKET_NOT_OPEN")
    if sig_book is None or not -5 <= now - sig_book["quoted_at"] <= 5:
        flags.append("STALE_OR_MISSING_SIG_BOOK")
    if external is not None and not poly.is_fresh(reference_book, now):
        flags.append("STALE_OR_MISSING_EXTERNAL_BOOK")
    if confidence != "HIGH":
        flags.append("LOW_CONFIDENCE_REFERENCE")
    if reference_book is None or min(reference_book["bid_depth"], reference_book["ask_depth"]) < MIN_EXTERNAL_DEPTH:
        flags.append("INSUFFICIENT_EXTERNAL_DEPTH")
    # High means complete book data, not certainty that the midpoint is fair value.
    if "STALE_OR_MISSING_EXTERNAL_BOOK" in flags:
        confidence = "LOW"
    rows = []
    for side in ("YES", "NO"):
        side_flags = flags.copy()
        buy = prices.get("buy_" + side.lower())
        depth = (Decimal(str(sig_book["ask_quantity" if side == "YES" else "bid_quantity"]))
                 if sig_book else Decimal(0))
        if depth < MIN_SIG_DEPTH:
            side_flags.append("INSUFFICIENT_SIG_DEPTH")
        if buy is None or not 0 < buy < 1:
            side_flags.append("NO_EXECUTABLE_SIG_BUY")
        fair = (fair_yes if side == "YES" else ONE - fair_yes) if fair_yes is not None else None
        edge = fair - buy if fair is not None and buy is not None else None
        sig_mid = prices.get("yes_midpoint")
        side_mid = (sig_mid if side == "YES" else ONE - sig_mid) if sig_mid is not None else None
        signal = ("BUY_SIG_" + side if not side_flags and edge is not None and edge >= MIN_EXTERNAL_EDGE
                  else "BLOCKED" if side_flags else "NO_SIGNAL")
        rows.append({"observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
                     "race": mapping["race"], "proposition": mapping["normalized_outcome"], "side": side,
                     "sig_market_id": mapping["sig_market_id"], "sig_exchange_id": mapping["sig_exchange_id"],
                     "tournament_id": tournament_id, "mapping_version": mapping["mapping_version"],
                     "tournament_slug": mapping["tournament_slug"],
                     "sig_market_status": market.get("status"),
                     "external_market_open": external["open"] if external else None,
                     "mapping_confidence": "EXACT_RESEARCH_ONLY" if status == "PASS" else status,
                     "source_urls": mapping["source_urls"],
                     "sig_root_sha256": mapping["sig_root_sha256"],
                     "polymarket_rules_sha256": mapping["polymarket_rules_sha256"],
                     "sig_expected_resolution_date": mapping["sig_expected_resolution_date"],
                     "polymarket_expected_resolution_date": mapping["polymarket_expected_resolution_date"],
                     "polymarket_market_id": mapping["polymarket_market_id"],
                     "polymarket_condition_id": mapping["polymarket_condition_id"],
                     "polymarket_tokens": mapping["polymarket_tokens"],
                     "polymarket_reference_outcome": reference_side,
                     "sig_buy": buy, "sig_sell": prices.get("sell_" + side.lower()), "sig_depth": depth,
                     "sig_bid_depth": Decimal(str(sig_book["bid_quantity"])) if sig_book else None,
                     "sig_ask_depth": Decimal(str(sig_book["ask_quantity"])) if sig_book else None,
                     "sig_sell_depth": Decimal(str(sig_book["bid_quantity" if side == "YES" else "ask_quantity"])) if sig_book else None,
                     "sig_buy_yes": prices.get("buy_yes"), "sig_buy_no": prices.get("buy_no"),
                     "sig_sell_yes": prices.get("sell_yes"), "sig_sell_no": prices.get("sell_no"),
                     "sig_yes_midpoint": sig_mid, "sig_spread": prices.get("spread"),
                     "sig_book_at": sig_book["quoted_at"] if sig_book else None,
                     "polymarket_books": books, "external_fair": fair, "external_fair_yes": fair_yes,
                     "external_fair_no": ONE - fair_yes if fair_yes is not None else None,
                     "external_edge": edge, "absolute_probability_difference": abs(fair - side_mid) if fair is not None and side_mid is not None else None,
                     "external_bid": reference_book["bid"] if reference_book else None,
                     "external_ask": reference_book["ask"] if reference_book else None,
                     "external_midpoint": reference_book["midpoint"] if reference_book else None,
                     "external_spread": reference_book["spread"] if reference_book else None,
                     "external_depth": min(reference_book["bid_depth"], reference_book["ask_depth"]) if reference_book else Decimal(0),
                     "external_book_at": reference_book["book_at"] if reference_book else None,
                     "external_headline": external["headline"] if external else None,
                     "external_reference_at": external["raw"].get("updatedAt") if external else None,
                     "external_liquidity": external["liquidity"] if external else None,
                     "external_volume": external["volume"] if external else None,
                     "reference_type": reference_type, "data_confidence": confidence, "signal": signal,
                     "flags": sorted(set(side_flags)), "settlement_notes": mapping["settlement_notes"]})
    return rows


def rank_rows(rows):
    # Rank cheap SIG sides by positive probability-point edge, never by politics.
    return sorted(rows, key=lambda r: (-(max(r["external_edge"], Decimal(0)) if r["external_edge"] is not None else Decimal(0)),
                                      r["race"], r["sig_market_id"], r["side"]))


def scan(sig_session, poly_session, mappings):
    tid = sig.get_tournament(sig_session, mappings[0]["tournament_slug"])
    rows = []
    for mapping in mappings:
        mid = mapping["sig_market_id"]
        market = sig.fetch_json(sig_session, f"{sig.API_BASE_URL}/markets/{mid}", {"tournamentId": tid})
        sig.check_context(market["contexts"], tid)
        poly.require(str(market["id"]) == mid and sig.get_exchange_id(market) == mapping["sig_exchange_id"], "SIG_MAPPING_NEEDS_REVALIDATION")
        tree = sig.fetch_json(sig_session, f"{sig.API_BASE_URL}/markets/{mid}/nodes", {"tournamentId": tid})
        sig.check_context(tree["contexts"], tid)
        poly.require(str(tree["market_id"]) == mid, "SIG_MAPPING_NEEDS_REVALIDATION")
        external, books, errors = None, {}, []
        if mapping["mapping_status"] == "APPROVED_RESEARCH_ONLY":
            try:
                external = poly.read_market(poly_session, mapping["polymarket_market_id"])
                for side, token in external["tokens"].items():
                    try:
                        books[side] = poly.read_book(poly_session, token, external["condition_id"])
                    except poly.ExternalDataError as error:
                        errors.append("EXTERNAL_" + side.upper() + "_BOOK_UNAVAILABLE:" + str(error))
                    except (KeyError, TypeError, ValueError):
                        errors.append("EXTERNAL_" + side.upper() + "_BOOK_UNAVAILABLE")
            except poly.ExternalDataError as error:
                errors.append("EXTERNAL_METADATA_UNAVAILABLE:" + str(error))
            except (KeyError, TypeError, ValueError):
                errors.append("EXTERNAL_METADATA_UNAVAILABLE")
        # Fetch SIG last to keep its existing five-second authoritative-quote gate.
        try:
            book = sig.get_best_prices(sig_session, market, tid)
        except sig.API_ERRORS:
            book = None
            errors.append("SIG_BOOK_UNAVAILABLE")
        rows.extend(compare(mapping, market, tree["root"], external, books, book, tid, errors=errors))
    return rank_rows(rows)


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("Unsupported research snapshot value")


def save_snapshots(rows, path=SNAPSHOT_PATH):
    """Atomic research-only ingestion, deduplication and offline shadow evaluation."""
    from external_shadow_tracker import collect
    return collect(rows, path, minimum_edge=MIN_EXTERNAL_EDGE)


def print_report(rows):
    def price(value):
        return "—" if value is None else f"{value:.4f}"
    print("READ-ONLY external value research | threshold 3 percentage points | no execution")
    print("Race | SIG proposition/side (market/exchange) | SIG buy | External fair | Edge | SIG depth | Poly depth | Mapping | Signal")
    for r in rows:
        print(f"{r['race']} | {r['proposition']} {r['side']} ({r['sig_market_id']}/{r['sig_exchange_id']}) | "
              f"{price(r['sig_buy'])} | {price(r['external_fair'])} | {price(r['external_edge'])} | "
              f"{r['sig_depth']} | {r['external_depth']} | {r['mapping_confidence']} | {r['signal']}")
    # Details once per instrument: all executable buy/sell prices, both external
    # outcome books, timestamps, reference type and explicit settlement limitations.
    for r in rows:
        if r["side"] != "YES":
            continue
        print(f"\n{r['race']} / {r['proposition']} | observed {r['observed_at']}")
        print(f"  SIG buy YES/NO {price(r['sig_buy_yes'])}/{price(r['sig_buy_no'])}; sell YES/NO "
              f"{price(r['sig_sell_yes'])}/{price(r['sig_sell_no'])}; midpoint {price(r['sig_yes_midpoint'])}; spread {price(r['sig_spread'])}")
        for side, b in r["polymarket_books"].items():
            print(f"  Poly {side}: bid/ask {price(b['bid'])}/{price(b['ask'])}; midpoint {price(b['midpoint'])}; "
                  f"depth {b['bid_depth']}/{b['ask_depth']}; book_at {b['book_at']}")
        print(f"  Poly market {r['polymarket_market_id']}; headline {r['external_headline']}; "
              f"liquidity {r['external_liquidity']}; volume {r['external_volume']}")
        print(f"  {r['reference_type']} / {r['data_confidence']}; flags {', '.join(r['flags']) or 'none'}")
        print(f"  Scheduled resolution: SIG {r['sig_expected_resolution_date']}; Poly {r['polymarket_expected_resolution_date']}")
        print("  Settlement: " + " ".join(r["settlement_notes"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print full timestamped comparison rows as JSON")
    parser.add_argument("--snapshots", action="store_true", help="Append research-only history to external_research/comparisons.sqlite3")
    parser.add_argument("--loop", action="store_true", help="Repeat GET-only collection every 60 seconds; Ctrl+C stops cleanly")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("Set SIG_API_KEY locally before reading SIG quotes.")
        return 1
    try:
        mappings = load_mappings()
        with poly.ReadOnlySession("SIG") as sig_session, poly.ReadOnlySession("POLYMARKET") as poly_session:
            sig_session.headers["Authorization"] = "Bearer " + key
            while True:
                started = time.monotonic()
                try:
                    rows = scan(sig_session, poly_session, mappings)
                    if args.snapshots:
                        print("Research history: " + json.dumps(save_snapshots(rows), sort_keys=True))
                    if args.json:
                        print(json.dumps(rows, default=json_default, indent=2))
                    else:
                        print_report(rows)
                except (poly.ExternalDataError, *sig.API_ERRORS, OSError, sqlite3.Error, ValueError):
                    # A failed research read has no trading state to halt or recover.
                    print("READ_ONLY_SCAN_UNAVAILABLE | cycle skipped; no signal approved")
                    if not args.loop:
                        return 1
                if not args.loop:
                    return 0
                time.sleep(max(0, 60 - (time.monotonic() - started)))
    except (poly.ExternalDataError, *sig.API_ERRORS, OSError, sqlite3.Error):
        # Do not expose request headers, bearer tokens, account data or raw exceptions.
        print("READ_ONLY_SCAN_UNAVAILABLE | mapping/API/research storage could not be validated; no signal approved")
        return 1
    except KeyboardInterrupt:
        print("Read-only external scan stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
