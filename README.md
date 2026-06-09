# VRP Monitor — Short Put BTC

Système de monitoring et de gestion d'un portefeuille de puts vendus sur BTC (Deribit), avec delta-hedge automatique via BTC-PERPETUAL. Tournant en GitHub Actions toutes les heures, avec un dashboard GitHub Pages mis à jour en temps réel.

---

## Architecture

```
greeks_hedge.py     — moteur principal : scan, entrées, roll, hedge
pnl_monitor.py      — calcul PnL et snapshot CSV par position
generate_html.py    — génération du dashboard HTML
positions.json      — état du portefeuille (source de vérité : GitHub Gist)
positions_detail.json — données live par position (pnl_monitor → generate_html)
scan_entry.json     — top 5 opportunités du dernier scan (greeks_hedge → generate_html)
```

Le pipeline GitHub Actions tourne dans cet ordre : fetch Gist → pnl_monitor → greeks_hedge → generate_html → commit → push Gist.

---

## Stratégie

Vente de puts OTM sur BTC à maturité courte (5–30 jours), delta-hedgés via BTC-PERPETUAL short. L'edge est le **Volatility Risk Premium (VRP)** : la volatilité implicite (IV) est structurellement supérieure à la volatilité réalisée (HV), ce qui rend la vente d'options statistiquement rentable à long terme.

**Risque principal** : un mouvement brutal à la baisse (crash, gap) où le gamma augmente rapidement et le delta s'emballe au-delà de la capacité de hedge.

---

## Sélection des options

### Univers de scan

| Paramètre | Valeur |
|---|---|
| Type | Puts OTM uniquement |
| TTE | 5 – 30 jours |
| Delta | −0.10 à −0.30 |
| Spread B/A max | 12% du mark |

### Score composite

Chaque option reçoit un score entre 0 et 1 calculé comme suit :

```
score = 0.40 × s_iv_hv + 0.30 × s_rank + 0.30 × s_yield
```

**Composante IV/HV** — capture la prime de risque de volatilité :
```
s_iv_hv = clamp(IV / HV_10j − 1.0, 0, 1)
```
Vaut 0 si IV = HV (pas de prime), 1 si IV = 2× HV. Poids 40%.

**Composante rang IV** — mesure le contexte marché sur 30 jours :
```
s_rank = (DVOL_actuel − DVOL_min30j) / (DVOL_max30j − DVOL_min30j)
```
Utilise le DVOL index (vol ATM marché), commun à tous les candidats. Vaut 0 si IV au plancher du mois, 1 si IV au plafond. Poids 30%.

Note : la vol implicite individuelle de chaque option (`mark_iv`) n'est pas utilisée ici car elle inclut le skew de volatilité (puts OTM ont toujours un `mark_iv` > DVOL), ce qui rendrait le rang 100% pour toutes les options.

**Composante yield** — prime annualisée normalisée à 20% BTC/an :
```
s_yield = min(1.0, (mark / TTE_années) / 0.20)
```
Vaut 1 si le yield annualisé atteint ou dépasse 20% BTC. Poids 30%.

### Seuils d'entrée opportuniste

Toutes les conditions suivantes doivent être réunies simultanément :

| Condition | Seuil |
|---|---|
| Score composite | ≥ 0.58 |
| Ratio IV/HV | ≥ 1.10 |
| IV absolue | ≥ 35% |
| Spread B/A | ≤ 12% du mark |

### Sizing

```
contracts = round(score, 1)  # ex. score 0.72 → 0.7 BTC
contracts = max(0.1, contracts)
contracts = min(contracts, MAX_PORTFOLIO_BTC − used_btc)
```

Plafond portefeuille : **3 BTC notionnel total**. Le sizing reflète la conviction : un score de 0.6 ouvre 0.6 BTC, un score de 1.0 ouvre 1.0 BTC (1 contrat Deribit = 1 BTC).

---

## Gestion du portefeuille

### Limites

| Paramètre | Valeur |
|---|---|
| Notionnel total max | 3 BTC |

### Garantie "toujours en position"

Si le portefeuille est vide (roll déclenché ou première ouverture), l'algo **ouvre obligatoirement** le meilleur candidat scoré, même si les conditions de signal ne sont pas réunies. En cas d'absence de candidat liquide (B/A > 12%), le filtre spread est levé pour garantir l'entrée.

### Entrées opportunistes

Quand le portefeuille est en dessous du max (< 3 positions), l'algo tente d'ouvrir une position supplémentaire à chaque run si le signal est actif et que le meilleur candidat non détenu atteint le score minimum.

---

## Logique de roll

### Fenêtre d'observation

Le roll entre dans sa fenêtre dès que **TTE ≤ 1 jour** (`ROLL_TRIGGER = 1.0`).

### Décision dans la fenêtre

Une fois dans la fenêtre, le roll n'est pas automatique : il dépend du **gamma en points de delta** :

```
gamma_pts = gamma × spot × 0.01 × 100
```

Ce chiffre représente combien de points de delta (%) la position perd si le spot bouge de 1%.

| Condition | Décision |
|---|---|
| TTE > 1j | HOLD — pas encore dans la fenêtre |
| TTE ≤ 1j ET gamma > 6 pts/1% | **ROLL** — put trop proche du strike, risque ATM |
| TTE ≤ 1j ET gamma ≤ 6 pts/1% | **HOLD** — put suffisamment OTM, on laisse expirer |

