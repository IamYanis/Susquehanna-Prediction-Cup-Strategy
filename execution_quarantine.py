"""Preserve one unresolved market-387 attempt while reserving its full risk.

No requests, retries or inferred order outcomes live here. The separate record
pins the original journal bytes; missing/changed evidence stops new execution.
"""
import hashlib
import json
import os
from pathlib import Path

from price_reader import DataValidationError, numeric_id

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "execution_quarantine.json"
SOURCE_DIR = ROOT / "paired_account_test"
ACTIVE_DIR = ROOT / "active_paired_account_test"
JOURNALS = ("pair.json", "leg_1.json", "leg_2.json")


class QuarantineError(DataValidationError):
    """A fixed local safety reason, safe to display without credentials."""


def source_snapshot():
    # Import lazily: the execution modules themselves use this risk checker.
    import paired_account_test as paired

    pair, legs = paired.load_pair(SOURCE_DIR)
    first, second = legs
    if not (pair["state"] == "UNKNOWN" and first["state"] == "UNKNOWN"
            and first["order_id"] is None and first["observation"] is None
            and first["market_id"] == "387" and first["request"]["exchangeId"] == "1076"
            and first["request"]["action"] == "buy" and first["request"]["side"] == "no"
            and first["request"]["quantity"] == 1 and second["state"] == "PREPARED"
            and second["order_id"] is None):
        raise QuarantineError("Only the unchanged UNKNOWN market-387 attempt can be quarantined")
    hashes = {name: hashlib.sha256((SOURCE_DIR / name).read_bytes()).hexdigest() for name in JOURNALS}
    body = first["request"]
    exposure = {"market_id": first["market_id"], "exchange_id": body["exchangeId"],
                "tournament_id": body["tournamentId"], "side": body["side"],
                "max_quantity": body["quantity"], "reserved_cost": body["quantity"] * body["price"],
                "state": "UNKNOWN", "source_directory": str(SOURCE_DIR.resolve())}
    return hashes, exposure


def load_quarantine():
    """Reload and verify on every check; an empty API history cannot release it."""
    try:
        if not STATE_PATH.exists():
            if ACTIVE_DIR.exists():
                raise QuarantineError("Active pair exists without its quarantine record; new orders blocked")
            controller = SOURCE_DIR / "pair.json"
            if controller.exists() and json.loads(controller.read_text(encoding="utf-8"))["state"] == "UNKNOWN":
                raise QuarantineError("Original UNKNOWN has no verified quarantine record; new orders blocked")
            return None
        record = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or set(record) != {"version", "source_directory", "sha256"}
                or type(record["version"]) is not int or record["version"] != 1
                or record["source_directory"] != str(SOURCE_DIR.resolve())):
            raise QuarantineError("Quarantine record is invalid; new orders blocked")
        hashes, exposure = source_snapshot()
        if record["sha256"] != hashes:
            raise QuarantineError("Quarantined journals changed; new orders blocked")
        return exposure
    except QuarantineError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise QuarantineError("Cannot verify quarantined journals; new orders blocked") from error


def create_quarantine():
    """Explicit, local-only registration. Never move or rewrite the source files."""
    import paired_account_test as paired
    import account_test as single

    if STATE_PATH.exists() or ACTIVE_DIR.exists():
        raise QuarantineError("Quarantine or active pair already exists; preserve it")
    if single.STATE_PATH.exists():
        raise QuarantineError("Another single-order journal exists; resolve it before quarantine")
    with paired.operation_lock(SOURCE_DIR):
        hashes, _ = source_snapshot()
        record = {"version": 1, "source_directory": str(SOURCE_DIR.resolve()), "sha256": hashes}
        try:
            descriptor = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            single.sync_directory(STATE_PATH.parent)
        except OSError as error:
            # A partial record remains a blocker; never silently recreate it.
            raise QuarantineError("Cannot durably save quarantine; stop without orders") from error
    return load_quarantine()


def require_unblocked_markets(market_ids):
    exposure = load_quarantine()
    if exposure and exposure["market_id"] in {numeric_id(value) for value in market_ids}:
        raise QuarantineError("Market 387 is quarantined; no order or pair containing it is allowed")


def require_unblocked_exchanges(exchange_ids):
    exposure = load_quarantine()
    if exposure and exposure["exchange_id"] in {numeric_id(value) for value in exchange_ids}:
        raise QuarantineError("Exchange 1076 is quarantined; new exposure is blocked")


def require_writable_path(path):
    if load_quarantine() and (Path(path).resolve() == SOURCE_DIR.resolve()
                              or SOURCE_DIR.resolve() in Path(path).resolve().parents):
        raise QuarantineError("Original quarantined journals are frozen; no writes or retries allowed")


def reserved_cost(tournament_id):
    exposure = load_quarantine()
    return exposure["reserved_cost"] if exposure and exposure["tournament_id"] == tournament_id else 0


def active_pair_directory(default):
    exposure = load_quarantine()
    if not exposure:
        return Path(default)
    if Path(default).resolve() != SOURCE_DIR.resolve():
        raise QuarantineError("Quarantine source differs from the default pair journal; stop")
    return ACTIVE_DIR


def print_quarantine(tournament_id=None):
    exposure = load_quarantine()
    if exposure:
        print("QUARANTINE | market 387 / exchange 1076 | UNKNOWN | no retries or inferred outcome")
        print(f"Possible exposure: 0..{exposure['max_quantity']} NO share | "
              f"full cash/exposure reserve {exposure['reserved_cost']:.3f} SUSQies")
        if tournament_id is not None and tournament_id != exposure["tournament_id"]:
            print("Cash reserve applies to the quarantined attempt's competition; market remains blocked.")
        print("Source journals are frozen. Visible holdings/orders do not release this additional reserve.")
