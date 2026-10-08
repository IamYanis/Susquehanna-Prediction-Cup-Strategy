"""Pilot policy only. The existing paper scanner keeps its own unchanged limits."""

# Naming a live mode is not permission to place an order. No pilot submission
# implementation is connected, and the default scanner remains paper-only.
PAPER = "PAPER"
LIVE_PILOT_DISABLED = "LIVE_PILOT_DISABLED"
LIVE_PILOT = "LIVE_PILOT"
DEFAULT_MODE = PAPER
LIVE_PILOT_SUBMISSION_ENABLED = False

LIVE_ALLOCATION = 5000
MAX_LIVE_CAPITAL_PER_TRADE = 50
MAX_LIVE_CAPITAL_PER_RACE = 100
MAX_TOTAL_LIVE_EXPOSURE = 500
MAX_LIVE_QUANTITY_PER_LEG = 1
