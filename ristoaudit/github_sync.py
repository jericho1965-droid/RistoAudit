"""Sincronizzazione automatica del database JSON dei ristoranti su GitHub.

Usa un branch dedicato (`data-sync` di default) cosi' i salvataggi non scatenano
re-deploy automatici del servizio web (es. Render su `main`), mantenendo pero'
tutti i dati dei ristoranti sempre accessibili e aggiornati da qualsiasi device.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi
import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = BASE_DIR / ".ristoaudit_settings.json"

DEFAULT_GITHUB_REPO = "jericho1965-droid/RistoAudit"
DEFAULT_GITHUB_BRANCH = "data-sync"
DEFAULT_GITHUB_PATH = "data/restaurants_db.json"

_SYNC_LOCK = threading.Lock()
_REMOTE_CACHE: dict[str, Any] = {"fetched_at": 0.0, "db": None, "sha": ""}


def _get_ssl_verify():
    try:
        return ssl.create_default_context()
    except Exception:
        return certifi.where()


def get_github_config() -> dict[str, str]:
    """Legge token, repository, branch dati e path file JSON da env o .ristoaudit_settings.json."""
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("github_token") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or os.environ.get("github_repo") or "").strip()
    branch = (os.environ.get("GITHUB_DATA_BRANCH") or os.environ.get("github_data_branch") or "").strip()
    path = (os.environ.get("GITHUB_DATA_PATH") or os.environ.get("github_data_path") or "").strip()

    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                token = token or str(saved.get("github_token") or "").strip()
                repo = repo or str(saved.get("github_repo") or "").strip()
                branch = branch or str(saved.get("github_data_branch") or "").strip()
                path = path or str(saved.get("github_data_path") or "").strip()
        except Exception:
            pass

    return {
        "token": token,
        "repo": repo or DEFAULT_GITHUB_REPO,
        "branch": branch or DEFAULT_GITHUB_BRANCH,
        "path": path or DEFAULT_GITHUB_PATH,
    }


def _api_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "RistoAudit-Sync/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _restaurant_identity(row: dict[str, Any]) -> tuple[str, str]:
    pid = str(row.get("google_place_id") or row.get("place_id") or "").strip()
    name = _norm_name(str(row.get("restaurant_name") or row.get("name") or ""))
    return pid, name


def _merge_single_restaurant(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Unisce due record dello stesso ristorante conservando i valori piu' recenti e completi."""
    merged = dict(base)
    base_ts = str(base.get("updated_at") or "")
    inc_ts = str(incoming.get("updated_at") or "")
    incoming_is_newer = bool(inc_ts and inc_ts >= base_ts)

    for k, v in incoming.items():
        if v is None or v == "":
            continue
        if incoming_is_newer or not merged.get(k):
            merged[k] = v

    if incoming.get("custom_added") or base.get("custom_added"):
        merged["custom_added"] = bool(incoming.get("custom_added") or base.get("custom_added"))
    if inc_ts > base_ts:
        merged["updated_at"] = inc_ts
    return merged


def merge_db_dicts(remote_db: dict[str, Any] | None, local_db: dict[str, Any] | None) -> dict[str, Any]:
    """Fonde il DB remoto di GitHub con quello locale senza perdere ristoranti o modifiche di altri device."""
    merged_cities: dict[str, dict[str, Any]] = {}

    for src in (remote_db or {}, local_db or {}):
        cities = src.get("cities") if isinstance(src, dict) else None
        if not isinstance(cities, dict):
            continue
        for slug, city_obj in cities.items():
            if not isinstance(city_obj, dict):
                continue
            city_name = str(city_obj.get("city") or slug).strip()
            rows = city_obj.get("restaurants")
            if not isinstance(rows, list):
                continue

            if slug not in merged_cities:
                merged_cities[slug] = {
                    "city": city_name,
                    "top50_loaded": bool(city_obj.get("top50_loaded")),
                    "restaurants": [],
                }
            else:
                if city_obj.get("top50_loaded"):
                    merged_cities[slug]["top50_loaded"] = True

            target_list = merged_cities[slug]["restaurants"]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid, name = _restaurant_identity(row)
                if not pid and not name:
                    continue
                match_idx: int | None = None
                for idx, existing in enumerate(target_list):
                    e_pid, e_name = _restaurant_identity(existing)
                    if (pid and e_pid and pid == e_pid) or (name and e_name and name == e_name):
                        match_idx = idx
                        break
                if match_idx is None:
                    target_list.append(dict(row))
                else:
                    target_list[match_idx] = _merge_single_restaurant(target_list[match_idx], row)

    for city_obj in merged_cities.values():
        city_obj["count"] = len(city_obj["restaurants"])

    return {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "cities": merged_cities,
    }


