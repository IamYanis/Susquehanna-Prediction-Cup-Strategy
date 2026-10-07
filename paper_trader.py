"""Paper trades, a local portfolio, and a CSV trade log. No real orders."""
import csv
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

# ---------------------------------------
# Paper trading configuration
# ---------------------------------------

# Fake starting balance for our simulation
STARTING_BALANCE = 5_000

# Maximum amount of paper capital allowed
# in a single trade
MAX_CAPITAL_PER_TRADE = 250

# Maximum total capital allowed
# across all positions in the same race
MAX_CAPITAL_PER_RACE = 150

# Keep the file beside this script, even when launched from another directory.
# This file contains only simulated cash and positions, never API credentials.
PORTFOLIO_PATH = Path(__file__).resolve().with_name("paper_portfolio.json")
PORTFOLIO_VERSION = 1
TRADE_LOG_PATH = Path(__file__).resolve().with_name("paper_trades.csv")
TRADE_LOG_FIELDS = (
    "trade_id", "timestamp_utc", "race", "position_type", "quantity",
    "cost_per_pair", "capital_used", "minimum_expected_profit", "cash_remaining",
)

# Current available paper balance
paper_balance = STARTING_BALANCE

# List of simulated open positions
open_positions = []


class PortfolioError(Exception):
    """A portfolio failure must stop scanning, unlike a temporary API failure."""


