"""
Pousse positions.json vers le Gist (secours manuel). Le token et l'ID du Gist sont lus
dans .env (GITHUB_TOKEN, GIST_ID) par gist_sync — aucun secret dans ce fichier.

Usage (depuis la racine du projet) : python tools/push_gist.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gist_sync import push_positions, read_positions_from_gist  # noqa: E402

if __name__ == "__main__":
    ok = push_positions(ROOT / "positions.json")
    print("Status:", "OK" if ok else "ECHEC")
    if ok:
        remote = read_positions_from_gist() or {}
        print(f"Positions dans le Gist: {len(remote.get('positions', []))}")
        for p in remote.get("positions", []):
            print(f"  - {p['instrument_name']}")
