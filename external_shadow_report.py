"""Read-only report of stored shadow returns and simple external-to-SIG lead/lag."""
import argparse
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

from external_shadow_tracker import DATABASE, HORIZONS, WINDOW_SECONDS, external_valid, number, sig_valid

BUCKETS = ("3% to <5%", "5% to <8%", ">=8%")
LAGS = (1, 5, 15, 30)


def bucket(edge):
    edge = number(edge)
    return BUCKETS[0] if edge < Decimal(".05") else BUCKETS[1] if edge < Decimal(".08") else BUCKETS[2]


def mean(values):
    values = [number(v) for v in values if v is not None]
    return sum(values, Decimal(0)) / len(values) if values else None


def middle(values):
    values = [number(v) for v in values if v is not None]
    return median(values) if values else None


def aggregate(shadows, horizons, dimension):
    grouped = defaultdict(list)
    by_id = {s["shadow_id"]: s for s in shadows}
    if dimension == "bucket":
        for label in BUCKETS:
            for minutes in HORIZONS:
                grouped[(label, minutes)] = []
    for result in horizons:
        shadow = by_id[result["shadow_id"]]
        label = (bucket(shadow["entry_edge"]) if dimension == "bucket" else shadow[dimension]
                 if dimension != "mapping" else f"SIG {shadow['sig_market_id']}/{shadow['sig_exchange_id']} -> Poly {shadow['polymarket_market_id']} [{shadow['mapping_key'][:10]}]")
        grouped[(label, result["horizon_minutes"])].append(result)
    output = []
    for (label, minutes), results in sorted(grouped.items()):
        evaluated = [r for r in results if r["status"] == "EVALUATED"]
        wins = sum(number(r["executable_pnl"]) > 0 for r in evaluated)
        adverse = [number(r["mae"]) for r in evaluated if r.get("mae") is not None]
        output.append({"group": label, "horizon_minutes": minutes, "sample_count": len(results),
                       "evaluated_count": len(evaluated), "profitable_count": wins,
                       "win_rate": Decimal(wins) / len(evaluated) if evaluated else None,
                       "average_executable_return": mean([r["executable_return"] for r in evaluated]),
                       "median_executable_return": middle([r["executable_return"] for r in evaluated]),
                       "average_executable_pnl": mean([r["executable_pnl"] for r in evaluated]),
                       "average_mark_return": mean([r["mark_return"] for r in evaluated]),
                       "median_convergence_percentage": middle([r["convergence_percentage"] for r in evaluated]),
                       "average_first_profit_seconds": mean([r.get("first_profit_seconds") for r in evaluated]),
                       "maximum_adverse_excursion": min(adverse) if adverse else None,
                       "missing_data_count": sum(r["status"] == "MISSING_HORIZON_DATA" for r in results),
                       "pending_count": sum(r["status"] == "PENDING" for r in results)})
    return output


def correlation(pairs):
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    xmean, ymean = mean(xs), mean(ys)
    xx = sum(((x - xmean) ** 2 for x in xs), Decimal(0))
    yy = sum(((y - ymean) ** 2 for y in ys), Decimal(0))
    if not xx or not yy:
        return None
    return sum(((x - xmean) * (y - ymean) for x, y in pairs), Decimal(0)) / (xx * yy).sqrt()


def lead_lag(observations):
    """No significance claim: external move, then subsequent SIG midpoint movement.

    Use YES once per mapping (NO complements would duplicate the same sample).
    External deltas need adjacent fresh observations no more than two minutes
    apart. Sparse, multi-hour gaps must not masquerade as one-minute signals.
    """
    grouped = defaultdict(list)
    for row in observations:
        if row["side"] == "YES":
            grouped[row["mapping_key"]].append(row)
    output = []
    for key, history in sorted(grouped.items()):
        if not any(r.get("mapping_confidence") == "EXACT_RESEARCH_ONLY" for r in history):
            continue
        history.sort(key=lambda r: r["observed_epoch"])
        valid = [r for r in history if sig_valid(r, executable=False) and external_valid(r)]
        events = []
        for previous, current in zip(valid, valid[1:]):
            if not 0 < current["observed_epoch"] - previous["observed_epoch"] <= WINDOW_SECONDS:
                continue
            move = number(current["external_fair"]) - number(previous["external_fair"])
            if move:
                events.append((current, move))
        for lag in LAGS:
            pairs = []
            for event, external_move in events:
                target = event["observed_epoch"] + lag * 60
                future = next((r for r in history if target <= r["observed_epoch"] <= target + WINDOW_SECONDS
                               and sig_valid(r, executable=False)), None)
                if future:
                    pairs.append((external_move, number(future["sig_reference"]) - number(event["sig_reference"])))
            output.append({"mapping_key": key, "race": history[0]["race"], "sig_market_id": history[0]["sig_market_id"],
                           "lag_minutes": lag, "external_move_events": len(events), "sample_count": len(pairs),
                           "missing_data_count": len(events) - len(pairs), "correlation": correlation(pairs),
                           "direction_hit_rate": Decimal(sum(x * y > 0 for x, y in pairs)) / len(pairs) if pairs else None,
                           "average_sig_move": mean([y for _, y in pairs])})
    return output


