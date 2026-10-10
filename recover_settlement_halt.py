"""Explicit recovery of the diagnosed policy-rendering halt; GET-only by default.

This supports the pre-entry, zero-debit false positive only. It cannot clear an
execution halt, adopt fills, retry orders, or adjust cash/exposure reservations.
--apply repeats fresh verification and performs one audited local atomic write.
"""
import argparse
import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import live_pilot as pilot
import live_settlement as live
import manual_autonomous_settlement as manual
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from recover_rejected_attempt import read_orders
from senate_readiness_watcher import GetOnly, readonly_pilot_lock

HALT_REASON = live.NEEDS_REVALIDATION + " | Manual autonomous IDs, mapping, rules, sources or policy content changed"
EXPECTED_PAIRS = (("377", "378"), ("381", "382"), ("256", "257"))
POLICY_HASH = "9d98f07bea2cc4a70275f963716ee9c8a3d843a0b89c6f17ac8c4513970ca76c"


class SettlementGets(GetOnly):
    """Existing API GET restriction plus the one pinned official policy URL."""
    def get(self, url, **kwargs):
        if url == manual.POLICY_URL:
            pilot.require(kwargs.get("allow_redirects", False) is False, "Policy redirects are not allowed")
            return self._session.get(url, **dict(kwargs, allow_redirects=False))
        return super().get(url, **kwargs)


def eligible_checkpoint(checkpoint):
    pilot.require(checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"]
                  and checkpoint["review_reason"] == HALT_REASON, "Not the diagnosed pre-entry policy-rendering halt")
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(journal.get("active_attempt") is None
                  and all(a["state"] == pilot.REJECTED_RETIRED for a in journal.get("attempts", {}).values())
                  and not checkpoint.get("autonomous_positions")
                  and all(e["execution_status"] == pilot.REJECTED_RETIRED for e in checkpoint["live_exposures"].values())
                  and pilot.amount(checkpoint["confirmed_cumulative_debits"]) == 0
                  and pilot.amount(checkpoint["reserved_unconfirmed_capital"]) == 0,
                  "Unfinished execution, position, debit or reservation prevents settlement recovery")
    pilot.require("baseline_snapshot" in checkpoint, "Saved account baseline is required")


def prove_clear(checkpoint, snapshot, orders):
    """Require a complete unchanged account, never repair or reconstruct it."""
    baseline = checkpoint["baseline_snapshot"]
    account = snapshot["account"]
    pilot.require(snapshot["data_complete"] is True and all(snapshot["coverage"][k]["complete"] is True
                  for k in ("fills", "transactions")), "Recovery account history is incomplete")
    duration = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
    pilot.require(0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE, "Recovery snapshot capture was stale")
    pilot.require(account["tournament"]["id"] == checkpoint["tournament_id"]
                  and pilot.amount(account["tournament"]["myBalance"]) == pilot.amount(checkpoint["last_reconciled_account_cash"])
                  and not account["positions"] and not account["orders"]
                  and probe.inventory(account, "") == probe.inventory(baseline["account"], "")
                  and snapshot["recent_fills"] == baseline["recent_fills"]
                  and snapshot["recent_transactions"] == baseline["recent_transactions"],
                  "Account balance, holdings, orders, fills or transactions changed; retain halt")
    pilot.require(pilot.amount(account["quarantine_reserve"]) == pilot.amount(checkpoint["quarantine_reserve"]),
                  "Quarantine reserve changed; retain halt")
    since = probe.single.parse_api_timestamp(checkpoint["created_at"])
    pilot.require(probe.single.parse_api_timestamp(snapshot["history_since"]) <= since
                  and not any(probe.single.parse_api_timestamp(o["createdAt"]) >= since for o in orders),
                  "Order history or reconciliation coverage cannot establish a pre-entry-only halt")


