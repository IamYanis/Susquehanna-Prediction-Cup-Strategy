"""Repeated, paper-only scanner. Run with --once for a single scan."""
import argparse
import math
import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from paper_trader import (
    PortfolioError,
    execute_paper_trade,
    load_portfolio,
    print_portfolio_summary,
)

MIN_EDGE = 0.02
MIN_LIQUIDITY = 50
MAX_TRADE_QUANTITY = 100
SCAN_INTERVAL = 15
REQUEST_TIMEOUT = 10
# Compare with the last reported values so small changes can accumulate.
MATERIAL_EDGE_CHANGE = 0.01
MATERIAL_QUANTITY_CHANGE = 10
MARKETS_URL = "https://sig.thesuper.market/api/v1/markets"


def fetch_json(session, url):
    """Bound each request and reject HTTP failures before reading JSON."""
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_races(session):
    """Match the two party markets by the race text in their titles."""
    markets = fetch_json(session, MARKETS_URL)["data"]
    if not isinstance(markets, list):
        raise ValueError("Invalid market list")
    parties = {"Democratic": {}, "Republican": {}}
    for market in markets:
        title = market["title"]
        for party in parties:
            prefix = f"Will the {party} Party win the "
            if title.startswith(prefix):
                race = title[len(prefix):].removesuffix("?").strip()
                parties[party][race] = market["id"]
    return {
        race: (market_id, parties["Republican"][race])
        for race, market_id in parties["Democratic"].items()
        if race in parties["Republican"]
    }


def get_best_prices(session, market_id):
    """Read the first exchange, preserving the original scanner's convention."""
    url = f"https://www.thesuper.market/api/v1/markets/{market_id}/orderbook?depth=1"
    exchanges = fetch_json(session, url)["exchanges"]
    if not exchanges:
        return None
    exchange = exchanges[0]
    if not exchange["bids"] or not exchange["asks"]:
        return None
    result = {}
    for side, name in (("bids", "bid"), ("asks", "ask")):
        level = exchange[side][0]
        price, quantity = float(level["price"]), float(level["quantity"])
        if not math.isfinite(price) or not 0 <= price <= 1:
            raise ValueError("Invalid price")
        if not math.isfinite(quantity) or quantity < 0:
            raise ValueError("Invalid quantity")
        result[name], result[name + "_quantity"] = price, quantity
    if result["bid"] > result["ask"]:
        raise ValueError("Crossed order book")
    return result


def find_opportunities(democrat, republican):
    """Use asks for YES purchases and complementary bid prices for NO."""
    if democrat is None or republican is None:
        return {}
    opportunities = {}
    for position_type, side in (("YES-PAIR", "ask"), ("NO-PAIR", "bid")):
        total = democrat[side] + republican[side]
        cost = total if position_type == "YES-PAIR" else 2 - total
        edge = 1 - cost
        available = min(democrat[side + "_quantity"], republican[side + "_quantity"])
        # Small tolerance prevents decimal rounding from rejecting exactly 2%.
        if edge + 1e-12 >= MIN_EDGE and available >= MIN_LIQUIDITY:
            opportunities[position_type] = {
                "cost_per_pair": cost,
                "profit_per_pair": edge,
                "quantity": min(available, MAX_TRADE_QUANTITY),
            }
    return opportunities


def report_race(race, current, previous):
    """Update only successfully observed races; disappearance never closes a trade."""
    for position_type in ("YES-PAIR", "NO-PAIR"):
        key = (race, position_type)
        old, new = previous.get(key), current.get(position_type)
        if new is None:
            if old is not None:
                print(f"DISAPPEARED | {race} | {position_type}")
                del previous[key]
            continue
        changed = old is not None and (
            abs(new["profit_per_pair"] - old["profit_per_pair"]) + 1e-12 >= MATERIAL_EDGE_CHANGE
            or abs(new["quantity"] - old["quantity"]) >= MATERIAL_QUANTITY_CHANGE
        )
        if old is None or changed:
            event = "APPEARED" if old is None else "CHANGED"
            print(f"{event} | {race} | {position_type} | "
                  f"edge {new['profit_per_pair']:.2%} | quantity {new['quantity']:g}")
            previous[key] = new.copy()
            execute_paper_trade(race=race, position_type=position_type, **new)


API_ERRORS = (requests.RequestException, ValueError, KeyError, TypeError, IndexError)


def scan_once(session, previous):
    """Failures mean unknown, rather than falsely reporting disappearance."""
    try:
        races = get_races(session)
    except API_ERRORS:
        # Do not print raw exceptions: request details could contain credentials.
        print("Market list unavailable or invalid; retaining previous observations.")
        return
    for race, (democrat_id, republican_id) in races.items():
        try:
            democrat = get_best_prices(session, democrat_id)
            republican = get_best_prices(session, republican_id)
            current = find_opportunities(democrat, republican)
        except API_ERRORS:
            print(f"Order book unavailable or invalid | {race} | retaining previous observations")
            continue
        report_race(race, current, previous)
    # Only a successful market-list response can establish that a race was removed.
    for race in {key[0] for key in previous} - races.keys():
        report_race(race, {}, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Scan once, then exit")
    args = parser.parse_args()
    load_dotenv()
    api_key = os.getenv("SIG_API_KEY")
    if not api_key:
        print("Set SIG_API_KEY in your local .env before running the scanner.")
        return 1
    # Load saved holdings before fetching markets or opening any paper trades.
    try:
        restored = load_portfolio()
    except PortfolioError as error:
        print(f"Scanner stopped: {error}")
        return 1
    print("Saved paper portfolio loaded." if restored else "No saved portfolio; starting with $5,000 paper cash.")
    previous = {}
    print("Paper-only scanner started. Ctrl+C stops it. Paper positions are saved locally.")
    print("Pair payouts depend on the original race settlement assumptions; no fees are modeled.")
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            while True:
                started = time.monotonic()
                print(f"\nScan {datetime.now().astimezone().isoformat(timespec='seconds')}")
                scan_once(session, previous)
                print_portfolio_summary()
                if args.once:
                    break
                # Aim for 15 seconds between starts; never overlap scans.
                time.sleep(max(0, SCAN_INTERVAL - (time.monotonic() - started)))
    except PortfolioError as error:
        print(f"\nScanner stopped: {error}")
        print_portfolio_summary()
        return 1
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        print_portfolio_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
