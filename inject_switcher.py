"""
inject_switcher.py — ajoute un sélecteur de design flottant à chaque page de docs/.

Pendant le choix du design du dashboard, toutes les variantes sont publiées côte à côte
(v1, v2, Cockpit, Éditorial, Bento, Backtests) ; ce petit widget (coin bas-droit, sans JS)
permet de passer de l'une à l'autre. Idempotent : le bloc est remplacé à chaque exécution.

Usage : python inject_switcher.py [--docs docs]
"""
import argparse
import re
from pathlib import Path

PAGES = [
    ("index.html", "v1 · historique"),
    ("v2.html", "v2 · décision"),
    ("design-cockpit.html", "Cockpit"),
    ("design-editorial.html", "Éditorial"),
    ("design-bento.html", "Bento"),
    ("backtest.html", "Backtests"),
]
START, END = "<!--design-switcher-->", "<!--/design-switcher-->"

CSS = """
#dsw{position:fixed;right:16px;bottom:16px;z-index:2147483000;font:500 13px/1.3 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#f5f7fa}
#dsw summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;background:rgba(17,20,28,.88);border:1px solid rgba(255,255,255,.18);box-shadow:0 6px 20px rgba(0,0,0,.28);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);user-select:none}
#dsw summary::-webkit-details-marker{display:none}
#dsw summary:focus-visible,#dsw a:focus-visible{outline:2px solid #7aa7ff;outline-offset:2px}
#dsw .dsw-k{opacity:.7;font-weight:400}
#dsw .dsw-c{font-weight:600}
#dsw .dsw-ar{opacity:.7;transition:transform .15s}
#dsw[open] .dsw-ar{transform:rotate(180deg)}
#dsw ul{position:absolute;right:0;bottom:calc(100% + 8px);margin:0;padding:6px;list-style:none;min-width:190px;border-radius:14px;background:rgba(17,20,28,.94);border:1px solid rgba(255,255,255,.18);box-shadow:0 10px 30px rgba(0,0,0,.35);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
#dsw li{margin:0;padding:0}
#dsw a{display:flex;justify-content:space-between;gap:12px;padding:8px 10px;border-radius:9px;color:#f5f7fa;text-decoration:none}
#dsw a:hover{background:rgba(255,255,255,.1)}
#dsw a[aria-current=page]{background:rgba(122,167,255,.22);font-weight:600}
#dsw a[aria-current=page]::after{content:"✓";opacity:.9}
@media (max-width:480px){#dsw{right:12px;bottom:12px}#dsw .dsw-k{display:none}}
@media print{#dsw{display:none}}
""".strip()


def block(current: str) -> str:
    cur_label = next((lab for f, lab in PAGES if f == current), current)
    cur_attr = ' aria-current="page"'
    items = "".join(
        f'<li><a href="{f}"{cur_attr if f == current else ""}>{lab}</a></li>'
        for f, lab in PAGES)
    return (f'{START}<style>{CSS}</style>'
            f'<details id="dsw"><summary aria-label="Changer de design du dashboard">'
            f'<span class="dsw-k">Design</span><span class="dsw-c">{cur_label}</span>'
            f'<span class="dsw-ar" aria-hidden="true">▴</span></summary>'
            f'<ul>{items}</ul></details>{END}')


def inject(path: Path) -> bool:
    html = path.read_text(encoding="utf-8")
    html = re.sub(re.escape(START) + r".*?" + re.escape(END), "", html, flags=re.S)
    b = block(path.name)
    i = html.lower().rfind("</body>")
    html = html[:i] + b + html[i:] if i >= 0 else html + b
    path.write_text(html, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="docs")
    a = ap.parse_args()
    done = [f for f, _ in PAGES if (Path(a.docs) / f).exists() and inject(Path(a.docs) / f)]
    print(f"inject_switcher : sélecteur ajouté à {len(done)} page(s) — {', '.join(done)}")


if __name__ == "__main__":
    main()