def is_finite_number(value):
    """JSON booleans are not money, and NaN/infinity cannot be used safely."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def parse_api_timestamp(value):
    """Read an aware ISO timestamp, including the engine's seven decimal places.

    Python 3.9 accepts only three or six fractional digits. Keep microseconds
    and discard finer precision, which does not affect our seconds-based checks.
    """
    if not isinstance(value, str):
        raise ValueError("API timestamp must be a string")
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"\.[0-9]+", lambda match: match.group()[:7].ljust(7, "0"), normalized)
    timestamp = datetime.fromisoformat(normalized)
    if timestamp.tzinfo is None:
        raise ValueError("API timestamp must include a timezone")
    return timestamp


def validate_trade_metadata(position):
    """Older positions have no log metadata; new ones must have all three fields."""
    fields = ("trade_id", "timestamp_utc", "cash_remaining")
    if not any(field in position for field in fields):
        return
    if not all(field in position for field in fields):
        raise ValueError("Incomplete saved trade log metadata")
    trade_id = position["trade_id"]
    if not isinstance(trade_id, str) or UUID(hex=trade_id).hex != trade_id:
        raise ValueError("Invalid saved trade ID")
    timestamp = position["timestamp_utc"]
    if not isinstance(timestamp, str):
        raise ValueError("Invalid saved trade timestamp")
    timestamp = datetime.fromisoformat(timestamp)
    if timestamp.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Saved trade timestamp must include the UTC offset")
    cash = position["cash_remaining"]
    if not is_finite_number(cash) or not 0 <= cash <= STARTING_BALANCE:
        raise ValueError("Invalid saved cash after trade")


def validate_market_context(position):
    """Older holdings have no provenance; recorded API metadata must be usable."""
    if "market_context" not in position:
        return
    context = position["market_context"]
    if not isinstance(context, dict):
        raise ValueError("Invalid saved market context")
    tournament_id = context.get("tournament_id")
    if not isinstance(tournament_id, str) or str(UUID(tournament_id)) != tournament_id:
        raise ValueError("Invalid saved competition ID")
    for name in ("market_ids", "exchange_ids"):
        identifiers = context.get(name)
        if (not isinstance(identifiers, list) or len(identifiers) != 2
                or any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)
                       for value in identifiers)
                or identifiers[0] == identifiers[1]):
            raise ValueError("Invalid saved market/exchange IDs")
    fingerprint = context.get("settlement_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Invalid saved settlement fingerprint")
    race_key = context.get("race_key")
    if not isinstance(race_key, list) or len(race_key) != 5 or any(not isinstance(value, str) for value in race_key):
        raise ValueError("Invalid saved race identity")
    relationships = context.get("relationships")
    if not isinstance(relationships, list) or not relationships:
        raise ValueError("Missing saved relationship evidence")
    wanted = set(zip(context["exchange_ids"], context["market_ids"]))
    supported = False
    for relationship in relationships:
        if (not isinstance(relationship, dict) or not isinstance(relationship.get("id"), str)
                or type(relationship.get("version")) is not int or relationship["version"] <= 0
                or type(relationship.get("isExhaustive")) is not bool):
            raise ValueError("Invalid saved relationship evidence")
        UUID(relationship["id"])
        members = relationship.get("members")
        if (not isinstance(members, list)
                or any(not isinstance(member, (list, tuple)) or len(member) != 2
                       or any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)
                              for value in member) for member in members)):
            raise ValueError("Invalid saved relationship members")
        members = {tuple(member) for member in members}
        if wanted.issubset(members):
            if position["position_type"] == "NO-PAIR" or (relationship["isExhaustive"] and members == wanted):
                supported = True
    if not supported:
        raise ValueError("Saved relationship does not support the position type")
    if context.get("payout_condition") != "ordinary_binary_settlement; refunds are separate":
        raise ValueError("Invalid saved payout condition")
    prices = context.get("leg_prices")
    if (not isinstance(prices, list) or len(prices) != 2
            or any(not is_finite_number(price) or not 0 <= price <= 1 for price in prices)
            or not math.isclose(sum(prices), position["cost_per_pair"], rel_tol=0, abs_tol=1e-6)):
        raise ValueError("Saved leg prices do not match the pair cost")
    versions = context.get("book_versions")
    if not isinstance(versions, list) or len(versions) != 2:
        raise ValueError("Missing saved book versions")
    for version in versions:
        if (not isinstance(version, dict) or type(version.get("sequence")) is not int
                or version["sequence"] < 0 or not isinstance(version.get("at"), str)):
            raise ValueError("Invalid saved book version")
        parse_api_timestamp(version["at"])


def validate_portfolio(data):
    """Check the whole snapshot before trusting it or changing memory."""
    if not isinstance(data, dict):
        raise ValueError("Portfolio must be a JSON object")
    if type(data.get("version")) is not int or data["version"] != PORTFOLIO_VERSION:
        raise ValueError("Unsupported portfolio version")
    if (not is_finite_number(data.get("starting_balance"))
            or data["starting_balance"] != STARTING_BALANCE):
        raise ValueError("Saved starting balance does not match this configuration")

    cash = data.get("paper_balance")
    positions = data.get("open_positions")
    if not is_finite_number(cash) or not 0 <= cash <= STARTING_BALANCE:
        raise ValueError("Invalid saved paper cash")
    if not isinstance(positions, list):
        raise ValueError("Open positions must be a list")

    seen_positions = set()
    seen_instruments = set()
    seen_trade_ids = set()
    race_exposure = {}
    pair_exposure = {}
    running_capital = 0
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("Invalid saved position")
        race = position.get("race")
        position_type = position.get("position_type")
        if not isinstance(race, str) or not race.strip():
            raise ValueError("Saved position needs a race name")
        if position_type not in ("YES-PAIR", "NO-PAIR"):
            raise ValueError("Invalid saved position type")
        key = (race, position_type)
        if key in seen_positions:
            raise ValueError("Duplicate saved race/position")
        seen_positions.add(key)

        for field in ("quantity", "cost_per_pair", "capital_used", "minimum_profit"):
            if not is_finite_number(position.get(field)):
                raise ValueError(f"Invalid saved {field}")
        quantity = position["quantity"]
        cost = position["cost_per_pair"]
        capital = position["capital_used"]
        profit = position["minimum_profit"]
        if quantity <= 0 or not 0 <= cost < 1 or profit <= 0:
            raise ValueError("Invalid saved position values")
        # Compare with a tiny tolerance for normal floating-point rounding.
        if not math.isclose(capital, cost * quantity, rel_tol=0, abs_tol=1e-6):
            raise ValueError("Saved position cost does not match its quantity")
        if not math.isclose(profit, (1 - cost) * quantity, rel_tol=0, abs_tol=1e-6):
            raise ValueError("Saved position profit does not match its pair cost")
        if capital < 0 or capital > MAX_CAPITAL_PER_TRADE:
            raise ValueError("Saved position exceeds the trade capital limit")
        race_exposure[race] = race_exposure.get(race, 0) + capital
        if race_exposure[race] > MAX_CAPITAL_PER_RACE:
            raise ValueError("Saved positions exceed the race capital limit")
        validate_trade_metadata(position)
        validate_market_context(position)
        context = position.get("market_context")
        if context is not None:
            pair = (context["tournament_id"], tuple(sorted(context["exchange_ids"])))
            instrument_position = (pair, position_type)
            if instrument_position in seen_instruments:
                raise ValueError("Duplicate saved instrument position")
            seen_instruments.add(instrument_position)
            pair_exposure[pair] = pair_exposure.get(pair, 0) + capital
            if pair_exposure[pair] > MAX_CAPITAL_PER_RACE:
                raise ValueError("Saved instruments exceed the race capital limit")
        running_capital += capital
        if "trade_id" in position:
            if position["trade_id"] in seen_trade_ids:
                raise ValueError("Duplicate saved trade ID")
            seen_trade_ids.add(position["trade_id"])
            # Positions are stored in trade order. This records cash at entry,
            # rather than substituting today's balance when rebuilding the CSV.
            if not math.isclose(position["cash_remaining"], STARTING_BALANCE - running_capital,
                                rel_tol=0, abs_tol=1e-6):
                raise ValueError("Saved cash after trade does not match its position order")

    # Nothing settles yet: all starting cash is either available or tied up.
    total_capital = sum(position["capital_used"] for position in positions)
    if not math.isclose(cash + total_capital, STARTING_BALANCE, rel_tol=0, abs_tol=1e-6):
        raise ValueError("Saved cash and position costs do not add up to the starting balance")


def load_portfolio():
    """Load at scanner startup. Only a missing file starts a fresh portfolio."""
    global paper_balance

    try:
        with PORTFOLIO_PATH.open(encoding="utf-8") as portfolio_file:
            data = json.load(portfolio_file)
    except FileNotFoundError:
        paper_balance = STARTING_BALANCE
        open_positions.clear()
        sync_trade_log()
        return False
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PortfolioError(f"Invalid JSON in {PORTFOLIO_PATH}; file left untouched.") from error
    except OSError as error:
        raise PortfolioError(f"Cannot read {PORTFOLIO_PATH}; check file permissions.") from error

    try:
        validate_portfolio(data)
    except ValueError as error:
        raise PortfolioError(f"Invalid portfolio in {PORTFOLIO_PATH}: {error}. File left untouched.") from error

    # Change memory only after every saved position and the cash have passed.
    paper_balance = data["paper_balance"]
    open_positions[:] = data["open_positions"]
    # Repair missing log rows before the scanner starts considering new trades.
    sync_trade_log()
    return True


def save_portfolio(balance, positions):
    """Save a proposed trade before committing its changes in memory."""
    data = {
        "version": PORTFOLIO_VERSION,
        "starting_balance": STARTING_BALANCE,
        "paper_balance": balance,
        "open_positions": positions,
    }
    temporary_path = None
    try:
        validate_portfolio(data)
        # A temporary file in the same directory lets os.replace swap the file
        # atomically: readers see either the old snapshot or the complete new one.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=PORTFOLIO_PATH.parent,
            prefix=f".{PORTFOLIO_PATH.name}.", suffix=".tmp", delete=False
        ) as portfolio_file:
            temporary_path = Path(portfolio_file.name)
            json.dump(data, portfolio_file, indent=2, allow_nan=False)
            portfolio_file.write("\n")
            portfolio_file.flush()
            os.fsync(portfolio_file.fileno())
        os.replace(temporary_path, PORTFOLIO_PATH)
    except (OSError, ValueError, TypeError) as error:
        raise PortfolioError(
            f"Cannot save {PORTFOLIO_PATH}; paper trade was not accepted."
        ) from error
    finally:
        # Remove any unfinished temporary file, including on Ctrl+C.
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def trade_log_row(position):
    """Use the saved trade's original timestamp and cash, including on recovery."""
    return {
        "trade_id": position["trade_id"],
        "timestamp_utc": position["timestamp_utc"],
        "race": position["race"],
        "position_type": position["position_type"],
        "quantity": position["quantity"],
        "cost_per_pair": position["cost_per_pair"],
        "capital_used": position["capital_used"],
        "minimum_expected_profit": position["minimum_profit"],
        "cash_remaining": position["cash_remaining"],
    }


