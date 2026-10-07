"""Paper trades and a local portfolio file. No real orders are submitted."""
import json
import math
import os
import tempfile
from pathlib import Path

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
    race_exposure = {}
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


def execute_paper_trade(
    race,
    position_type,
    cost_per_pair,
    profit_per_pair,
    quantity
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

    # A position stays open even if its market opportunity disappears.
    # Restored positions also block another copy after restarting the scanner.
    if any(position["race"] == race and position["position_type"] == position_type
           for position in open_positions):
        print(f"PAPER TRADE SKIPPED | {race} | {position_type} already open")
        return False

    # Calculate how much capital this trade requires
    capital_used = cost_per_pair * quantity

    # Calculate the minimum expected profit
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
        if position["race"] == race
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

    # Describe the new position without changing the current portfolio yet.
    position = {
        "race": race,
        "position_type": position_type,
        "quantity": quantity,
        "cost_per_pair": cost_per_pair,
        "capital_used": capital_used,
        "minimum_profit": minimum_profit
    }

    new_balance = paper_balance - capital_used
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
        "Minimum expected profit:",
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

    # Total minimum expected profit
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
        "Minimum expected profit:",
        round(total_minimum_profit, 2)
    )
    # Show holdings at entry cost; expected profit is not realized cash.
    for position in open_positions:
        print(f"  {position['race']} | {position['position_type']} | "
              f"qty {position['quantity']:g} | cost ${position['capital_used']:.2f}")
    print("Expected profit assumes stated settlement payouts; excludes fees.")
    print("===========================")
