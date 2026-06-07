"""
Gist Sync — synchronise positions.json vers GitHub Gist
Appelé automatiquement après chaque trade/roll.
"""

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# Charge .env si python-dotenv dispo, sinon lit manuellement
ENV_FILE = Path(__file__).parent / ".env"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def push_positions(positions_path: Path = None) -> bool:
    """
    Pousse positions.json vers le Gist GitHub.
    Retourne True si succès, False sinon.
    """
    if positions_path is None:
        positions_path = Path(__file__).parent / "positions.json"

    env = load_env()
    token   = env.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    gist_id = env.get("GIST_ID")      or os.getenv("GIST_ID")

    if not token or not gist_id:
        print("  [gist] GITHUB_TOKEN ou GIST_ID manquant dans .env — sync ignoré")
        return False

    if not positions_path.exists():
        print("  [gist] positions.json introuvable")
        return False

    # Ajoute last_updated avant de pousser
    state = json.loads(positions_path.read_text())
    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    content = json.dumps(state, indent=2, default=str)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "files": {
            "positions.json": {"content": content}
        }
    }

    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=headers,
            json=payload,
            verify=False,
            timeout=10,
        )
        if r.status_code == 200:
            print(f"  [gist] Sync OK -> gist.github.com/{gist_id}")
            return True
        else:
            print(f"  [gist] Erreur {r.status_code}: {r.text[:100]}")
            return False
    except Exception as e:
        print(f"  [gist] Erreur reseau: {e}")
        return False


def read_positions_from_gist() -> dict | None:
    """Lit positions.json depuis le Gist (utile pour debug)."""
    env = load_env()
    token   = env.get("GITHUB_TOKEN")
    gist_id = env.get("GIST_ID")
    if not token or not gist_id:
        return None
    try:
        r = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {token}"},
            verify=False, timeout=10,
        )
        content = r.json()["files"]["positions.json"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"  [gist] Lecture impossible: {e}")
        return None


if __name__ == "__main__":
    print("Test sync positions.json -> Gist...")
    ok = push_positions()
    print("Resultat:", "OK" if ok else "ECHEC")
