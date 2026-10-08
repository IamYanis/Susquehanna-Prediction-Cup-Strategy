"""Read-only pair research: apparent price gaps never authorize a trade."""
import argparse
import csv
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

from order_preview import ceil_buy_limit
from price_reader import (
    API_BASE_URL, API_ERRORS, MAX_TRADE_QUANTITY, MIN_EDGE, MIN_LIQUIDITY,
    DataValidationError, RateLimitError, api_error_reason, fetch_pages,
    get_best_prices, get_election_rule, get_exchange_id, get_pair_rules,
    get_races, get_tournament, numeric_id,
)

REPORT_PATH = Path(__file__).resolve().with_name("research_report.csv")
PAIR_REPORT_PATH = REPORT_PATH.with_name("research_pair_report.csv")
FIELDS = (
    "observed_at", "tournament_id", "race", "position_type", "dem_market_id",
    "rep_market_id", "dem_exchange_id", "rep_exchange_id", "dem_buy_price",
    "rep_buy_price", "raw_combined_cost", "dem_limit", "rep_limit",
    "combined_limit_cost", "apparent_price_gap", "dem_visible_shares",
    "rep_visible_shares", "available_pairs", "research_quantity",
    "meets_price_and_depth_thresholds", "dem_book_at", "rep_book_at",
    "quote_status", "settlement_status", "settlement_note", "rules_review",
    "execution_approved",
)


