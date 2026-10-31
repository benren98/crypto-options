"""
generate_autocall_html.py — page du pricer d'autocall BTC (docs/autocall.html).

Recalibre la surface SSVI sur la chaîne Deribit (autocall_market.py) puis injecte dans le template
autocall_page.html : CSS/JS communs des dashboards, moteur de pricing (autocall_engine.js, exécuté
dans le navigateur) et données de marché. Le pricing lui-même tourne côté navigateur : la page reste
statique (GitHub Pages) et réagit à chaque paramètre du produit.

Usage : python generate_autocall_html.py [--no-refresh] [--out docs/autocall.html]
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).parent
ASSETS = HERE / "dashboard_assets"


def render(market: dict, template: Path) -> str:
    html = template.read_text(encoding="utf-8")
    html = html.replace("/*__COMMON_CSS__*/", (ASSETS / "common.css").read_text(encoding="utf-8"))
    html = html.replace("/*__COMMON_JS__*/", (ASSETS / "common.js").read_text(encoding="utf-8"))
    html = html.replace("/*__AUTOCALL_ENGINE__*/", (HERE / "autocall_engine.js").read_text(encoding="utf-8"))
    payload = json.dumps(market, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return html.replace("/*__AUTOCALL_DATA__*/null", payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-refresh", action="store_true", help="réutilise autocall_market.json sans appeler Deribit")
    ap.add_argument("--market", default=str(HERE / "autocall_market.json"))
    ap.add_argument("--out", default=str(HERE / "docs" / "autocall.html"))
    a = ap.parse_args()
    if a.no_refresh and Path(a.market).exists():
        market = json.loads(Path(a.market).read_text(encoding="utf-8"))
    else:
        import autocall_market
        market = autocall_market.build(a.market)
    out = Path(a.out)
    out.write_text(render(market, HERE / "autocall_page.html"), encoding="utf-8")
    s = market["ssvi"]
    print(f"{out.name} généré ({out.stat().st_size // 1024} KB) — surface du {market['as_of']} · "
          f"{len(market['expiries'])} échéances · RMSE SSVI {s['rmse_volpts']} pt")


if __name__ == "__main__":
    main()
