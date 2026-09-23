# research/ — scripts d'exploration

Scripts ponctuels ayant servi à calibrer la stratégie (sweeps historiques, tests de circuit
breaker, variantes rejetées, analyses de plage des composantes du score…). Ils ne tournent
dans aucun workflow ; leurs conclusions sont consignées dans le README principal
(« Approaches Tested and Rejected »).

**À lancer depuis la racine du projet** (ils lisent `vol_model_fit.json`, `funding_history.jsonl`…
par chemin relatif et importent `backtest`, `greeks_hedge`) :

```bash
python research/backtest_putspread.py
```

La routine automatique qui remplace la plupart de ces sweeps est `backtest_routine.py`.
