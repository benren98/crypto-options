"""Backtest final : stratégie complète avec circuit breaker calibré (10% / +12pts)."""
import backtest
from backtest import run

backtest.CB_MOVE_3D_PCT = 10.0
backtest.CB_DVOL_3D_PTS = 12.0

print("\n################ SANS CIRCUIT BREAKER ################")
run(4.0, always_one=True, circuit_breaker=False)

print("\n################ AVEC CIRCUIT BREAKER (10% / +12pts) ################")
run(4.0, always_one=True, circuit_breaker=True)
