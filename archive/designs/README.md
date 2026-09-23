# Propositions de design du dashboard (archivées)

Trois variantes du dashboard v2, créées le 2026-09-23, publiées un temps puis écartées le
2026-09-24 : la v2 (`dashboard_v2.html`) a été jugée plus lisible. Gardées ici pour
réutiliser des idées ou des composants. Ce dossier n'est **pas** publié (GitHub Pages ne sert
que `docs/`) et le workflow ne les régénère pas.

| Variante | Idée | Éléments réutilisables |
|---|---|---|
| `cockpit/` | Salle de marché sombre, esprit tableau de bord d'avion (thème sombre seulement) | Jauges radiales SVG (exposition, delta vs bande, déclencheurs du CB avec distance restante), cascade du PnL, carte « meilleur candidat », zone DVOL–HV ombrée |
| `editorial/` | Une de quotidien financier (« Journal de pilotage »), clair + « édition de nuit » | Chapô rédigé depuis les données, titres de rubrique calculés, graphiques annotés (allègements CB, expirations, rebalancements, zones VRP négative) |
| `bento/` | Grille de cartes façon Apple, clair et sombre | Carte héro avec halo selon le verdict, anneau de score au seuil, bande des candidats face à la zone d'entrée (survol lié au tableau), distances du CB en clair |

Les trois lisent le même modèle JSON que la v2 (`generate_dashboard.py`) : le score est en
première colonne du scanner, avec l'écart au seuil.

## Les rendre avec les données actuelles

```bash
python generate_dashboard.py --template archive/designs/cockpit/template.html --out archive/designs/cockpit/preview.html
```

(idem avec `editorial` ou `bento`). Les aperçus `preview.html` sont ignorés par git.
Pour tester un état chargé du book : ajouter `--data-dir <dossier> --now "2026-08-29 23:50:00"`.

`inject_switcher.py` ajoute un sélecteur flottant entre pages d'un dossier (liste `PAGES` à
adapter) — utile pour comparer à nouveau des variantes côte à côte.
