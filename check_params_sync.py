"""
Vérifie que backtest.py miroite bien les constantes de greeks_hedge.py.

Parse les deux fichiers par regex (pas d'import : évite réseau/effets de bord)
et compare via une table de correspondance live → backtest. Exit code 1 si drift.

Usage : python check_params_sync.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent

# Correspondance nom_live → nom_backtest (les noms diffèrent parfois)
MIRROR = {
    "ENTRY_SCORE_MIN":           "ENTRY_SCORE_MIN",
    "MAX_PORTFOLIO_BTC":         "MAX_PORTFOLIO_BTC",
    "GAMMA_PENALTY_START":       "GAMMA_PEN_START",
    "GAMMA_SCORE_CAP":           "GAMMA_SCORE_CAP",
    "SKEW_NORM":                 "SKEW_NORM",
    "IVHV_NORM":                 "IVHV_NORM",
    "SCORE_W_IVHV":              "SCORE_W_IVHV",
    "SCORE_W_YIELD":             "SCORE_W_YIELD",
    "SCORE_W_SKEW":              "SCORE_W_SKEW",
    "SIZE_CONVEXITY":            "SIZE_CONVEXITY",
    "MIN_PREMIUM_USD":           "MIN_PREMIUM_USD",
    "ENTRY_SCORE_REENTRY_BOOST": "ENTRY_SCORE_REENTRY_BOOST",
    "RANK_FLOOR":                "RANK_FLOOR",
    "CB_MOVE_3D_PCT":            "CB_MOVE_3D_PCT",
    "CB_DVOL_3D_PTS":            "CB_DVOL_3D_PTS",
    "CB_REENTRY_MOVE_PCT":       "CB_REENTRY_MOVE",
    "GRADUATED_CB":              "GRADUATED_CB",
    "CB_T1_MOVE_1D_PCT":         "CB_T1_MOVE_1D",
    "CB_T1_MOVE_3D_PCT":         "CB_T1_MOVE_3D",
    "CB_T1_KEEP":                "CB_T1_KEEP",
    "CB_T1_RESTORE_MOVE_PCT":    "CB_T1_RESTORE",
}

# Divergences assumées (documentées) : ne déclenchent pas d'erreur
KNOWN_EXCEPTIONS = {
    # backtest utilise un spacing plus étroit (grille de strikes discrète du simulateur)
    "DELTA_MIN_SPACING": "0.08 live vs 0.04 backtest — assumé",
}

ASSIGN_RE = re.compile(r"^([A-Z][A-Z0-9_]+)\s*=\s*([^#\n]+)", re.MULTILINE)


def parse_constants(path: Path) -> dict:
    consts = {}
    for m in ASSIGN_RE.finditer(path.read_text(encoding="utf-8")):
        name, raw = m.group(1), m.group(2).strip()
        try:
            consts[name] = eval(raw, {"__builtins__": {}}, {})
        except Exception:
            consts[name] = raw  # non-évaluable (Path, etc.) → comparaison brute
    return consts


def main() -> int:
    live = parse_constants(HERE / "greeks_hedge.py")
    bt   = parse_constants(HERE / "backtest.py")

    errors = []
    for live_name, bt_name in MIRROR.items():
        if live_name not in live:
            errors.append(f"{live_name} absent de greeks_hedge.py (renommé ?)")
            continue
        if bt_name not in bt:
            errors.append(f"{bt_name} absent de backtest.py (miroir manquant pour {live_name})")
            continue
        lv, bv = live[live_name], bt[bt_name]
        if lv != bv:
            errors.append(f"{live_name}={lv} (live)  !=  {bt_name}={bv} (backtest)")

    if errors:
        print("[FAIL] Drift de parametres live <-> backtest :")
        for e in errors:
            print(f"  - {e}")
        print("\nSynchroniser backtest.py avec greeks_hedge.py (cf. CLAUDE.md).")
        return 1

    print(f"[OK] {len(MIRROR)} parametres synchronises entre greeks_hedge.py et backtest.py")
    if KNOWN_EXCEPTIONS:
        for k, why in KNOWN_EXCEPTIONS.items():
            print(f"  (exception assumee : {k} — {why})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
