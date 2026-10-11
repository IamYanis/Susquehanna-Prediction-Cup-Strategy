"""Offline, one-share shadow research. Writes only the separate research SQLite DB."""
import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

DATABASE = Path(__file__).resolve().parent / "external_research" / "comparisons.sqlite3"
MIN_EXTERNAL_EDGE = Decimal("0.03")
SHADOW_TARGET_PROFIT = Decimal("0.01")
MATERIAL_CHANGE = Decimal("0.005")  # One SIG tick, research deduplication only.
HORIZONS = (5, 15, 30, 60)
WINDOW_SECONDS = 120
ONE = Decimal("1")


def number(value):
    """Unknown numeric data stays unknown; never replace missing prices with zero."""
    try:
        result = Decimal(str(value)) if value is not None and not isinstance(value, bool) else None
        return result if result is not None and result.is_finite() else None
    except InvalidOperation:
        return None


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda x: str(x) if isinstance(x, Decimal) else _unsupported(x))


def _unsupported(value):
    raise TypeError("Unsupported research value")


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def epoch(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Research timestamps must include UTC offset")
    return date.timestamp()


def mapping_key(row):
    # Bind evidence and exact outcome identity, not only a race/title.
    fields = ("tournament_id", "sig_market_id", "sig_exchange_id", "proposition", "mapping_version",
              "polymarket_market_id", "polymarket_condition_id", "polymarket_tokens",
              "polymarket_reference_outcome", "sig_root_sha256", "polymarket_rules_sha256", "source_urls")
    return fingerprint({field: row.get(field) for field in fields})


def sig_valid(row, executable=True):
    """Fresh AT the observation, not fresh relative to today's offline evaluation."""
    flags = row.get("flags", [])
    if row.get("mapping_confidence") not in ("EXACT_RESEARCH_ONLY", "EXTERNAL_DATA_UNAVAILABLE"):
        return False
    if row.get("sig_market_status") != "open" or "STALE_OR_MISSING_SIG_BOOK" in flags:
        return False
    quoted = number(row.get("sig_book_at"))
    if quoted is None or not Decimal("-5") <= number(row["observed_epoch"]) - quoted <= Decimal("5"):
        return False
    if executable is None:
        return True
    if not executable:
        return number(row.get("sig_reference")) is not None
    sell, depth = number(row.get("sig_sell")), number(row.get("sig_sell_depth"))
    return sell is not None and 0 <= sell <= 1 and depth is not None and depth >= ONE


def external_valid(row):
    if row.get("mapping_confidence") != "EXACT_RESEARCH_ONLY" or row.get("data_confidence") != "HIGH":
        return False
    if row.get("external_market_open") is not True:
        return False
    book = row.get("polymarket_books", {}).get(row.get("polymarket_reference_outcome"))
    if not book:
        return False
    observed, quoted, received = (number(row.get(k)) for k in ("observed_epoch", "external_book_at", "external_received_at"))
    return (observed is not None and quoted is not None and received is not None
            and Decimal("-5") <= observed - quoted <= Decimal("60")
            and 0 <= observed - received <= 5 and number(row.get("external_fair")) is not None)


def normalize(row, companion=None, minimum_edge=MIN_EXTERNAL_EDGE):
    result = json.loads(encode(row))  # Copy: never mutate the scanner's current rows.
    side = result["side"]
    if side not in ("YES", "NO"):
        raise ValueError("Unknown research semantic side")
    observed = epoch(result["observed_at"])
    result["observed_at"] = datetime.fromtimestamp(observed, timezone.utc).isoformat(timespec="milliseconds")
    result["observed_epoch"] = epoch(result["observed_at"])
    result["mapping_key"] = mapping_key(result)
    result["observation_id"] = fingerprint([result["observed_at"], result["mapping_key"], side])
    # Legacy v0.3 stored each side's BUY depth. Its companion supplies SELL depth.
    # Missing legacy companions remain unknown, rather than invented liquidity.
    if "sig_sell_depth" not in result:
        result["sig_sell_depth"] = companion.get("sig_depth") if companion else None
        result["sig_ask_depth"] = result.get("sig_depth") if side == "YES" else result["sig_sell_depth"]
        result["sig_bid_depth"] = result.get("sig_depth") if side == "NO" else result["sig_sell_depth"]
    result.setdefault("sig_market_status", "closed" if "MARKET_NOT_OPEN" in result.get("flags", []) else "open")
    result.setdefault("external_market_open", result.get("mapping_confidence") == "EXACT_RESEARCH_ONLY"
                      and "MARKET_NOT_OPEN" not in result.get("flags", []))
    midpoint = number(result.get("sig_yes_midpoint"))
    result["sig_reference"] = midpoint if side == "YES" else ONE - midpoint if midpoint is not None else None
    result["sig_reference_type"] = "MIDPOINT" if midpoint is not None else "UNAVAILABLE"
    reference_book = result.get("polymarket_books", {}).get(result.get("polymarket_reference_outcome"), {})
    result["external_received_at"] = reference_book.get("fetched_at")
    for direction in ("yes", "no"):
        buy, fair = number(result.get("sig_buy_" + direction)), number(result.get("external_fair_" + direction))
        result["buy_" + direction + "_edge"] = fair - buy if fair is not None and buy is not None else None
    buy, fair = number(result.get("sig_buy")), number(result.get("external_fair"))
    edge = fair - buy if fair is not None and buy is not None else None
    result["external_edge"] = edge
    result["minimum_external_edge"] = minimum_edge
    result["threshold_met"] = edge is not None and edge >= minimum_edge
    result["edge_exceeded_threshold"] = edge is not None and edge > minimum_edge
    result["signal_side"] = side if result.get("signal") == "BUY_SIG_" + side else None
    result["signal_strength"] = edge
    result["freshness_flags"] = [f for f in result.get("flags", []) if "STALE" in f or "MISSING" in f]
    result["liquidity_flags"] = [f for f in result.get("flags", []) if "DEPTH" in f]
    return json.loads(encode(result))


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS comparisons (
          id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, race TEXT NOT NULL,
          sig_market_id TEXT NOT NULL, sig_exchange_id TEXT NOT NULL,
          polymarket_market_id TEXT, side TEXT NOT NULL, mapping_version INTEGER NOT NULL,
          sig_buy TEXT, sig_sell TEXT, external_bid TEXT, external_ask TEXT,
          external_midpoint TEXT, external_edge TEXT, sig_depth TEXT,
          external_depth TEXT, signal TEXT NOT NULL, record_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations (
          observation_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, observed_epoch REAL NOT NULL,
          mapping_key TEXT NOT NULL, race TEXT NOT NULL, sig_market_id TEXT NOT NULL,
          sig_exchange_id TEXT NOT NULL, side TEXT NOT NULL, record_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS observation_timeline ON observations(mapping_key,side,observed_epoch);
        CREATE TABLE IF NOT EXISTS shadow_trades (
          shadow_id TEXT PRIMARY KEY, mapping_key TEXT NOT NULL, side TEXT NOT NULL,
          entry_observation_id TEXT NOT NULL UNIQUE REFERENCES observations(observation_id),
          entry_epoch REAL NOT NULL, record_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shadow_horizons (
          shadow_id TEXT NOT NULL REFERENCES shadow_trades(shadow_id), horizon_minutes INTEGER NOT NULL,
          status TEXT NOT NULL, observation_id TEXT REFERENCES observations(observation_id),
          record_json TEXT NOT NULL, PRIMARY KEY(shadow_id,horizon_minutes));
    """)


def valid_entry(row):
    buy, depth = number(row.get("sig_buy")), number(row.get("sig_depth"))
    return (row["threshold_met"] and not row.get("flags") and external_valid(row)
            and sig_valid(row, executable=None) and row.get("signal") == "BUY_SIG_" + row["side"]
            and buy is not None and 0 < buy < 1 and depth is not None and depth >= ONE)


def create_shadow(db, row):
    if not valid_entry(row):
        return 0
    previous = db.execute("SELECT record_json FROM shadow_trades WHERE mapping_key=? AND side=? ORDER BY entry_epoch DESC LIMIT 1",
                          (row["mapping_key"], row["side"])).fetchone()
    if previous:
        old = json.loads(previous[0])
        if row["observed_epoch"] <= old["entry_epoch"]:
            return 0
        if (abs(number(row["sig_buy"]) - number(old["entry_price"])) < MATERIAL_CHANGE
                and abs(number(row["external_fair"]) - number(old["external_fair_at_entry"])) < MATERIAL_CHANGE):
            return 0
    shadow = {k: row[k] for k in ("race", "proposition", "side", "sig_market_id", "sig_exchange_id", "mapping_key",
                                  "mapping_confidence", "polymarket_market_id", "polymarket_condition_id", "polymarket_tokens")}
    reference = number(row["sig_reference"])
    reference_type = "MIDPOINT"
    if reference is None:
        reference = number(row["sig_buy"])
        reference_type = "ENTRY_BUY_FALLBACK"
    shadow.update(shadow_id=fingerprint(["shadow-v1", row["observation_id"]]), signal_observation_id=row["observation_id"],
                  entry_timestamp=row["observed_at"], entry_epoch=row["observed_epoch"], entry_price=row["sig_buy"],
                  external_fair_at_entry=row["external_fair"], entry_edge=row["external_edge"], quantity=1,
                  entry_executable_depth=row["sig_depth"], entry_reference=reference, entry_reference_type=reference_type,
                  initial_external_gap=number(row["external_fair"]) - reference, status="OPEN_SHADOW")
    db.execute("INSERT INTO shadow_trades VALUES (?,?,?,?,?,?)", (shadow["shadow_id"], row["mapping_key"], row["side"],
               row["observation_id"], row["observed_epoch"], encode(shadow)))
    return 1


def insert_observations(db, rows, append_legacy, minimum_edge):
    companion = {(r["observed_at"], mapping_key(r), r["side"]): r for r in rows}
    inserted = created = 0
    fields = ("observed_at", "race", "sig_market_id", "sig_exchange_id", "polymarket_market_id", "side", "mapping_version",
              "sig_buy", "sig_sell", "external_bid", "external_ask", "external_midpoint", "external_edge", "sig_depth", "external_depth", "signal")
    for original in sorted(rows, key=lambda r: epoch(r["observed_at"])):
        other = companion.get((original["observed_at"], mapping_key(original), "NO" if original["side"] == "YES" else "YES"))
        row = normalize(original, other, minimum_edge)
        saved = db.execute("SELECT record_json FROM observations WHERE observation_id=?", (row["observation_id"],)).fetchone()
        if saved:
            if saved[0] != encode(row):
                raise ValueError("Conflicting data for the same timestamped observation")
            continue
        db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)", tuple(row[k] for k in
                   ("observation_id", "observed_at", "observed_epoch", "mapping_key", "race", "sig_market_id", "sig_exchange_id", "side")) + (encode(row),))
        if append_legacy:
            db.execute("INSERT INTO comparisons (" + ",".join(fields) + ",record_json) VALUES (" + ",".join("?" for _ in range(len(fields) + 1)) + ")",
                       tuple(str(row.get(f)) if row.get(f) is not None else None for f in fields) + (encode(row),))
        inserted += 1
        created += create_shadow(db, row)
    return inserted, created


def trajectory(shadow, rows, end_epoch):
    eligible = [r for r in rows if r["observed_epoch"] <= end_epoch and sig_valid(r)]
    entry = number(shadow["entry_price"])
    values = [(r, number(r["sig_sell"]) - entry) for r in eligible]
    positive = next((r for r, pnl in values if pnl > 0), None)
    target = next((r for r, pnl in values if pnl >= SHADOW_TARGET_PROFIT), None)
    return {"first_profit_seconds": number(positive["observed_epoch"] - shadow["entry_epoch"]) if positive else None,
            "first_target_seconds": number(target["observed_epoch"] - shadow["entry_epoch"]) if target else None,
            "mfe": max([Decimal(0)] + [v for _, v in values]) if values else None,
            "mae": min([Decimal(0)] + [v for _, v in values]) if values else None,
            "early_exit_observation_id": target["observation_id"] if target else None,
            "early_exit_timestamp": target["observed_at"] if target else None,
            "early_exit_proceeds": target["sig_sell"] if target else None,
            "early_exit_pnl": number(target["sig_sell"]) - entry if target else None}


def horizon_result(shadow, row, minutes, history):
    entry = number(shadow["entry_price"])
    sell, reference = number(row["sig_sell"]), number(row["sig_reference"])
    fair = number(row["external_fair"]) if external_valid(row) else None
    gap = number(shadow["initial_external_gap"])
    frozen_gap = number(shadow["external_fair_at_entry"]) - reference if reference is not None else None
    current_gap = fair - reference if fair is not None and reference is not None else None
    convergence = gap - frozen_gap if frozen_gap is not None else None
    current_convergence = gap - current_gap if current_gap is not None else None
    buy = number(row["sig_buy"])
    result = {"horizon_minutes": minutes, "status": "EVALUATED", "observation_id": row["observation_id"],
              "observed_at": row["observed_at"], "delay_seconds": number(row["observed_epoch"] - shadow["entry_epoch"] - minutes * 60),
              "sell_price": sell, "sell_depth": row["sig_sell_depth"], "reference_price": reference, "external_fair": fair,
              "external_edge_remaining": fair - buy if fair is not None and buy is not None else None,
              "executable_pnl": sell - entry, "executable_return": (sell - entry) / entry,
              "mark_pnl": reference - entry if reference is not None else None,
              "mark_return": (reference - entry) / entry if reference is not None else None,
              "convergence": convergence, "convergence_percentage": convergence / gap if convergence is not None and gap else None,
              "contemporaneous_convergence": current_convergence,
              "contemporaneous_convergence_percentage": current_convergence / gap if current_convergence is not None and gap else None,
              "frozen_fair_remaining_gap": frozen_gap, "contemporaneous_remaining_gap": current_gap,
              "divergence_widened": frozen_gap > gap if frozen_gap is not None else None,
              "contemporaneous_divergence_widened": current_gap > gap if current_gap is not None else None,
              "executable_profitable": sell > entry}
    # Excursions and first-profit times for a 5m report cannot use later 60m data.
    result.update(trajectory(shadow, history, row["observed_epoch"]))
    return result


def evaluate(db, now=None):
    now = time.time() if now is None else now
    for saved, in db.execute("SELECT record_json FROM shadow_trades ORDER BY entry_epoch").fetchall():
        shadow = json.loads(saved)
        end = shadow["entry_epoch"] + max(HORIZONS) * 60 + WINDOW_SECONDS
        rows = [json.loads(r[0]) for r in db.execute("SELECT record_json FROM observations WHERE mapping_key=? AND side=? AND observed_epoch BETWEEN ? AND ? ORDER BY observed_epoch,observation_id",
                (shadow["mapping_key"], shadow["side"], shadow["entry_epoch"], min(end, now)))]
        for minutes in HORIZONS:
            target = shadow["entry_epoch"] + minutes * 60
            match = next((r for r in rows if target <= r["observed_epoch"] <= target + WINDOW_SECONDS and sig_valid(r)), None)
            if match:
                result = horizon_result(shadow, match, minutes, rows)
            else:
                result = {"horizon_minutes": minutes, "status": "MISSING_HORIZON_DATA" if now > target + WINDOW_SECONDS else "PENDING",
                          "observation_id": None}
            db.execute("INSERT INTO shadow_horizons VALUES (?,?,?,?,?) ON CONFLICT(shadow_id,horizon_minutes) DO UPDATE SET status=excluded.status,observation_id=excluded.observation_id,record_json=excluded.record_json",
                       (shadow["shadow_id"], minutes, result["status"], result["observation_id"], encode(result)))
        shadow.update(trajectory(shadow, rows, min(end, now)))
        shadow["status"] = "CLOSED_SHADOW_TARGET" if shadow["early_exit_observation_id"] else "EXPIRED_SHADOW_NO_TARGET" if now > end else "OPEN_SHADOW"
        db.execute("UPDATE shadow_trades SET record_json=? WHERE shadow_id=?", (encode(shadow), shadow["shadow_id"]))


def collect(rows=(), path=DATABASE, now=None, minimum_edge=MIN_EXTERNAL_EDGE):
    """One atomic research transaction; all network reads happened before this call."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=5) as db:
        db.execute("PRAGMA foreign_keys=ON")
        initialize(db)
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM research_metadata WHERE name='legacy_backfilled'").fetchone():
            legacy = [json.loads(r[0]) for r in db.execute("SELECT record_json FROM comparisons ORDER BY id")]
            insert_observations(db, legacy, False, minimum_edge)
            db.execute("INSERT INTO research_metadata VALUES ('legacy_backfilled','1')")
        inserted, created = insert_observations(db, rows, True, minimum_edge)
        evaluate(db, now)
        return {"new_observations": inserted, "new_shadows": created,
                "observations": db.execute("SELECT count(*) FROM observations").fetchone()[0],
                "shadow_trades": db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE, help="Research SQLite database only")
    args = parser.parse_args()
    try:
        print(encode(collect(path=args.database)))
        return 0
    except (OSError, sqlite3.Error, ValueError):
        print("SHADOW_RESEARCH_UNAVAILABLE | research database could not be validated")
        return 1
    except KeyboardInterrupt:
        print("Shadow evaluation stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