def sync_trade_log():
    """Keep existing history and add saved trades whose IDs are missing from CSV."""
    temporary_path = None
    try:
        rows = []
        logged_trades = {}
        try:
            with TRADE_LOG_PATH.open(encoding="utf-8", newline="") as log_file:
                reader = csv.DictReader(log_file, strict=True)
                if reader.fieldnames != list(TRADE_LOG_FIELDS):
                    raise ValueError("Unexpected CSV header")
                for row in reader:
                    # A truncated row, extra column, or repeated ID must not be
                    # mistaken for a complete logged trade.
                    if set(row) != set(TRADE_LOG_FIELDS) or any(value in (None, "") for value in row.values()):
                        raise ValueError("Incomplete CSV row")
                    if row["trade_id"] in logged_trades:
                        raise ValueError("Duplicate CSV trade ID")
                    rows.append(row)
                    logged_trades[row["trade_id"]] = row
        except FileNotFoundError:
            pass

        pending_rows = []
        for position in open_positions:
            # Legacy positions have no known timestamp, so do not invent one.
            if "trade_id" not in position:
                continue
            row = trade_log_row(position)
            logged_row = logged_trades.get(position["trade_id"])
            if logged_row is None:
                pending_rows.append(row)
            elif logged_row != {field: str(value) for field, value in row.items()}:
                raise ValueError("CSV row differs from its saved trade")
        if not pending_rows:
            return

        # Replace the CSV atomically rather than risking a half-written appended
        # row. History from earlier paper portfolios stays in the file too.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=TRADE_LOG_PATH.parent,
            prefix=f".{TRADE_LOG_PATH.name}.", suffix=".tmp", delete=False
        ) as log_file:
            temporary_path = Path(log_file.name)
            writer = csv.DictWriter(log_file, fieldnames=TRADE_LOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows + pending_rows)
            log_file.flush()
            os.fsync(log_file.fileno())
        os.replace(temporary_path, TRADE_LOG_PATH)
    except (OSError, ValueError, csv.Error) as error:
        raise PortfolioError(
            f"Cannot read or update {TRADE_LOG_PATH}. Saved paper positions remain open; "
            "check the log file and restart to retry missing rows."
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def execute_paper_trade(
    race,
    position_type,
    cost_per_pair,
    profit_per_pair,
    quantity,
    market_context=None,
):
    """
    Simulate an arbitrage trade without sending
    any real order to the exchange.
    """

    global paper_balance
    global open_positions

    # Reject invalid inputs before changing cash or positions.
    values = (cost_per_pair, profit_per_pair, quantity)
    if (any(not is_finite_number(value) for value in values)
            or not isinstance(race, str) or not race.strip()
            or position_type not in ("YES-PAIR", "NO-PAIR")
            or not 0 <= cost_per_pair < 1 or profit_per_pair <= 0 or quantity <= 0):
        print(f"PAPER TRADE REJECTED | {race} | Invalid trade values")
        return False
    if market_context is not None:
        try:
            validate_market_context({"market_context": market_context,
                                     "position_type": position_type, "cost_per_pair": cost_per_pair})
        except (ValueError, KeyError, TypeError):
            print(f"PAPER TRADE REJECTED | {race} | Invalid market provenance")
            return False

    # A position stays open even if its market opportunity disappears.
    # Restored positions also block another copy after restarting the scanner.
    def same_pair(position):
        # Title changes must not allow a second copy of the same instruments.
        old_context = position.get("market_context")
        return (isinstance(old_context, dict) and isinstance(market_context, dict)
                and old_context["tournament_id"] == market_context["tournament_id"]
                and set(old_context["exchange_ids"]) == set(market_context["exchange_ids"]))

    if any(position["position_type"] == position_type
           and (position["race"] == race or same_pair(position)) for position in open_positions):
        print(f"PAPER TRADE SKIPPED | {race} | {position_type} already open")
        return False

    # Calculate how much capital this trade requires
    capital_used = cost_per_pair * quantity

    # This is a projection assuming both markets settle normally and the pair's
    # payout assumption holds. Cancellation refunds are not a guaranteed profit.
    minimum_profit = profit_per_pair * quantity

    # ---------------------------------------
    # Risk check 1:
    # Maximum capital per trade
    # ---------------------------------------

    if capital_used > MAX_CAPITAL_PER_TRADE:
        print()
        print("----- PAPER TRADE REJECTED -----")
        print("Race:", race)
        print("Reason: Trade exceeds capital limit")
        print("Capital required:", round(capital_used, 2))
        print("Maximum allowed:", MAX_CAPITAL_PER_TRADE)
        print("--------------------------------")
        return False

    # ---------------------------------------
    # Risk check 2:
    # Maximum total exposure per race
    # ---------------------------------------

    # Add up all capital already tied up
    # in this same race
    current_race_exposure = sum(
        position["capital_used"]
        for position in open_positions
        if position["race"] == race or same_pair(position)
    )

    # Calculate what total race exposure
    # would become after this new trade
    new_race_exposure = (
        current_race_exposure
        + capital_used
    )

    # Reject if this would exceed our race limit
    if new_race_exposure > MAX_CAPITAL_PER_RACE:
        print()
        print("----- PAPER TRADE REJECTED -----")
        print("Race:", race)
        print("Reason: Race exposure limit exceeded")
        print(
            "Current race exposure:",
            round(current_race_exposure, 2)
        )
        print(
            "New capital required:",
            round(capital_used, 2)
        )
        print(
            "Exposure after trade:",
            round(new_race_exposure, 2)
        )
        print(
            "Maximum allowed per race:",
            MAX_CAPITAL_PER_RACE
        )
        print("--------------------------------")
        return False

    # ---------------------------------------
    # Risk check 3:
    # Check available paper balance
    # ---------------------------------------

    if capital_used > paper_balance:
        print()
        print("----- PAPER TRADE REJECTED -----")
        print("Race:", race)
        print("Reason: Not enough paper balance")
        print(
            "Capital required:",
            round(capital_used, 2)
        )
        print(
            "Available balance:",
            round(paper_balance, 2)
        )
        print("--------------------------------")
        return False

    # ---------------------------------------
    # Execute simulated trade
    # ---------------------------------------

    new_balance = paper_balance - capital_used
    # Describe the new position without changing the current portfolio yet.
    position = {
        "race": race,
        "position_type": position_type,
        "quantity": quantity,
        "cost_per_pair": cost_per_pair,
        "capital_used": capital_used,
        "minimum_profit": minimum_profit,
        # Persist log metadata with the trade so a missing CSV row can be retried.
        "trade_id": uuid4().hex,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cash_remaining": new_balance,
    }
    if market_context is not None:
        # Only public market/rule/book metadata belongs here, never headers/keys.
        position["market_context"] = market_context

    new_positions = open_positions + [position]
    # If saving fails, this raises PortfolioError and stops the scanner.
    # The current cash, positions, and previous portfolio file stay intact.
    save_portfolio(new_balance, new_positions)
    paper_balance = new_balance
    open_positions.append(position)

    print()
    print("----- PAPER TRADE -----")
    print("Race:", race)
    print("Position:", position_type)
    print("Quantity:", quantity)
    print(
        "Capital used:",
        round(capital_used, 2)
    )
    print(
        "Projected profit (normal settlement):",
        round(minimum_profit, 2)
    )
    print(
        "Race exposure:",
        round(new_race_exposure, 2)
    )
    print(
        "Remaining cash:",
        round(paper_balance, 2)
    )
    print("-----------------------")
    # The portfolio is already safely saved. If CSV writing fails, the scanner
    # stops with that position still open and repairs its log row on next startup.
    sync_trade_log()
    return True


def print_portfolio_summary():
    """
    Print a summary of the simulated portfolio.
    """

    # Total capital currently tied up
    capital_tied_up = sum(
        position["capital_used"]
        for position in open_positions
    )

    # Historical field names remain compatible with saved portfolios and CSVs.
    # These figures are conditional projections, not guaranteed/realized gains.
    total_minimum_profit = sum(
        position["minimum_profit"]
        for position in open_positions
    )

    print()
    print("===== PAPER PORTFOLIO =====")
    print(
        "Starting balance:",
        round(STARTING_BALANCE, 2)
    )
    print(
        "Cash remaining:",
        round(paper_balance, 2)
    )
    print(
        "Capital tied up:",
        round(capital_tied_up, 2)
    )
    print(
        "Open positions:",
        len(open_positions)
    )
    print(
        "Projected profit (normal settlement):",
        round(total_minimum_profit, 2)
    )
    # Show holdings at entry cost; expected profit is not realized cash.
    for position in open_positions:
        print(f"  {position['race']} | {position['position_type']} | "
              f"qty {position['quantity']:g} | cost {position['capital_used']:.2f} paper SUSQies")
    print("Local simulation in paper SUSQies; separate from the competition account.")
    print("Profit assumes valid pair rules and normal settlement; refunds can erase the gain.")
    legacy_count = sum("market_context" not in position for position in open_positions)
    if legacy_count:
        print(f"Positions without recorded API provenance: {legacy_count} (historical simulations)")
    print("===========================")