def load_report(path=DATABASE):
    # Opening with mode=ro cannot create a database or run a schema migration.
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        # All tables must come from the same committed collector cycle, even
        # when the optional loop commits while a report is being read.
        db.execute("BEGIN")
        observations = [json.loads(r[0]) for r in db.execute("SELECT record_json FROM observations ORDER BY observed_epoch")]
        shadows = [json.loads(r[0]) for r in db.execute("SELECT record_json FROM shadow_trades ORDER BY entry_epoch")]
        horizons = []
        for shadow_id, payload in db.execute("SELECT shadow_id,record_json FROM shadow_horizons ORDER BY horizon_minutes"):
            horizons.append(dict(json.loads(payload), shadow_id=shadow_id))
        coverage = {minutes: {status: sum(r["horizon_minutes"] == minutes and r["status"] == status for r in horizons)
                             for status in ("EVALUATED", "PENDING", "MISSING_HORIZON_DATA")} for minutes in HORIZONS}
        return {"observation_count": len(observations), "legacy_comparison_count": db.execute("SELECT count(*) FROM comparisons").fetchone()[0],
                "first_observed_at": observations[0]["observed_at"] if observations else None,
                "last_observed_at": observations[-1]["observed_at"] if observations else None,
                "shadow_count": len(shadows), "open_shadows": [s for s in shadows if s["status"] == "OPEN_SHADOW"],
                "closed_target_count": sum(s["status"] == "CLOSED_SHADOW_TARGET" for s in shadows),
                "expired_count": sum(s["status"] == "EXPIRED_SHADOW_NO_TARGET" for s in shadows),
                "coverage": coverage,
                "groups": {dimension: aggregate(shadows, horizons, dimension) for dimension in ("bucket", "race", "side", "mapping")},
                "lead_lag": lead_lag(observations)}


def display(value, percent=False):
    return "—" if value is None else f"{number(value) * (100 if percent else 1):.4f}" + ("%" if percent else "")


def print_report(report):
    print("READ-ONLY shadow research | one hypothetical share | no account or orders")
    print(f"Observations {report['observation_count']}; legacy rows {report['legacy_comparison_count']}; "
          f"shadows {report['shadow_count']}; open {len(report['open_shadows'])}; target exits {report['closed_target_count']}; expired {report['expired_count']}")
    print(f"History: {report['first_observed_at']} -> {report['last_observed_at']}")
    for minutes, counts in report["coverage"].items():
        print(f"{minutes}m coverage: evaluated={counts['EVALUATED']}, pending={counts['PENDING']}, missing={counts['MISSING_HORIZON_DATA']}")
    for s in report["open_shadows"]:
        print(f"OPEN_SHADOW | {s['shadow_id']} | {s['race']} / {s['proposition']} {s['side']} | "
              f"SIG {s['sig_market_id']}/{s['sig_exchange_id']} | entry {s['entry_price']} | fair {s['external_fair_at_entry']} | "
              f"edge {s['entry_edge']} | depth {s['entry_executable_depth']} | {s['entry_timestamp']}")
    print("Returns below are P&L / executable entry cost (ROI); excursions are price points. — means no measurement.")
    for dimension, rows in report["groups"].items():
        print(f"\nBy {dimension}: group | horizon | samples/evaluated/wins | win rate | mean/median exec ROI | mean mark ROI | "
              "median gap closed | mean first-profit seconds | max adverse | missing/pending")
        for row in rows:
            print(f"{row['group']} | {row['horizon_minutes']}m | {row['sample_count']}/{row['evaluated_count']}/{row['profitable_count']} | "
                  f"{display(row['win_rate'], True)} | {display(row['average_executable_return'], True)}/{display(row['median_executable_return'], True)} | "
                  f"{display(row['average_mark_return'], True)} | {display(row['median_convergence_percentage'], True)} | "
                  f"{display(row['average_first_profit_seconds'])} | {display(row['maximum_adverse_excursion'])} | "
                  f"{row['missing_data_count']}/{row['pending_count']}")
    print("\nLead/lag: SIG market | lag | samples/events | correlation | direction hit rate | mean subsequent SIG move | missing")
    for row in report["lead_lag"]:
        print(f"{row['race']} {row['sig_market_id']} | {row['lag_minutes']}m | {row['sample_count']}/{row['external_move_events']} | "
              f"{display(row['correlation'])} | {display(row['direction_hit_rate'], True)} | {display(row['average_sig_move'])} | {row['missing_data_count']}")
    if not any(c["EVALUATED"] for c in report["coverage"].values()):
        print("No evaluable forward returns yet. Collect quotes around each horizon; a long timestamp span alone is insufficient.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = load_report(args.database)
        if args.json:
            print(json.dumps(report, default=str, indent=2))
        else:
            print_report(report)
        return 0
    except (OSError, sqlite3.Error, ValueError):
        print("SHADOW_REPORT_UNAVAILABLE | collect/evaluate research history first")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
