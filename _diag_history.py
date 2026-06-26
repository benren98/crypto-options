import json
from pathlib import Path

with open('positions.json') as f:
    state = json.load(f)

hist = state.get('history', [])
print(f'History entries: {len(hist)}')
for i, h in enumerate(hist):
    name = h.get('instrument_name', '?')
    er   = h.get('exit_reason', 'MISSING')
    ts   = str(h.get('exit_ts', '?'))[:16]
    pnl  = h.get('pnl_usd', '?')
    ctr  = h.get('contracts', '?')
    print(f'  [{i}] {name}  ctr={ctr}  exit_reason={er}  ts={ts}  pnl={pnl}')

print()
print('Open positions:')
for p in state.get('positions', []):
    print(f"  {p['instrument_name']}  contracts={p.get('contracts')}")
