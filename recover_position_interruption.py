"""GET-only audited recovery of the exact revision-290 read-only interruption.

Dry-run is the default. --apply only clears this reviewed halt and archives GET
evidence. It cannot alter positions/capital, submit/cancel orders or start a bot.
"""
import sys

import recover_position_freshness as verified_hold

HALT_REASON = "Position execution interrupted; never resume a sale automatically"
KIND = "READ_ONLY_POSITION_INTERRUPTION"
AUDIT_KEY = "position_interruption_recoveries"
SAVE_FLAG = "interruption_recovery"
TARGET_REVISION = 290
# Binds all three OPEN positions, entry histories, 3.02 allocation charges and
# the unchanged 0.125 quarantine reserve. Any changed checkpoint fails closed.
TARGET_CHECKPOINT_HASH = "c4c708a4e0b8313fcdef82612b194ff1baa08b42083b9cf2a7827feceb3933d6"


def eligible_checkpoint(checkpoint):
    return verified_hold.eligible_checkpoint(checkpoint, rules=sys.modules[__name__])


def proposed_checkpoint(checkpoint, record):
    return verified_hold.proposed_checkpoint(checkpoint, record, rules=sys.modules[__name__])


def validate_history(checkpoint):
    return verified_hold.validate_history(checkpoint, rules=sys.modules[__name__])


def validate_transition(previous, proposed):
    return verified_hold.validate_transition(previous, proposed, rules=sys.modules[__name__])


def recover(session, apply=False):
    # Reuse the existing six-order, holdings/cash/history and authorization
    # proofs under the same exclusive lock and atomic local state writer.
    return verified_hold.recover(session, apply=apply, rules=sys.modules[__name__])


def main(argv=None):
    return verified_hold.main(argv, rules=sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