def pair_rows(session, race, markets, tournament_id, relationships, relationship_error=""):
    """Read prices even when settlement evidence cannot approve the pair.

    This deliberately does NOT call the paper trader, account reader or order
    endpoints. A gap relative to one unit is hypothetical until payout is proved.
    """
    democrat, republican = markets
    base = {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tournament_id": tournament_id, "race": race,
        "dem_market_id": democrat["id"], "rep_market_id": republican["id"],
        "settlement_status": "UNVERIFIED", "settlement_note": relationship_error,
        "rules_review": "NOT_INSPECTED", "execution_approved": False,
        "meets_price_and_depth_thresholds": False,
    }
    rows = [dict(base, position_type=kind) for kind in ("YES-PAIR", "NO-PAIR")]
    if any(market["status"] != "open" for market in markets):
        for row in rows:
            row.update(quote_status="MARKET_CLOSED", settlement_note="One or both markets are closed")
        return rows

    # Compute immediately after reading both books. Settlement reads later in
    # this function must not make an old book appear freshly executable.
    try:
        exchange_ids = [get_exchange_id(market) for market in markets]
        if exchange_ids[0] == exchange_ids[1]:
            raise DataValidationError("Pair legs must use different exchanges")
        for row in rows:
            row.update(dem_exchange_id=exchange_ids[0], rep_exchange_id=exchange_ids[1])
        books = [get_best_prices(session, market, tournament_id) for market in markets]
        now, clock = time.monotonic(), time.time()
        for book in books:
            if book is not None and not (0 <= now - book["received_at"] <= 5
                                        and -5 <= clock - book["quoted_at"] <= 5):
                raise DataValidationError("Pair books became stale during reads")
        for row, side in zip(rows, ("ask", "bid")):
            row["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            # Preserve an available leg even if its counterpart has no quote.
            # A missing side still prevents combined-cost and gap calculations.
            for prefix, book in zip(("dem", "rep"), books):
                if book is not None:
                    row[prefix + "_book_at"] = book["version"]["at"]
                    row[prefix + "_visible_shares"] = book[side + "_quantity"]
                    if book[side] is not None:
                        row[prefix + "_buy_price"] = float(Decimal(str(book[side])) if side == "ask"
                                                          else Decimal(1) - Decimal(str(book[side])))
            if any(book is None or book[side] is None for book in books):
                row["quote_status"] = "MISSING_BOOK_SIDE"
                continue
            # YES buys use the ask; NO buys use 1 minus the YES bid.
            prices = [Decimal(str(book[side])) if side == "ask"
                      else Decimal(1) - Decimal(str(book[side])) for book in books]
            quantities = [book[side + "_quantity"] for book in books]
            row.update(dem_buy_price=float(prices[0]), rep_buy_price=float(prices[1]),
                       raw_combined_cost=float(sum(prices)),
                       dem_visible_shares=quantities[0], rep_visible_shares=quantities[1],
                       available_pairs=min(quantities),
                       dem_book_at=books[0]["version"]["at"], rep_book_at=books[1]["version"]["at"])
            try:
                limits = [ceil_buy_limit(float(price)) for price in prices]
            except ValueError:
                row["quote_status"] = "NO_SUPPORTED_LIMIT_PRICE"
                continue
            total = sum(Decimal(str(limit)) for limit in limits)
            gap = Decimal(1) - total
            available = min(quantities)
            row.update(dem_limit=limits[0], rep_limit=limits[1], combined_limit_cost=float(total),
                       apparent_price_gap=float(gap), quote_status="OBSERVED",
                       research_quantity=min(math.floor(available), MAX_TRADE_QUANTITY),
                       meets_price_and_depth_thresholds=(gap >= Decimal(str(MIN_EDGE))
                                                          and available >= MIN_LIQUIDITY))
    except RateLimitError:
        raise
    except API_ERRORS as error:
        for row in rows:
            # Drop partial observations rather than ranking a failed pair read.
            for field in FIELDS[8:23]:
                if field not in ("meets_price_and_depth_thresholds",):
                    row.pop(field, None)
            row.update(quote_status=api_error_reason(error), meets_price_and_depth_thresholds=False)

    # Canonical evidence still goes through the scanner's original approval
    # checks. A missing graph is recorded, never replaced by a title assumption.
    if not relationship_error:
        try:
            allowed, _ = get_pair_rules(session, democrat, republican, tournament_id,
                                        relationships=relationships)
            for row in rows:
                row.update(settlement_status="VERIFIED_NORMAL_SETTLEMENT"
                           if row["position_type"] in allowed else "UNVERIFIED",
                           settlement_note="Matching structured rules and active engine relationship"
                           if row["position_type"] in allowed else "Relationship does not prove this pair type",
                           rules_review="MATCHING_STRUCTURED_RULES")
        except RateLimitError:
            raise
        except API_ERRORS as error:
            for row in rows:
                row["settlement_note"] = api_error_reason(error)
    return rows


def ranked_rows(rows):
    """Threshold-sized gaps first, then smaller gaps; unavailable quotes last."""
    return sorted(rows, key=lambda row: (
        not row["meets_price_and_depth_thresholds"],
        -row.get("apparent_price_gap", -math.inf),
        -row.get("available_pairs", 0), row["race"], row["position_type"],
    ))


def inspect_candidate(session, markets, tournament_id):
    """Inspect structured identity only; it cannot prove exclusivity or coverage.

    Even matching Party Winner rules need review for third parties, fusion
    tickets and refunds. No manual override is created from this inspection.
    """
    try:
        rules = [get_election_rule(session, market, party, tournament_id)
                 for market, party in zip(markets, ("Democratic", "Republican"))]
        if rules[0][0] != rules[1][0] or rules[0][1]["settlement_date"] != rules[1][1]["settlement_date"]:
            return "RULE_MISMATCH: contracts describe different races, stages or settlement dates"
        key = rules[0][0]
        return (f"MATCHING_IDENTITY: race {key[0]}, stage {key[1]}, election {key[2]}; "
                "third-party coverage, fusion tickets and refund treatment remain unverified")
    except RateLimitError:
        raise
    except API_ERRORS as error:
        return "UNREVIEWED: " + api_error_reason(error)


def build_report(session, tournament_id, market_ids=None):
    # Validate a requested pair before any reads. IDs are ordered D then R;
    # reversing them or matching only one leg must not select a different race.
    if market_ids is not None:
        if not isinstance(market_ids, (list, tuple)) or len(market_ids) != 2:
            raise DataValidationError("A focused report needs two market IDs")
        market_ids = tuple(numeric_id(value) for value in market_ids)
        if market_ids[0] == market_ids[1]:
            raise DataValidationError("Select two different market IDs")
    races, _ = get_races(session, tournament_id)
    if market_ids is not None:
        races = {race: markets for race, markets in races.items()
                 if tuple(numeric_id(market["id"]) for market in markets) == market_ids}
        if len(races) != 1:
            raise DataValidationError("Requested IDs are not one discovered Democratic/Republican race pair")
    print(f"Discovered {len(races)} title pairs. Reading fresh scoped books; this may take several minutes.", flush=True)
    relationship_error = ""
    try:
        relationships = fetch_pages(session, f"{API_BASE_URL}/relationships",
                                    {"tournamentId": tournament_id, "limit": 200})
    except RateLimitError:
        raise
    except API_ERRORS as error:
        relationships = []
        relationship_error = "Relationship evidence unavailable: " + api_error_reason(error)
    rows = []
    for index, (race, markets) in enumerate(races.items(), start=1):
        rows.extend(pair_rows(session, race, markets, tournament_id, relationships, relationship_error))
        if index % 10 == 0 or index == len(races):
            print(f"Researched {index}/{len(races)} pairs.", flush=True)
    rows = ranked_rows(rows)
    # Inspect the strongest positive, adequately liquid price gap first. If
    # none meets both policies, still inspect the best observed pair's identity.
    candidate = next((row for row in rows if row.get("quote_status") == "OBSERVED"), None)
    if candidate is not None:
        review = inspect_candidate(session, races[candidate["race"]], tournament_id)
        for row in rows:
            if row["race"] == candidate["race"]:
                row["rules_review"] = review
    return rows


def csv_value(value):
    # Public titles are outside our control. Stop spreadsheet applications from
    # interpreting a title/error as a formula when opening the CSV.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def save_report(rows, path):
    """Replace a completed research CSV atomically, leaving older files on error."""
    path, temporary = Path(path), None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows({key: csv_value(row.get(key, "")) for key in FIELDS} for row in rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def print_report(rows, top):
    observed = [row for row in rows if row.get("quote_status") == "OBSERVED"]
    candidates = [row for row in observed if row["meets_price_and_depth_thresholds"]]
    verified = [row for row in candidates if row["settlement_status"] == "VERIFIED_NORMAL_SETTLEMENT"]
    print(f"Report: {len(rows)} pair-type rows | {len(observed)} observed | "
          f"{len(candidates)} meet price/depth policies | {len(verified)} also have normal-payout evidence")
    print("All rows are research observations. Execution approved: FALSE. Fees/refunds and account risk are unverified.")
    print("Quotes have individual timestamps; the full report is not a simultaneous executable snapshot.")
    for row in rows[:top]:
        if row.get("quote_status") == "OBSERVED":
            print(f"{row['race']} | {row['position_type']} | markets {row['dem_market_id']}/{row['rep_market_id']} | "
                  f"buys {row['dem_buy_price']:.3f} + {row['rep_buy_price']:.3f} | "
                  f"limit cost {row['combined_limit_cost']:.3f} | apparent gap {row['apparent_price_gap']:.2%} | "
                  f"depth {row['available_pairs']:g} | {row['settlement_status']}")
        else:
            print(f"{row['race']} | {row['position_type']} | {row.get('quote_status', 'UNAVAILABLE')}")
        print(f"  Evidence: {row['settlement_note']} | rules: {row['rules_review']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tournament", default="midterm-elections")
    parser.add_argument("--output", type=Path, help="CSV destination; focused and full reports have separate defaults")
    parser.add_argument("--dem-market", help="Optional Democratic market ID for a focused report")
    parser.add_argument("--rep-market", help="Optional Republican market ID; requires --dem-market")
    parser.add_argument("--top", type=int, default=10, help="Rows to print; the CSV contains all pairs")
    args = parser.parse_args()
    if args.top < 1:
        parser.error("--top must be positive")
    if (args.dem_market is None) != (args.rep_market is None):
        parser.error("A focused report requires both --dem-market and --rep-market")
    market_ids = None if args.dem_market is None else (args.dem_market, args.rep_market)
    if args.output is None:
        args.output = REPORT_PATH if market_ids is None else PAIR_REPORT_PATH
    # Keep the report separate from portfolio, credentials and existing files.
    # Custom outputs must be CSVs and cannot resolve onto any reserved state file.
    if args.output.suffix.lower() != ".csv" or args.output.resolve().name.lower() == "paper_trades.csv":
        parser.error("Choose a research CSV path, separate from the paper trade log")
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    api_key = os.getenv("SIG_API_KEY")
    if not api_key:
        print("Set SIG_API_KEY locally before running research.")
        return 1
    print("READ-ONLY RESEARCH | GET requests only; no account orders or paper trades.", flush=True)
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            rows = build_report(session, get_tournament(session, args.tournament), market_ids=market_ids)
        save_report(rows, args.output)
        print_report(rows, args.top)
        print(f"Saved completed research report: {args.output}")
        return 0
    except API_ERRORS as error:
        print(f"Research stopped: {api_error_reason(error)}. No completed report saved; no orders placed.")
    except OSError:
        print("Research report could not be saved. No orders placed.")
    except KeyboardInterrupt:
        print("Research interrupted. No completed report saved; no orders placed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