def validate_history(records):
    """Archived approvals/evidence stay readable without consulting today's API."""
    pilot.require(isinstance(records, list) and records, "Missing settlement recovery audit")
    for record in records:
        pilot.require(set(record) == {"version", "kind", "recovered_at", "prior_checkpoint_hash", "prior_halt_reason",
                      "prior_revision", "prior_updated_at", "approvals", "evidence", "snapshot", "orders", "reads"}
                      and type(record["version"]) is int and record["version"] == 1
                      and record["kind"] == "POLICY_RENDERING_FALSE_POSITIVE"
                      and record["prior_halt_reason"] == HALT_REASON
                      and type(record["prior_revision"]) is int and record["prior_revision"] >= 1
                      and re.fullmatch(r"[0-9a-f]{64}", record["prior_checkpoint_hash"]), "Invalid settlement recovery audit")
        pilot.require(probe.single.parse_api_timestamp(record["recovered_at"]) >=
                      probe.single.parse_api_timestamp(record["prior_updated_at"]), "Recovery predates its halt")
        pilot.require([tuple(a["market_ids"]) for a in record["approvals"]] == list(EXPECTED_PAIRS)
                      and len(record["evidence"]) == len(EXPECTED_PAIRS), "Recovery must verify all three migrated pairs")
        for approval, evidence in zip(record["approvals"], record["evidence"]):
            live.validate_authorization(approval)
            manual.require_matching_evidence(approval, evidence)
            pilot.require(approval["policy_content_sha256"] == POLICY_HASH
                          and manual.evidence_hash(evidence, approval) == approval["evidence_hash"],
                          "Recovery evidence differs from the reviewed unchanged policy")
        pilot.require(record["reads"] and all(r.get("http_status") == 200 and r.get("credential_redacted") is False
                      for r in record["reads"]), "Recovery GET evidence missing or invalid")


def proposed_checkpoint(checkpoint, record):
    proposed = copy.deepcopy(checkpoint)
    proposed.setdefault("settlement_halt_recoveries", []).append(copy.deepcopy(record))
    proposed.update(state=pilot.READY, manual_review_required=False, review_reason="")
    return proposed


def validate_transition(previous, proposed):
    eligible_checkpoint(previous)
    record = proposed["settlement_halt_recoveries"][-1]
    validate_history([record])
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["prior_halt_reason"] == previous["review_reason"]
                  and record["prior_revision"] == previous["revision"]
                  and record["prior_updated_at"] == previous["updated_at"], "Recovery does not bind the current halt")
    prove_clear(previous, record["snapshot"], record["orders"])
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])
    pilot.require(proposed == proposed_checkpoint(previous, record), "Recovery may only clear this halt and append its audit")


def recover(session, apply=False):
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        eligible_checkpoint(checkpoint)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)  # In memory until an explicit --apply.
        get_only = SettlementGets(reads)
        approvals = live.load_manual_autonomous_authorizations()
        pilot.require([tuple(a["market_ids"]) for a in approvals] == list(EXPECTED_PAIRS), "Allowlist differs from reviewed migration")
        evidence = [pilot.read_live_pair(get_only, a)[2]["manual_evidence"] for a in approvals]
        started = time.monotonic()
        orders = read_orders(get_only, checkpoint["tournament_id"])
        snapshot = pilot_account.read_snapshot(get_only, approvals[0]["tournament_slug"], checkpoint, started=started, order_ids=[])
        pilot.require(read_orders(get_only, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        prove_clear(checkpoint, snapshot, orders)
        record = {"version": 1, "kind": "POLICY_RENDERING_FALSE_POSITIVE", "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "prior_halt_reason": checkpoint["review_reason"],
                  "prior_revision": checkpoint["revision"], "prior_updated_at": checkpoint["updated_at"],
                  "approvals": approvals, "evidence": evidence, "snapshot": snapshot, "orders": orders, "reads": reads.reads}
        proposed = proposed_checkpoint(checkpoint, record)
        pilot.validate_checkpoint(proposed)
        validate_transition(checkpoint, proposed)
        for approval in approvals:
            live.revalidate_authorization(approval)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        pilot.require(pilot.amount(pilot.quarantine.reserved_cost(checkpoint["tournament_id"])) ==
                      pilot.amount(checkpoint["quarantine_reserve"]), "Quarantine changed during recovery")
        pilot_account.check_fresh(started)
        if apply:
            pilot.require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
            pilot._state_lock_owners[path] = (os.getpid(), threading.get_ident())
            try:
                proposed = pilot._save_checkpoint_locked(proposed, path, settlement_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "confirmed_cumulative_debits", "reserved_unconfirmed_capital", "quarantine_reserve",
                  "calculated_remaining_allocation")
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "before": {k: checkpoint[k] for k in fields},
                "after": {k: proposed[k] for k in fields}, "verified_pairs": [a["market_ids"] for a in approvals],
                "get_requests": len(reads.reads), "orders_submitted": 0, "state_written": apply}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Explicit audited atomic local recovery; does not start the bot")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("RECOVERY_BLOCKED | Local SIG_API_KEY is unavailable")
        return 1
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": "Bearer " + key})
            print(json.dumps(recover(session, apply=args.apply), indent=2))
        return 0
    except (OSError, *scanner.API_ERRORS) as error:
        reason = str(error) if isinstance(error, (pilot.PilotBlocked, live.LiveSettlementBlocked,
                                                 pilot_account.AccountReadinessBlocked)) else "GET reconciliation unavailable; halt retained"
        print("RECOVERY_BLOCKED | " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