def _ensure_branch_exists(client: httpx.Client, repo: str, branch: str, token: str) -> bool:
    """Verifica se il branch dati esiste su GitHub e lo crea da `main` se assente."""
    headers = _api_headers(token)
    r_ref = client.get(f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}", headers=headers)
    if r_ref.status_code == 200:
        return True
    if r_ref.status_code != 404:
        return False

    # Recupera lo SHA di main (o default branch)
    base_sha = ""
    for candidate in ("main", "master"):
        r_base = client.get(f"https://api.github.com/repos/{repo}/git/ref/heads/{candidate}", headers=headers)
        if r_base.status_code == 200:
            base_sha = str((r_base.json().get("object") or {}).get("sha") or "")
            if base_sha:
                break
    if not base_sha:
        return False

    r_create = client.post(
        f"https://api.github.com/repos/{repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    return r_create.status_code in (200, 201, 422)


def fetch_remote_db(max_age_seconds: float = 15.0, force: bool = False) -> tuple[dict[str, Any] | None, str]:
    """Scarica il file JSON centralizzato dei ristoranti da GitHub (`data/restaurants_db.json`).

    Usa una breve cache in memoria (`max_age_seconds`) per evitare chiamate ripetute
    durante la digitazione; passa `force=True` prima di una scrittura o di un audit.
    """
    now = time.time()
    if not force and _REMOTE_CACHE["db"] is not None and (now - _REMOTE_CACHE["fetched_at"]) <= max_age_seconds:
        return _REMOTE_CACHE["db"], str(_REMOTE_CACHE["sha"] or "")

    cfg = get_github_config()
    repo = cfg["repo"]
    branch = cfg["branch"]
    path = cfg["path"]
    token = cfg["token"]

    api_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    headers = _api_headers(token)

    try:
        with httpx.Client(verify=_get_ssl_verify(), timeout=10.0) as client:
            resp = client.get(api_url, headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                sha = str(payload.get("sha") or "")
                raw_b64 = payload.get("content") or ""
                if raw_b64:
                    decoded = base64.b64decode(raw_b64).decode("utf-8")
                    db = json.loads(decoded)
                    if isinstance(db, dict) and isinstance(db.get("cities"), dict):
                        _REMOTE_CACHE.update({"fetched_at": time.time(), "db": db, "sha": sha})
                        return db, sha
                elif payload.get("download_url"):
                    r_dl = client.get(payload["download_url"], headers=headers)
                    if r_dl.status_code == 200:
                        db = r_dl.json()
                        if isinstance(db, dict) and isinstance(db.get("cities"), dict):
                            _REMOTE_CACHE.update({"fetched_at": time.time(), "db": db, "sha": sha})
                            return db, sha
            elif resp.status_code == 404:
                return None, ""
            else:
                # Fallback raw.githubusercontent.com per repo pubblici senza token/rate-limit
                if not token:
                    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}?ts={int(now)}"
                    r_raw = client.get(raw_url)
                    if r_raw.status_code == 200:
                        db = r_raw.json()
                        if isinstance(db, dict) and isinstance(db.get("cities"), dict):
                            _REMOTE_CACHE.update({"fetched_at": time.time(), "db": db, "sha": ""})
                            return db, ""
                print(f"[RistoAudit] GitHub sync GET HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[RistoAudit] GitHub sync GET errore: {exc}")

    return None, ""


def push_db_to_github(
    local_db: dict[str, Any],
    commit_message: str = "Update restaurants database",
) -> dict[str, Any]:
    """Fonde `local_db` con GitHub e committa il JSON aggiornato sul branch `data-sync`."""
    cfg = get_github_config()
    token = cfg["token"]
    repo = cfg["repo"]
    branch = cfg["branch"]
    path = cfg["path"]

    if not token:
        return {
            "synced": False,
            "reason": "missing_github_token",
            "repo": repo,
            "branch": branch,
            "path": path,
        }

    with _SYNC_LOCK:
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = _api_headers(token)
        try:
            with httpx.Client(verify=_get_ssl_verify(), timeout=15.0) as client:
                if not _ensure_branch_exists(client, repo, branch, token):
                    return {"synced": False, "reason": "branch_creation_failed", "repo": repo, "branch": branch, "path": path}

                for attempt in range(2):
                    remote_db, sha = fetch_remote_db(force=True)
                    merged = merge_db_dicts(remote_db, local_db)
                    encoded_json = json.dumps(merged, indent=2, ensure_ascii=False).encode("utf-8")
                    body: dict[str, Any] = {
                        "message": commit_message[:120],
                        "content": base64.b64encode(encoded_json).decode("ascii"),
                        "branch": branch,
                    }
                    if sha:
                        body["sha"] = sha

                    r_put = client.put(api_url, headers=headers, json=body)
                    if r_put.status_code in (200, 201):
                        new_sha = str(((r_put.json().get("content") or {}).get("sha")) or "")
                        _REMOTE_CACHE.update({"fetched_at": time.time(), "db": merged, "sha": new_sha})
                        print(f"[RistoAudit] GitHub sync OK -> {repo}@{branch}:{path} ({commit_message})")
                        return {
                            "synced": True,
                            "repo": repo,
                            "branch": branch,
                            "path": path,
                            "sha": new_sha,
                            "merged_db": merged,
                        }
                    if r_put.status_code == 409 and attempt == 0:
                        continue
                    print(f"[RistoAudit] GitHub sync PUT HTTP {r_put.status_code}: {r_put.text[:250]}")
                    return {
                        "synced": False,
                        "reason": f"http_{r_put.status_code}",
                        "repo": repo,
                        "branch": branch,
                        "path": path,
                    }
        except Exception as exc:
            print(f"[RistoAudit] GitHub sync PUT errore: {exc}")
            return {"synced": False, "reason": str(exc), "repo": repo, "branch": branch, "path": path}

    return {"synced": False, "reason": "conflict_retry_exhausted", "repo": repo, "branch": branch, "path": path}
