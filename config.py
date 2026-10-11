"""Pilot policy only. The existing paper scanner keeps its own unchanged limits."""
from decimal import Decimal

# Naming a live mode is not permission to place an order. The separate
# autonomous coordinator remains disabled; the default scanner is paper-only.
PAPER = "PAPER"
LIVE_PILOT_DISABLED = "LIVE_PILOT_DISABLED"
LIVE_PILOT = "LIVE_PILOT"
DEFAULT_MODE = PAPER
LIVE_PILOT_SUBMISSION_ENABLED = False
# This is the single switch for autonomous v0.1. Leave it off during development.
# The older diagnostic/probe switch above does not enable this coordinator.
AUTONOMOUS_LIVE_PILOT_ENABLED = True

LIVE_ALLOCATION = 5000
MAX_LIVE_CAPITAL_PER_TRADE = 50
MAX_LIVE_CAPITAL_PER_RACE = 100
MAX_TOTAL_LIVE_EXPOSURE = 500
MAX_LIVE_QUANTITY_PER_LEG = 1
# This remains the size of EACH BUY execution, not the aggregate position cap.
# v0.2 adds one matched unit at a time, using new independently reconciled keys.
MAX_MATCHED_PAIRS_PER_RACE = 5
MIN_ADDON_EDGE = Decimal("0.010")
MIN_ADDON_COMPLETION_EDGE = Decimal("0.005")
