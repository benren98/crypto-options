# CLAUDE.md — VRP Options Bot

Guidance opérationnelle pour Claude Code. La **stratégie** est documentée dans `README.md`
(dont le log « Approaches Tested and Rejected ») ; ce fichier couvre le **comment opérer**.

## Ce que c'est
Stratégie VRP : vente de puts BTC OTM delta-hedgés via BTC-PERPETUAL (Deribit). Suivi d'état
(pas de passage d'ordre réel). Tourne **toutes les heures via GitHub Actions** : scan → score →
sizing → hedge → régénère le dashboard (`docs/index.html`, GitHub Pages). État dans
`positions.json` + un Gist GitHub.

## Environnement (Windows)
- **Python** : utiliser `C:\Users\bacee\anaconda3\python.exe`. Le `python`/`python3` nu est le stub
  du Microsoft Store et échoue.
- **Toujours** `$env:PYTHONIOENCODING="utf-8"` avant de lancer un script : les sorties ont accents/
  émojis (cp1252 plante sinon).
- Shell = PowerShell.

## Git
- **Pull avant push.** Le bot Actions commite chaque heure (snapshots PnL + `docs/index.html`
  régénéré) → `git push` est souvent rejeté → `git pull --no-edit` → résoudre.
- **Conflit récurrent** : `docs/index.html` (toi et le bot le régénérez). Résolution : relancer
  `python generate_html.py`, puis `git add docs/index.html` et committer le merge.
- **Messages de commit** : PowerShell casse les `'` et `"` dans `git commit -m @'...'@` (ils
  partent en pathspecs). → **aucun apostrophe ni guillemet** dans les messages. Fermer `'@` en
  colonne 0 sur sa propre ligne ; ne pas enchaîner `git push` sur la même ligne.
- Finir les commits par `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Jamais** committer `.env` / tokens. Les secrets sont dans les Actions secrets (`GIST_TOKEN`,
  `GIST_ID`).

## Lancer les choses
- Backtest BTC (config prod) : `python backtest.py` — miroir des params live de `greeks_hedge.py`.
- Routine de sweeps (anti-overfit, hebdo en Actions) : `python backtest_routine.py`
  → `backtest_routine.json` + dashboard backtests.
- Dashboards : `python generate_html.py` (live → `docs/index.html`) ·
  `python generate_backtest_html.py` (backtests → `docs/backtest.html`).
- Collecte surfaces de vol : `vol_surface_logger.py` (horaire) → `vol_surface.jsonl` ;
  fit `fit_vol_model.py` → `vol_model_fit.json` (≥15 jours).
- Vérifier l'impact LIVE d'un changement de scoring : `greeks_hedge.fetch_scored_candidates(...)`
  sur les vraies IV Deribit (pas seulement le backtest).

## Où vivent les paramètres
- **Live** : constantes en tête de `greeks_hedge.py` (`SCORE_W_*`, `SKEW_NORM`, `IVHV_NORM`,
  `YIELD_NORM`, `ENTRY_SCORE_MIN`, `MIN_PREMIUM_USD`, `CB_*`, `SIZE_CONVEXITY`, `GRADUATED_CB`,
  `ALWAYS_IN_POSITION`…).
- `backtest.py` **miroite** ces constantes — les garder synchronisées à chaque changement live.

## Philosophie de calibration (important)
- Le backtest price les options avec un modèle de skew ; les **vraies surfaces** sont collectées
  (`vol_surface.jsonl`) pour supprimer le risque modèle. Le fit vient de peu de jours calmes →
  les **magnitudes du backtest fité sont optimistes** ; ne pas se fier au Calmar absolu pour l'instant.
- **Ne changer un param de scoring/sizing que si la routine le flagge `✅ robuste`** (gagne ≥3/5
  folds, gain ≥1.0 vs actuel), **pas** sur un Calmar fité plus haut seul.
- Normalisations du score (skew/IV-HV/yield), seuil d'entrée et sizing sont **couplés par l'échelle
  du score** : changer une norme rescale les scores → re-vérifier le seuil (`ENTRY_SCORE_MIN`) et le
  sizing. Un changement de norme sans ajuster le seuil peut figer les entrées (vérifié plusieurs fois).
- Le live voit le **vrai skew Deribit** ; le backtest utilise le modèle → ils divergent (surtout en
  régime calme, où les scores live sont bas). Valider tout changement impactant le live sur le scan réel.