**Seuil gamma roll : 6 pts de delta / 1% move.**

Le raisonnement : si une option à 1 jour de maturité a encore un gamma élevé, c'est qu'elle est proche du strike (ATM ou légèrement OTM). Le risque d'un gap à la baisse dans les dernières heures est asymétrique. Si le gamma est faible, l'option est profondément OTM et va expirer sans valeur — on laisse tourner le theta.

### Expiration automatique

Les positions dont la date d'expiry est dépassée sont automatiquement déplacées de `positions[]` vers `history[]` au début de chaque `run_once()` via `expire_positions()`.

---

## Delta hedge

### Instrument

BTC-PERPETUAL short. Le hedge vise à maintenir le delta net du portefeuille (options + hedge) proche de zéro.

### Calcul du delta cible

```
target_hedge_qty = −(delta_net_options × contracts)
```

Le delta net des options est la somme des deltas de toutes les positions ouvertes. Comme les puts ont un delta négatif, le hedge est un short BTC-PERPETUAL.

### Seuil de rebalancement — adaptatif selon l'IV

Le seuil n'est pas fixe : il s'élargit quand l'IV monte (la volatilité réalisée est plus forte, rebalancer trop souvent coûte cher en frais de transaction).

```
threshold_pct = BASE_PCT × sqrt(IV_current / IV_ref)
threshold_pct = clamp(threshold_pct, 2%, 8%)
threshold_btc = threshold_pct / 100
```

| Paramètre | Valeur |
|---|---|
| Bande de base (`BASE_PCT`) | 5% |
| IV de référence (`IV_REF`) | 70% |
| Borne basse du seuil | 2% |
| Borne haute du seuil | 8% |

**Exemples :**
- IV = 70% → seuil = 5.0% (cas nominal)
- IV = 30% → seuil ≈ 3.3% (vol basse, on rebalance plus souvent)
- IV = 120% → seuil ≈ 6.5% (vol haute, on tolère plus de drift)

Le rebalancement est déclenché quand `|delta_drift| > threshold_btc`.

### VWAP du hedge

Le VWAP (prix d'entrée moyen pondéré) du hedge est mis à jour à chaque exécution :
- **Short supplémentaire** : VWAP recalculé par moyenne pondérée
- **Rachat partiel** : VWAP entrée inchangé, PnL réalisé comptabilisé sur la portion clôturée
- **Clôture totale** : PnL réalisé sur toute la position

---

## PnL Attribution

Pour chaque position, le PnL option est décomposé en 5 contributions :

| Composante | Formule |
|---|---|
| Δ Delta | `\|delta\| × ΔSpot` |
| Γ Gamma | `0.5 × (−gamma) × ΔSpot²` |
| Θ Theta | `theta_daily_usd × jours_tenus` |
| ν Vega | `(−vega) × ΔIV_pts` |
| Résidu | `PnL_total − (delta + gamma + theta + vega)` |

Le résidu capture les effets d'ordre supérieur, les frictions et les erreurs de modèle.

---

## Paramètres globaux résumé

```python
# Portefeuille
MAX_PORTFOLIO_BTC  = 3.0     # notionnel total max (BTC)

# Scan
SCAN_TTE_MIN       = 5.0     # TTE min (jours)
SCAN_TTE_MAX       = 30.0    # TTE max (jours)
SCAN_DELTA_MIN     = -0.30   # delta min
SCAN_DELTA_MAX     = -0.10   # delta max
BA_MAX_PCT         = 12.0    # spread B/A max (% du mark)

# Signal d'entrée
ENTRY_SCORE_MIN    = 0.58    # score composite minimum
ENTRY_IV_HV_MIN    = 1.10    # ratio IV/HV minimum

# Roll
ROLL_TRIGGER            = 1.0   # TTE (jours) pour entrer en fenêtre roll
GAMMA_ROLL_THRESHOLD    = 6.0   # gamma_pts au-dessus duquel on rolle

# Hedge
HEDGE_THRESHOLD_BASE_PCT = 5.0  # bande de base (% delta)
HEDGE_IV_REF             = 70.0 # IV de référence pour calibration
# seuil effectif clampé entre 2% et 8%
```

---

## Dashboard

Accessible sur la GitHub Page du repo. Mis à jour toutes les heures par GitHub Actions. Contient :

- **Header** : spot, PnL total, TTE min, drift hedge
- **Positions ouvertes** : strike, TTE, prime, mark/ask, IV, greeks, PnL, score d'entrée, sizing
- **Greeks nets** : delta/gamma/vega/theta cumulés + état hedge + barre de drift
- **PnL global** : option + hedge (MtM + réalisé) + funding + cumul stratégie
- **Attribution PnL** : décomposition delta/gamma/theta/vega/résidu par position
- **Opportunités d'entrée** : top 5 candidats scorés du dernier scan avec contexte de marché et rappel des seuils
- **Historique hedge** : toutes les exécutions BTC-PERPETUAL
- **Alertes** : roll, rebalancement, spike IV, stop-loss
- **Graphiques** : greeks et PnL dans le temps
