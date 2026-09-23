# CLAUDE.md — VRP Options Bot

Guidance opérationnelle pour Claude Code. La **stratégie** est documentée dans `README.md`
(dont le log « Approaches Tested and Rejected ») ; ce fichier couvre le **comment opérer**.

## Ce que c'est
Stratégie VRP : vente de puts BTC OTM delta-hedgés via BTC-PERPETUAL (Deribit). Suivi d'état
(pas de passage d'ordre réel). Tourne **~toutes les heures via GitHub Actions** : expirations →
rolls → circuit breaker → scan/entrées → hedge → régénère les dashboards (GitHub Pages). État
dans `positions.json` + un Gist GitHub.
- Cron : `17,47 * * * *` + garde « dernier run < 40 min → skip » (job `gate`). L'ancien
  `0 * * * *` ne tournait qu'environ toutes les 3 h (minute :00 saturée chez GitHub, jusqu'à 12 h
  d'écart). Vérifier la cadence réelle : `gh run list --workflow pnl_monitor.yml`.

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
- Finir les commits par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Jamais** committer `.env` / tokens. Les secrets sont dans les Actions secrets (`GIST_TOKEN`,
  `GIST_ID`).

## Lancer les choses
- Backtest BTC (config prod par défaut : circuit breaker ON, pas de « toujours ≥1 position ») :
  `python backtest.py` — miroir des params live de `greeks_hedge.py`, 4 ans réels (DVOL paginé),
  frais Deribit, funding réel. Chaque **run horaire** est rejoué comme le live (prix de
  `funding_history.jsonl`) : rolls → circuit breaker → entrées (2 passes si book vide, calendrier
  d'échéances Deribit, grille de strikes, DVOL de la veille) → hedge. Marks et DVOL restent
  journaliers ; `RUN_EVERY_H` (hypothèse) simule une cadence live dégradée. ~20 s par run.
- Routine de sweeps (anti-overfit, hebdo en Actions, ~80 min) : `python backtest_routine.py`
  → `backtest_routine.json` + page backtests. La config prod est lue dans `backtest.py` (pas de
  copie). Changer seulement la règle de verdict : `python backtest_routine.py --rejudge`.
- Scripts d'exploration ponctuels : `research/` (lancer depuis la racine). Aides manuelles : `tools/`.
- Dashboards : `python generate_html.py` (live v1 → `docs/index.html`) ·
  `python generate_dashboard.py` (v2 orientée décision → `docs/v2.html`) ·
  `python generate_backtest_html.py` (backtests → `docs/backtest.html`).
- Propositions de design (en cours de choix) : `designs/{cockpit,editorial,bento}/template.html`,
  rendus à chaque run par `generate_dashboard.py --template … --out docs/design-*.html` ;
  `python inject_switcher.py` ajoute ensuite à toutes les pages de `docs/` un sélecteur flottant
  (v1 · v2 · Cockpit · Éditorial · Bento · Backtests). Une fois le design choisi : en faire la page
  principale et retirer les autres (workflow + `PAGES` de `inject_switcher.py`).
- Dashboards v2 et backtests : le modèle de données est calculé en Python, le rendu est dans un
  template (`dashboard_v2.html`, `backtest_page.html`) + CSS/JS communs `dashboard_assets/`
  (ne jamais éditer `docs/*.html`, ils sont régénérés). Tester un autre
  état : `python generate_dashboard.py --data-dir <dossier> --now "2026-08-29 23:50:00"` (ex. fichiers
  extraits d'un ancien commit avec `git show <sha>:positions.json`).
- Collecte surfaces de vol : `vol_surface_logger.py` (horaire) → `vol_surface.jsonl` ;
  fit `fit_vol_model.py` → `vol_model_fit.json` (≥15 jours).
- Vérifier l'impact LIVE d'un changement de scoring : `greeks_hedge.fetch_scored_candidates(...)`
  sur les vraies IV Deribit (pas seulement le backtest).

## Où vivent les paramètres
- **Live** : constantes en tête de `greeks_hedge.py` (`SCORE_W_*`, `SKEW_NORM`, `IVHV_NORM`,
  `YIELD_NORM`, `ENTRY_SCORE_MIN`, `MIN_PREMIUM_USD`, `CB_*`, `SIZE_CONVEXITY`, `GRADUATED_CB`,
  `ALWAYS_IN_POSITION`…).
- `backtest.py` **miroite** ces constantes — les garder synchronisées à chaque changement live.
  `python check_params_sync.py` vérifie 46 paramètres (score, HV, entrée, fenêtre d'échéances,
  entrées/jour, rolls, sizing, CB, politique de hedge `HEDGE_*`, frais `FEE_*`) ; bloquant dans la
  routine hebdo. Un paramètre sweepé sans équivalent live est typé `candidate` (jamais recommandé).
- Frais Deribit (`FEE_*`, grille Standard vérifiée le 2026-09-23 sur support.deribit.com) : le bot
  est en paper, ils ne sont pas débités mais le backtest les applique et le dashboard v2 affiche
  le PnL net de frais estimés. Revérifier la grille de temps en temps.
- Capital / marge : `margin.py` (marge standard Deribit exacte, portfolio margin ESTIMÉE — forme du
  choc de vol non publiée ; `TBILL_YIELD` = hypothèse de rémunération du collatéral). Utilisé par
  le backtest (capital jour par jour, rendement sur capital) et le dashboard v2.
- Appels API : le scan lit toute la chaîne via `fetch_option_chain` (book summary, greeks et IV
  au bid recalculés en Black-76) — ne pas réintroduire d'appel `ticker` par option dans une boucle.

## Philosophie de calibration (important)
- Le backtest price les options avec un modèle (niveau ATM/DVOL + skew quadratique par maturité) ;
  les **vraies surfaces** sont collectées (`vol_surface.jsonl`) et utilisées telles quelles sur les
  dates couvertes. Écart moyen modèle − marché ≈ +0,1 pt de vol depuis l'ajout du niveau ATM
  (+4 pts avant : le DVOL servait d'ATM pour toutes les échéances). Le fit reste statique (DVOL
  < 15 pts d'amplitude observée) → comportement en stress encore extrapolé.
- Le hedge du backtest est rejoué **heure par heure** comme le live : l'ancien contrôle à la clôture
  sous-estimait son coût d'un facteur ~7.
- **Ne changer un param de scoring/sizing que si la routine le flagge `✅ robuste`** (gagne ≥3/5
  folds, gain ≥1.0 vs actuel), **pas** sur un Calmar fité plus haut seul.
- Normalisations du score (skew/IV-HV/yield), seuil d'entrée et sizing sont **couplés par l'échelle
  du score** : changer une norme rescale les scores → re-vérifier le seuil (`ENTRY_SCORE_MIN`) et le
  sizing. Un changement de norme sans ajuster le seuil peut figer les entrées (vérifié plusieurs fois).
- Le live voit le **vrai skew Deribit** ; le backtest utilise le modèle → ils divergent (surtout en
  régime calme, où les scores live sont bas). Valider tout changement impactant le live sur le scan réel.
