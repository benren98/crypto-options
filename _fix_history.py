"""
Remove the 4 duplicate roll entries for BTC-26JUN26-58000-P (indices 42-45)
that were added with exit_reason=None and inflated PnL due to a bug in the
roll close code (used global CONTRACTS=1 instead of pos.get("contracts")).

The position remains open so expire_positions can close it correctly today.
"""
import json
from pathlib import Path

path = Path("positions.json")
state = json.load(path.open())

before = len(state.get("history", []))

# Remove entries with exit_reason=None (the buggy roll entries)
state["history"] = [
    h for h in state.get("history", [])
    if h.get("exit_reason") is not None
]

after = len(state["history"])
removed = before - after
print(f"Removed {removed} entries with exit_reason=None (were: {before}, now: {after})")

if removed > 0:
    path.write_text(json.dumps(state, indent=2, default=str))
    print("positions.json updated")
else:
    print("Nothing to remove")
