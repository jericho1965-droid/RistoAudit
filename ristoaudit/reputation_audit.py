"""Audit presenza social, recensioni e reputazione online.

Combina:
- Lettura automatica completa da Google Places API (New) via Place ID, link Google Maps o nome + citta'
- Classificazione automatica recensioni positive / neutre / negative e temi ricorrenti nelle critiche
- Verifica completezza scheda Google Business Profile (orari, foto, telefono, sito, prenotazioni)
- Link social estratti dal sito web e profili rilevati sul web
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import certifi
import httpx

from .models import AuditInput

_PLACE_ID_RE = re.compile(r"\b(ChIJ[A-Za-z0-9_-]{15,})\b")
_MAPS_PLACE_NAME_RE = re.compile(r"/maps/place/([^/@]+)", re.I)
_MAPS_COORDS_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
_TA_LOCATION_ID_RE = re.compile(r"-d(\d+)(?:-|\.html|$)", re.I)

# Temi critici comuni nelle recensioni ristoranti (IT + EN)
COMPLAINT_THEMES: dict[str, tuple[str, ...]] = {
    "Tempi di attesa / Servizio lento": (
        "attesa", "aspettato", "lento", "lenti", "lentezza", "ritardo", "un'ora", "ora per",
        "slow", "wait", "waited", "waiting", "delay",
    ),
    "Prezzi / Rapporto qualità-prezzo": (
        "caro", "cara", "cari", "costoso", "prezzi alti", "prezzo alto", "conto salato",
        "esagerato", "coperto", "rapporto qualità", "overpriced", "expensive", "pricey",
    ),
    "Qualità cibo / Piatti": (
        "freddo", "fredda", "crudo", "bruciato", "salato", "insipido", "scotto", "immangiabile",
        "surgelato", "porzioni piccole", "porzioni scarse", "deludente", "cold", "tasteless", "overcooked",
    ),
    "Accoglienza / Cortesia personale": (
        "maleducato", "maleducati", "scortese", "scortesi", "arrogante", "sbrigativo", "antipatico",
        "cameriere", "rude", "unfriendly", "impolite",
    ),
    "Organizzazione / Prenotazione / Menù": (
        "prenotazione", "prenotato", "tavolo", "menù", "menu", "diverso", "non disponibile",
        "finito", "confusione", "disorganizzato", "reservation", "unavailable",
    ),
    "Pulizia / Ambiente": (
        "sporco", "sporca", "pulizia", "bagno", "rumoroso", "caotico", "caldo", "dirty", "noisy",
    ),
}


def _parse_int(val: str) -> int | None:
    try:
        cleaned = str(val or "").strip().replace(".", "").replace(",", "")
        return int(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_float(val: str) -> float | None:
    try:
        cleaned = str(val or "").strip().replace(",", ".")
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def extract_place_info_from_maps_url(
    maps_url: str,
    explicit_place_id: str = "",
) -> dict[str, Any]:
    """Estrae Place ID (ChIJ...), nome luogo o coordinate da un link Google Maps."""
    info: dict[str, Any] = {
        "place_id": "",
        "place_name": "",
        "lat": None,
        "lng": None,
        "resolved_url": (maps_url or "").strip(),
    }

    if explicit_place_id.strip():
        m = _PLACE_ID_RE.search(explicit_place_id.strip())
        info["place_id"] = m.group(1) if m else explicit_place_id.strip()
        return info

    url = (maps_url or "").strip()
    if not url:
        return info

    # Permette anche di incollare direttamente un Place ID nel campo link Maps
    if _PLACE_ID_RE.fullmatch(url):
        info["place_id"] = url
        return info

    parsed = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("place_id", "query_place_id", "placeid"):
        if qs.get(key):
            info["place_id"] = qs[key][0].strip()
            return info

    m_id = _PLACE_ID_RE.search(url)
    if m_id:
        info["place_id"] = m_id.group(1)
        return info

    if any(dom in url.lower() for dom in ("maps.app.goo.gl", "goo.gl/maps")):
        try:
            with httpx.Client(verify=certifi.where(), follow_redirects=True, timeout=8.0) as client:
                r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                url = str(r.url)
                info["resolved_url"] = url
        except Exception:
            pass

    m_id2 = _PLACE_ID_RE.search(url)
    if m_id2:
        info["place_id"] = m_id2.group(1)
        return info

    m_name = _MAPS_PLACE_NAME_RE.search(url)
    if m_name:
        info["place_name"] = urllib.parse.unquote_plus(m_name.group(1)).replace("+", " ").strip()

    m_coords = _MAPS_COORDS_RE.search(url)
    if m_coords:
        try:
            info["lat"] = float(m_coords.group(1))
            info["lng"] = float(m_coords.group(2))
        except ValueError:
            pass

    return info


def _classify_review_sentiment(rating: int | None) -> str:
    if rating is None:
        return "neutra"
    if rating >= 4:
        return "positiva"
    if rating == 3:
        return "neutra"
    return "negativa"


# Temi positivi comuni nelle recensioni ristoranti (IT + EN)
PRAISE_THEMES: dict[str, tuple[str, ...]] = {
    "Qualità della cucina e dei piatti": (
        "ottimo", "ottima", "buonissimo", "buonissima", "delizioso", "squisito", "fresco", "freschi",
        "pizza", "pasta", "pesce", "carne", "dolci", "cucina", "delicious", "amazing food", "great food",
    ),
    "Accoglienza e cortesia dello staff": (
        "gentile", "gentili", "cortese", "cortesi", "accogliente", "disponibile", "simpatico",
        "personale", "servizio impeccabile", "servizio veloce", "friendly", "attentive", "kind staff",
    ),
    "Atmosfera e location": (
        "ambiente", "atmosfera", "location", "bellissimo", "curato", "pulito", "terrazza", "centro",
        "cozy", "atmosphere", "beautiful",
    ),
    "Buon rapporto qualità-prezzo": (
        "onesto", "onesti", "prezzi giusti", "qualità prezzo", "qualità-prezzo", "abbondanti", "fair price",
    ),
}


def _detect_praise_themes(reviews: list[dict[str, Any]]) -> list[str]:
    """Identifica i punti di forza lodati dai clienti nelle recensioni positive (4-5 stelle)."""
    found: list[str] = []
    pos_text = " ".join(
        r.get("text", "").lower()
        for r in reviews
        if r.get("rating") is not None and r["rating"] >= 4
    )
    if not pos_text.strip():
        return found
    for theme, keywords in PRAISE_THEMES.items():
        if any(kw in pos_text for kw in keywords):
            found.append(theme)
    return found


def _detect_complaint_themes(reviews: list[dict[str, Any]]) -> list[str]:
    """Identifica i temi ricorrenti nelle recensioni negative o neutre (<= 3 stelle)."""
    found: list[str] = []
    crit_text = " ".join(
        r.get("text", "").lower()
        for r in reviews
        if r.get("rating") is not None and r["rating"] <= 3
    )
    if not crit_text.strip():
        return found
    for theme, keywords in COMPLAINT_THEMES.items():
        if any(kw in crit_text for kw in keywords):
            found.append(theme)
    return found


def _parse_google_reviews(raw_reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalizza l'array reviews restituito da Google Places API (New) con classificazione sentiment."""
    parsed: list[dict[str, Any]] = []
    for rev in raw_reviews[:5]:
        if not isinstance(rev, dict):
            continue
        author = (
            (rev.get("authorAttribution") or {}).get("displayName")
            or rev.get("authorName")
            or "Utente Google"
        )
        text_obj = rev.get("text") or rev.get("originalText") or {}
        text = text_obj.get("text", "") if isinstance(text_obj, dict) else str(text_obj or "")
        rating = rev.get("rating")
        rating_int = int(rating) if rating is not None else None
        rel_time = rev.get("relativePublishTimeDescription") or ""
        pub_time = rev.get("publishTime") or rev.get("creationTime") or ""
        # Verifica eventuale campo risposta proprietario se mai esposto
        owner_reply = rev.get("ownerResponse") or rev.get("reply") or None
        parsed.append({
            "author": str(author).strip(),
            "rating": rating_int,
            "sentiment": _classify_review_sentiment(rating_int),
            "text": " ".join(str(text).split()),
            "relative_time": str(rel_time).strip(),
            "publish_time": str(pub_time).strip(),
            "has_owner_reply": bool(owner_reply) if owner_reply is not None else None,
        })
    return parsed


def fetch_google_places_data(
    name: str,
    city: str,
    address: str = "",
    google_maps_url: str = "",
    google_place_id: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    """Legge automaticamente tutti i dati pubblici del ristorante da Google Places API (New).

    Strategia ottimizzata in 2 step:
    1) Se il Place ID non e' gia' noto, esegue Text Search IDs-Only
       (POST /v1/places:searchText con X-Goog-FieldMask: places.id — gratuito/illimitato)
       per ottenere il Place ID esatto.
    2) Con il Place ID esegue Place Details (GET /v1/places/{place_id}?languageCode=it)
       per leggere voto, numero recensioni, ultime 5 recensioni testuali, sito web, orari e foto.
    """
    key = (api_key or "").strip() or os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not key:
        return {}

    maps_info = extract_place_info_from_maps_url(google_maps_url, google_place_id)
    place_id = maps_info.get("place_id", "")

    details_fields = (
        "id,displayName,formattedAddress,nationalPhoneNumber,rating,userRatingCount,"
        "googleMapsUri,websiteUri,regularOpeningHours,photos,editorialSummary,"
        "businessStatus,priceLevel,reservable,dineIn,takeout,delivery,reviews"
    )

    place_obj: dict[str, Any] = {}
    try:
        with httpx.Client(verify=certifi.where(), timeout=12.0) as client:
            # STEP 1: Recupero Place ID gratuito con Text Search (IDs Only)
            if not place_id:
                query_parts = [maps_info.get("place_name") or name, address, city]
                text_query = " ".join(p.strip() for p in query_parts if p and p.strip())
                body: dict[str, Any] = {
                    "textQuery": text_query,
                    "languageCode": "it",
                    "maxResultCount": 1,
                }
                if maps_info.get("lat") is not None and maps_info.get("lng") is not None:
                    body["locationBias"] = {
                        "circle": {
                            "center": {"latitude": maps_info["lat"], "longitude": maps_info["lng"]},
                            "radius": 500.0,
                        }
                    }
                search_headers = {
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": "places.id",
                }
                resp_id = client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    headers=search_headers,
                    json=body,
                )
                if resp_id.status_code == 200:
                    places = resp_id.json().get("places", [])
                    if places:
                        place_id = str(places[0].get("id") or "").strip()
                else:
                    print(f"[RistoAudit] Google Places searchText HTTP {resp_id.status_code}: {resp_id.text[:250]}")

            # STEP 2: Place Details (New) con il Place ID per ottenere recensioni e dati completi
            if place_id:
                url = f"https://places.googleapis.com/v1/places/{urllib.parse.quote(place_id)}?languageCode=it"
                details_headers = {
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": details_fields,
                }
                resp_det = client.get(url, headers=details_headers)
                if resp_det.status_code == 200:
                    place_obj = resp_det.json()
                else:
                    print(f"[RistoAudit] Google Places details HTTP {resp_det.status_code}: {resp_det.text[:250]}")
    except Exception as exc:
        print(f"[RistoAudit] Errore Google Places API: {exc}")
        return {}

    if not place_obj:
        return {}

    reviews = _parse_google_reviews(place_obj.get("reviews") or [])
    pos_count = sum(1 for r in reviews if r["sentiment"] == "positiva")
    neu_count = sum(1 for r in reviews if r["sentiment"] == "neutra")
    neg_count = sum(1 for r in reviews if r["sentiment"] == "negativa")
    complaint_themes = _detect_complaint_themes(reviews)
    praise_themes = _detect_praise_themes(reviews)
    photos = place_obj.get("photos") or []

    # Verifica se Google ha restituito eventuali info sulle risposte proprietario
    reply_known = [r for r in reviews if r.get("has_owner_reply") is not None]
    detected_unanswered = (
        sum(1 for r in reply_known if not r["has_owner_reply"])
        if reply_known else None
    )

    return {
        "place_id": place_obj.get("id") or place_id,
        "name": (place_obj.get("displayName") or {}).get("text", ""),
        "formatted_address": place_obj.get("formattedAddress", ""),
        "phone": place_obj.get("nationalPhoneNumber", ""),
        "rating": place_obj.get("rating"),
        "reviews_count": place_obj.get("userRatingCount"),
        "maps_uri": place_obj.get("googleMapsUri", ""),
        "website_uri": place_obj.get("websiteUri", ""),
        "has_opening_hours": bool(place_obj.get("regularOpeningHours")),
        "photos_count": len(photos),
        "has_editorial_summary": bool((place_obj.get("editorialSummary") or {}).get("text")),
        "business_status": place_obj.get("businessStatus", ""),
        "price_level": place_obj.get("priceLevel", ""),
        "reservable": place_obj.get("reservable"),
        "dine_in": place_obj.get("dineIn"),
        "takeout": place_obj.get("takeout"),
        "delivery": place_obj.get("delivery"),
        "reviews": reviews,
        "positive_reviews_count": pos_count,
        "neutral_reviews_count": neu_count,
        "negative_reviews_count": neg_count,
        "low_rating_reviews_count": neg_count + neu_count,
        "complaint_themes": complaint_themes,
        "praise_themes": praise_themes,
        "detected_unanswered_count": detected_unanswered,
    }


def extract_tripadvisor_location_id(tripadvisor_url: str) -> str:
    """Estrae il Location ID numerico da un URL TripAdvisor (es. -d2272025- -> 2272025)."""
    m = _TA_LOCATION_ID_RE.search(str(tripadvisor_url or ""))
    return m.group(1) if m else ""


def _extract_localized_text(val: Any, prefer_lang: str = "it") -> str:
    """Estrae testo leggibile da stringhe, dict {'value': ...} o liste localizzate Terra API."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return str(val.get("value") or val.get("text") or "").strip()
    if isinstance(val, list):
        items = [x for x in val if isinstance(x, dict) and (x.get("value") or x.get("text"))]
        if not items:
            return " ".join(str(x).strip() for x in val if x).strip()
        # 1. Preferisci italiano
        for it in items:
            if str(it.get("language") or "").lower().startswith(prefer_lang):
                return str(it.get("value") or it.get("text") or "").strip()
        # 2. Altrimenti quello marcato primary=True
        for it in items:
            if it.get("primary"):
                return str(it.get("value") or it.get("text") or "").strip()
        # 3. Altrimenti il primo disponibile
        return str(items[0].get("value") or items[0].get("text") or "").strip()
    return str(val).strip()


def _parse_terra_reviews(payload: Any) -> list[dict[str, Any]]:
    """Normalizza le recensioni restituite da TripAdvisor Terra API."""
    if isinstance(payload, dict):
        items = payload.get("reviews") or payload.get("data") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    parsed: list[dict[str, Any]] = []
    for rev in items[:5]:
        if not isinstance(rev, dict):
            continue
        rating = rev.get("rating")
        if isinstance(rating, dict):
            rating = rating.get("value") or rating.get("overall") or rating.get("score")
        try:
            rating_int = int(round(float(rating))) if rating is not None else None
        except (TypeError, ValueError):
            rating_int = None

        user = rev.get("user") or rev.get("author") or {}
        author = (
            user.get("username") or user.get("display_name") or user.get("name")
            if isinstance(user, dict) else str(user or "")
        ) or "Utente TripAdvisor"

        title = _extract_localized_text(rev.get("title"))
        text = _extract_localized_text(rev.get("text") or rev.get("body"))
        full_text = f"{title} — {text}" if (title and text and title.lower() not in text.lower()) else (text or title)

        raw_date = (
            rev.get("published_date")
            or rev.get("published_ts")
            or rev.get("published_at")
            or rev.get("submission_date")
            or rev.get("travel_date")
            or rev.get("created_at")
            or ""
        )
        pub_date = str(raw_date)[:10] if raw_date else ""

        mgmt = (
            rev.get("management_response")
            or rev.get("owner_response")
            or rev.get("response")
            or rev.get("reply")
        )
        has_reply = bool(mgmt)
        parsed.append({
            "author": str(author).strip(),
            "rating": rating_int,
            "sentiment": _classify_review_sentiment(rating_int),
            "text": " ".join(full_text.split()),
            "published_date": pub_date,
            "has_owner_reply": has_reply,
        })
    return parsed


def fetch_tripadvisor_terra_data(
    name: str,
    city: str,
    tripadvisor_url: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    """Legge dettagli, classifica e recensioni reali da TripAdvisor Terra API."""
    key = (api_key or "").strip() or os.environ.get("TRIPADVISOR_TERRA_API_KEY", "").strip()
    if not key:
        return {}

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": key,
    }
    loc_id = extract_tripadvisor_location_id(tripadvisor_url)
    details: dict[str, Any] = {}
    reviews_raw: Any = []

    try:
        with httpx.Client(verify=certifi.where(), timeout=12.0) as client:
            # Step 1: Se non abbiamo ancora il location_id dall'URL, cercalo via Terra recommendations/search
            if not loc_id:
                r_search = client.post(
                    "https://terra.tripadvisor.com/api/recommendations/search?version=1",
                    headers=headers,
                    json={
                        "geo": {},
                        "query": f"{name} {city}",
                        "top_level_categories": ["Eat & Drink"],
                        "limit": 5,
                        "response_preference": "quality",
                    },
                )
                if r_search.status_code == 200:
                    s_data = r_search.json()
                    candidates = (
                        s_data.get("recommendations")
                        or s_data.get("locations")
                        or s_data.get("results")
                        or s_data.get("data")
                        or []
                    )
                    name_tokens = [t for t in re.findall(r"[a-zA-Z0-9]+", name.lower()) if len(t) >= 3 and t not in ("ristorante", "pizzeria", "trattoria", "osteria")]
                    for cand in candidates:
                        if not isinstance(cand, dict):
                            continue
                        loc_obj = cand.get("location") if isinstance(cand.get("location"), dict) else cand
                        cand_name = _extract_localized_text(loc_obj.get("name") or loc_obj.get("title")).lower()
                        if name_tokens and all(tok in cand_name for tok in name_tokens[:2]):
                            cid = loc_obj.get("id") or loc_obj.get("location_id")
                            if cid:
                                loc_id = str(cid)
                                break
                    if not loc_id:
                        print(f"[RistoAudit] TripAdvisor Terra search 200 ma nessun match per '{name}': keys={list(s_data.keys()) if isinstance(s_data, dict) else type(s_data)}")
                else:
                    print(f"[RistoAudit] TripAdvisor Terra search HTTP {r_search.status_code}: {r_search.text[:250]}")

            if not loc_id:
                return {}

            # Step 2: Dettagli location
            r_det = client.get(
                f"https://terra.tripadvisor.com/api/locations/{loc_id}?version=1&locale=it-IT",
                headers=headers,
            )
            if r_det.status_code == 200:
                details = r_det.json()
            else:
                print(f"[RistoAudit] TripAdvisor Terra details HTTP {r_det.status_code}: {r_det.text[:250]}")

            # Step 3: Recensioni recenti
            r_rev = client.get(
                f"https://terra.tripadvisor.com/api/locations/{loc_id}/reviews?sort_by=MOST_RECENT",
                headers=headers,
            )
            if r_rev.status_code == 200:
                reviews_raw = r_rev.json()
            else:
                print(f"[RistoAudit] TripAdvisor Terra reviews HTTP {r_rev.status_code}: {r_rev.text[:250]}")
    except Exception as exc:
        print(f"[RistoAudit] Errore TripAdvisor Terra API: {exc}")
        return {}

    if not details and not reviews_raw:
        return {}

    # Estrazione flessibile campi Terra (supporta sia struttura piatta sia nested)
    loc = details.get("location") if isinstance(details.get("location"), dict) else details
    rev_summary = loc.get("reviews_summary") or loc.get("review_summary") or loc.get("rating_summary") or {}
    if not isinstance(rev_summary, dict):
        rev_summary = {}

    rating_raw = (
        loc.get("rating")
        or loc.get("average_rating")
        or loc.get("review_rating")
        or rev_summary.get("rating")
        or rev_summary.get("average")
    )
    if isinstance(rating_raw, dict):
        rating_raw = rating_raw.get("value") or rating_raw.get("average") or rating_raw.get("overall")
    try:
        ta_rating = float(rating_raw) if rating_raw is not None else None
    except (TypeError, ValueError):
        ta_rating = None

    rev_count_raw = (
        loc.get("num_reviews")
        or loc.get("review_count")
        or loc.get("total_reviews")
        or rev_summary.get("count")
        or rev_summary.get("total")
    )
    try:
        ta_reviews_count = int(rev_count_raw) if rev_count_raw is not None else None
    except (TypeError, ValueError):
        ta_reviews_count = None

    ranking = _extract_localized_text(loc.get("ranking_string"))
    if not ranking and isinstance(loc.get("ranking_data"), dict):
        ranking = _extract_localized_text(loc["ranking_data"].get("ranking_string"))
    if not ranking and isinstance(loc.get("ranking"), dict):
        rk = loc["ranking"]
        if rk.get("rank") and rk.get("total"):
            ranking = f"#{rk['rank']} su {rk['total']}"

    reviews = _parse_terra_reviews(reviews_raw)
    unanswered_ta = sum(1 for r in reviews if not r.get("has_owner_reply")) if reviews else None

    return {
        "location_id": loc_id,
        "name": _extract_localized_text(loc.get("name")),
        "web_url": str(loc.get("web_url") or loc.get("url") or tripadvisor_url or ""),
        "rating": ta_rating,
        "reviews_count": ta_reviews_count,
        "ranking": str(ranking).strip(),
        "reviews": reviews,
        "unanswered_recent_count": unanswered_ta,
        "complaint_themes": _detect_complaint_themes(reviews),
        "praise_themes": _detect_praise_themes(reviews),
    }


def _parse_brightdata_payload(text_body: str) -> list[dict[str, Any]]:
    """Supporta sia JSON array sia NDJSON restituito da Bright Data Datasets API."""
    body = (text_body or "").strip()
    if not body:
        return []
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
        return rows
    return []


def fetch_google_maps_brightdata_reviews(
    google_maps_url: str,
    api_key: str = "",
    limit_per_input: int = 25,
) -> dict[str, Any]:
    """Recupera recensioni Google Maps (fino a 25) con stato risposta proprietario via Bright Data."""
    key = (api_key or "").strip() or os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    url = (google_maps_url or "").strip()
    if not key or not url or "google." not in url.lower() and "goo.gl" not in url.lower():
        return {}

    endpoint = (
        "https://api.brightdata.com/datasets/v3/scrape"
        "?dataset_id=gd_luzfs1dn2oa0teb81&notify=false&include_errors=true"
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": [{"url": url}],
        "limit_per_input": limit_per_input,
    }

    rows: list[dict[str, Any]] = []
    try:
        with httpx.Client(verify=certifi.where(), timeout=35.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                rows = _parse_brightdata_payload(resp.text)
            elif resp.status_code == 202:
                snap = resp.json().get("snapshot_id")
                if snap:
                    snap_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snap}?format=json"
                    for _ in range(5):
                        time.sleep(4)
                        r_snap = client.get(snap_url, headers=headers)
                        if r_snap.status_code == 200:
                            rows = _parse_brightdata_payload(r_snap.text)
                            break
                        if r_snap.status_code not in (202, 404):
                            break
            else:
                print(f"[RistoAudit] Bright Data Google Maps HTTP {resp.status_code}: {resp.text[:250]}")
                return {}
    except Exception as exc:
        print(f"[RistoAudit] Errore Bright Data Google Maps API: {exc}")
        return {}

    reviews: list[dict[str, Any]] = []
    place_rating: float | None = None
    place_reviews_count: int | None = None

    for row in rows:
        if row.get("error") or row.get("error_code"):
            continue
        if place_rating is None:
            pr = row.get("overall_rating") or row.get("place_rating") or row.get("rating_overall")
            try:
                if pr is not None:
                    place_rating = float(pr)
            except (TypeError, ValueError):
                pass
        if place_reviews_count is None:
            pc = row.get("reviews_count") or row.get("total_reviews")
            try:
                if pc is not None:
                    place_reviews_count = int(pc)
            except (TypeError, ValueError):
                pass

        r_val = row.get("review_rating") if row.get("review_rating") is not None else row.get("rating")
        try:
            rating_int = int(round(float(r_val))) if r_val is not None else None
        except (TypeError, ValueError):
            rating_int = None

        text = str(row.get("review") or row.get("review_text") or row.get("text") or "").strip()
        if rating_int is None and not text:
            continue

        author = str(
            row.get("reviewer_name") or row.get("author_name") or row.get("user_name") or row.get("author") or "Utente Google"
        ).strip()
        rel_time = str(row.get("review_date") or row.get("date") or row.get("published_at") or "")[:16]
        owner_reply = (
            row.get("owner_response")
            or row.get("response_from_owner_text")
            or row.get("owner_answer")
            or row.get("reply")
        )
        has_reply = bool(str(owner_reply).strip()) if owner_reply else False

        reviews.append({
            "author": author,
            "rating": rating_int,
            "sentiment": _classify_review_sentiment(rating_int),
            "text": " ".join(text.split()),
            "relative_time": rel_time,
            "publish_time": rel_time,
            "has_owner_reply": has_reply,
        })

    if not reviews:
        return {}

    pos_c = sum(1 for r in reviews if r["sentiment"] == "positiva")
    neu_c = sum(1 for r in reviews if r["sentiment"] == "neutra")
    neg_c = sum(1 for r in reviews if r["sentiment"] == "negativa")
    unanswered_total = sum(1 for r in reviews if not r["has_owner_reply"])
    unanswered_neg = sum(1 for r in reviews if (r["rating"] is not None and r["rating"] <= 3) and not r["has_owner_reply"])

    return {
        "brightdata_google_used": True,
        "rating": place_rating,
        "reviews_count": place_reviews_count,
        "reviews": reviews,
        "analyzed_count": len(reviews),
        "positive_reviews_count": pos_c,
        "neutral_reviews_count": neu_c,
        "negative_reviews_count": neg_c,
        "low_rating_reviews_count": neg_c + neu_c,
        "unanswered_count": unanswered_total,
        "unanswered_negative_count": unanswered_neg,
        "complaint_themes": _detect_complaint_themes(reviews),
        "praise_themes": _detect_praise_themes(reviews),
    }


def fetch_facebook_brightdata_reviews(
    facebook_url: str,
    api_key: str = "",
    num_of_reviews: int = 15,
) -> dict[str, Any]:
    """Recupera e classifica le recensioni Facebook tramite Bright Data Datasets API."""
    key = (api_key or "").strip() or os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    base = (facebook_url or "").strip().split("?")[0].rstrip("/")
    if not key or not base or "facebook.com" not in base.lower():
        return {}

    reviews_url = f"{base}/reviews/" if not base.lower().endswith("/reviews") else f"{base}/"
    endpoint = (
        "https://api.brightdata.com/datasets/v3/scrape"
        "?dataset_id=gd_m0dtqpiu1mbcyc2g86&notify=false&include_errors=true"
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": [{"url": reviews_url, "num_of_reviews": num_of_reviews}],
        "limit_per_input": None,
    }

    rows: list[dict[str, Any]] = []
    try:
        with httpx.Client(verify=certifi.where(), timeout=35.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                rows = _parse_brightdata_payload(resp.text)
            elif resp.status_code == 202:
                snap = resp.json().get("snapshot_id")
                if snap:
                    snap_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snap}?format=json"
                    for _ in range(5):
                        time.sleep(4)
                        r_snap = client.get(snap_url, headers=headers)
                        if r_snap.status_code == 200:
                            rows = _parse_brightdata_payload(r_snap.text)
                            break
                        if r_snap.status_code not in (202, 404):
                            break
            else:
                print(f"[RistoAudit] Bright Data Facebook HTTP {resp.status_code}: {resp.text[:250]}")
                return {}
    except Exception as exc:
        print(f"[RistoAudit] Errore Bright Data Facebook API: {exc}")
        return {}

    reviews: list[dict[str, Any]] = []
    sample_keys_logged = False
    for row in rows:
        if row.get("error") or row.get("error_code"):
            continue
        raw_text = (
            row.get("review_content")
            or row.get("review_text")
            or row.get("review")
            or row.get("message")
            or row.get("body")
            or row.get("text")
            or row.get("content")
            or row.get("comment")
            or row.get("description")
            or row.get("post_text")
            or ""
        )
        text = _extract_localized_text(raw_text) if isinstance(raw_text, (list, dict)) else str(raw_text).strip()
        if not text and not sample_keys_logged:
            print(f"[RistoAudit] Bright Data FB row keys (text vuoto): {sorted(row.keys())}")
            sample_keys_logged = True
        rec_raw = row.get("recommends")
        if rec_raw is None:
            rec_raw = row.get("is_recommended")
        if isinstance(rec_raw, str):
            low_r = rec_raw.strip().lower()
            recommends = True if low_r in ("true", "yes", "1", "recommends", "consiglia") else (
                False if low_r in ("false", "no", "0", "does not recommend", "non consiglia") else None
            )
        elif isinstance(rec_raw, bool):
            recommends = rec_raw
        else:
            recommends = None

        if not text and recommends is None:
            continue

        author = str(
            row.get("user_name") or row.get("author_name") or row.get("reviewer_name") or row.get("author") or "Utente Facebook"
        ).strip()
        rev_time = str(
            row.get("review_time") or row.get("date_posted") or row.get("date") or row.get("created_at") or ""
        )[:10]

        sentiment = "positiva" if recommends is True else ("negativa" if recommends is False else "neutra")
        # Mappa a rating fittizio 5/1 solo per riusare i detector tematici IT/EN
        pseudo_rating = 5 if recommends is True else (1 if recommends is False else 3)
        reviews.append({
            "author": author,
            "recommends": recommends,
            "sentiment": sentiment,
            "rating": pseudo_rating,
            "text": " ".join(text.split()),
            "date": rev_time,
        })

    if not reviews:
        return {}

    rec_yes = sum(1 for r in reviews if r["recommends"] is True)
    rec_no = sum(1 for r in reviews if r["recommends"] is False)
    judged = rec_yes + rec_no
    rec_pct = int(round((rec_yes / judged) * 100)) if judged > 0 else None

    parts = [f"{len(reviews)} recensioni analizzate"]
    if rec_pct is not None:
        parts.insert(0, f"Consigliato dal {rec_pct}% ({rec_yes} sì / {rec_no} no)")

    return {
        "reviews_url": reviews_url,
        "brightdata_used": True,
        "reviews": reviews[:10],
        "reviews_count": len(reviews),
        "recommended_count": rec_yes,
        "not_recommended_count": rec_no,
        "recommendation_pct": rec_pct,
        "summary": " · ".join(parts),
        "complaint_themes": _detect_complaint_themes(reviews),
        "praise_themes": _detect_praise_themes(reviews),
    }


def fetch_facebook_public_reviews(facebook_url: str) -> dict[str, Any]:
    """Legge eventuali metadati pubblici di valutazione/recensioni dalla pagina Facebook."""
    base = (facebook_url or "").strip().split("?")[0].rstrip("/")
    if not base or "facebook.com" not in base.lower():
        return {}
    reviews_url = f"{base}/reviews/" if not base.lower().endswith("/reviews") else f"{base}/"
    result: dict[str, Any] = {
        "reviews_url": reviews_url,
        "rating": None,
        "reviews_count": None,
        "recommendation_pct": None,
        "summary": "",
        "public_data_found": False,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    try:
        from bs4 import BeautifulSoup
        with httpx.Client(verify=certifi.where(), follow_redirects=True, timeout=8.0, headers=headers) as client:
            for target in (reviews_url, base):
                r = client.get(target)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text[:350_000], "lxml")
                metas = []
                for attr, val in (("name", "description"), ("property", "og:description"), ("property", "og:title")):
                    m = soup.find("meta", attrs={attr: val})
                    if m and m.get("content"):
                        metas.append(str(m["content"]))
                combined = " | ".join(metas)

                # Es: "4,5 su 5", "4.6 / 5", "4,5 stelle"
                m_rat = re.search(r"(\d[.,]\d)\s*(?:su\s*5|/\s*5|out of 5|stelle|stars)", combined, re.I)
                if m_rat and result["rating"] is None:
                    result["rating"] = float(m_rat.group(1).replace(",", "."))

                # Es: "92% consigliato" o "Consigliato dal 92%" o "92% recommend"
                m_pct = re.search(r"(\d{1,3})\s*%\s*(?:consigliat|recommend)|(?:consigliat[oa]?\s*dal?\s*)(\d{1,3})\s*%", combined, re.I)
                if m_pct and result["recommendation_pct"] is None:
                    pct_val = m_pct.group(1) or m_pct.group(2)
                    if pct_val:
                        result["recommendation_pct"] = int(pct_val)

                # Es: "128 recensioni" / "128 reviews"
                m_cnt = re.search(r"([\d.]+)\s*(?:recensioni|reviews|valutazioni)", combined, re.I)
                if m_cnt and result["reviews_count"] is None:
                    try:
                        result["reviews_count"] = int(m_cnt.group(1).replace(".", ""))
                    except ValueError:
                        pass

                if result["rating"] is not None or result["recommendation_pct"] is not None or result["reviews_count"] is not None:
                    result["public_data_found"] = True
                    parts = []
                    if result["rating"] is not None:
                        parts.append(f"{result['rating']:.1f}/5")
                    if result["recommendation_pct"] is not None:
                        parts.append(f"Consigliato dal {result['recommendation_pct']}%")
                    if result["reviews_count"] is not None:
                        parts.append(f"{result['reviews_count']} recensioni")
                    result["summary"] = " · ".join(parts)
                    break
    except Exception:
        pass
    return result


def run_reputation_and_social_audit(
    audit_input: AuditInput,
    website_data: dict[str, Any],
    osint_data: dict[str, Any],
    gp_data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggrega e valuta lo stato dei canali social e delle recensioni."""
    site_socials = website_data.get("social_links_on_site", {})

    # Mappa profili trovati sul web (sia da search_records diretti sia da username scan)
    web_profiles_by_platform: dict[str, str] = dict(osint_data.get("discovered_social_urls") or {})
    for prof in osint_data.get("discovered_profiles", []):
        plat = str(prof.get("platform") or "").lower()
        purl = str(prof.get("profile_url") or prof.get("url") or "")
        if plat and purl and plat not in web_profiles_by_platform:
            web_profiles_by_platform[plat] = purl

    for scan in osint_data.get("username_scans", []):
        for hit in scan.get("found", []):
            plat = str(hit.get("platform") or "").lower()
            purl = str(hit.get("url") or "")
            for key in ("instagram", "facebook", "tiktok", "youtube", "pinterest", "linkedin", "tripadvisor"):
                if key in plat or key in purl.lower():
                    web_profiles_by_platform.setdefault(key, purl)

    channels: dict[str, dict[str, Any]] = {}
    explicit_map = {
        "instagram": audit_input.instagram_url.strip(),
        "facebook": audit_input.facebook_url.strip(),
        "tiktok": audit_input.tiktok_url.strip(),
    }

    for ch in ("instagram", "facebook", "tiktok", "youtube", "whatsapp"):
        url = explicit_map.get(ch) or site_socials.get(ch) or web_profiles_by_platform.get(ch) or ""
        source = (
            "modulo" if explicit_map.get(ch)
            else ("sito_web" if site_socials.get(ch) else ("ricerca_web" if web_profiles_by_platform.get(ch) else "assente"))
        )
        channels[ch] = {
            "present": bool(url),
            "url": url,
            "source": source,
            "linked_on_website": bool(site_socials.get(ch)),
        }

    days_since_last_post: int | None = None
    if audit_input.last_social_post_date.strip():
        try:
            dt = datetime.strptime(audit_input.last_social_post_date.strip(), "%Y-%m-%d")
            days_since_last_post = max(0, (datetime.now() - dt).days)
        except ValueError:
            pass

    active_core_socials = sum(1 for c in ("instagram", "facebook", "tiktok") if channels[c]["present"])

    social_data: dict[str, Any] = {
        "channels": channels,
        "active_core_count": active_core_socials,
        "last_post_date": audit_input.last_social_post_date.strip(),
        "days_since_last_post": days_since_last_post,
        "social_inactivity_level": (
            "critica" if (days_since_last_post is not None and days_since_last_post > 90)
            else ("alta" if (days_since_last_post is not None and days_since_last_post > 30)
                  else ("attiva" if days_since_last_post is not None else "non_dichiarata"))
        ),
    }

    # --- RECENSIONI & REPUTAZIONE ---
    if gp_data is None:
        gp_data = fetch_google_places_data(
            name=audit_input.restaurant_name,
            city=audit_input.city,
            address=audit_input.address,
            google_maps_url=audit_input.google_maps_url,
            google_place_id=audit_input.google_place_id,
            api_key=audit_input.google_places_api_key,
        )

    google_rating = _parse_float(audit_input.google_rating) or gp_data.get("rating")
    google_reviews_count = _parse_int(audit_input.google_reviews_count) or gp_data.get("reviews_count")
    tripadvisor_rating = _parse_float(audit_input.tripadvisor_rating)
    unanswered_total = _parse_int(audit_input.unanswered_reviews_count)
    if unanswered_total is None:
        unanswered_total = gp_data.get("detected_unanswered_count")
    unanswered_negative = _parse_int(audit_input.unanswered_negative_reviews)

    web_review_urls = [r.get("url", "") for r in osint_data.get("review_portal_hits", [])]
    tripadvisor_url = (
        audit_input.tripadvisor_url.strip()
        or site_socials.get("tripadvisor", "")
        or next((u for u in web_review_urls if "tripadvisor." in u.lower() and "restaurant_review" in u.lower()), "")
    )
    thefork_url = (
        audit_input.thefork_url.strip()
        or site_socials.get("thefork", "")
        or next(
            (u for u in web_review_urls if ("thefork." in u.lower() or "lafourchette." in u.lower())
             and ("/ristorante/" in u.lower() or "/restaurant/" in u.lower())),
            "",
        )
    )
    google_maps_url = (
        audit_input.google_maps_url.strip()
        or gp_data.get("maps_uri", "")
        or next((u for u in web_review_urls if "google.com/maps" in u.lower()), "")
    )

    # Esegue IN PARALLELO Bright Data (Google Maps + Facebook) e TripAdvisor Terra per restare sotto i 30-40s!
    fb_url = channels.get("facebook", {}).get("url", "")
    bd_g_data: dict[str, Any] = {}
    ta_data: dict[str, Any] = {}
    fb_rev_data: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_bd_g = (
            pool.submit(fetch_google_maps_brightdata_reviews, google_maps_url, audit_input.brightdata_api_key)
            if (google_maps_url and audit_input.brightdata_api_key.strip()) else None
        )
        fut_ta = pool.submit(
            fetch_tripadvisor_terra_data,
            audit_input.restaurant_name,
            audit_input.city,
            tripadvisor_url,
            audit_input.tripadvisor_terra_api_key,
        )
        fut_fb = (
            pool.submit(fetch_facebook_brightdata_reviews, fb_url, audit_input.brightdata_api_key)
            if (fb_url and audit_input.brightdata_api_key.strip()) else None
        )

        if fut_bd_g:
            try:
                bd_g_data = fut_bd_g.result(timeout=55)
            except Exception as exc:
                print(f"[RistoAudit] Timeout/errore Bright Data Google: {exc}")
        try:
            ta_data = fut_ta.result(timeout=20)
        except Exception as exc:
            print(f"[RistoAudit] Timeout/errore TripAdvisor Terra: {exc}")
        if fut_fb:
            try:
                fb_rev_data = fut_fb.result(timeout=55)
            except Exception as exc:
                print(f"[RistoAudit] Timeout/errore Bright Data Facebook: {exc}")

    if fb_url and not fb_rev_data:
        fb_rev_data = fetch_facebook_public_reviews(fb_url)

    if bd_g_data:
        if google_rating is None and bd_g_data.get("rating") is not None:
            google_rating = bd_g_data["rating"]
        if google_reviews_count is None and bd_g_data.get("reviews_count") is not None:
            google_reviews_count = bd_g_data["reviews_count"]
        if unanswered_total is None:
            unanswered_total = bd_g_data.get("unanswered_count")
        if unanswered_negative is None:
            unanswered_negative = bd_g_data.get("unanswered_negative_count")

    effective_google_reviews = bd_g_data.get("reviews") or gp_data.get("reviews", [])
    effective_pos = bd_g_data.get("positive_reviews_count") if bd_g_data else gp_data.get("positive_reviews_count", 0)
    effective_neu = bd_g_data.get("neutral_reviews_count") if bd_g_data else gp_data.get("neutral_reviews_count", 0)
    effective_neg = bd_g_data.get("negative_reviews_count") if bd_g_data else gp_data.get("negative_reviews_count", 0)
    effective_low = bd_g_data.get("low_rating_reviews_count") if bd_g_data else gp_data.get("low_rating_reviews_count", 0)

    if ta_data:
        if not tripadvisor_url and ta_data.get("web_url"):
            tripadvisor_url = ta_data["web_url"]
        if tripadvisor_rating is None and ta_data.get("rating") is not None:
            tripadvisor_rating = ta_data["rating"]

    merged_complaints = list(dict.fromkeys(
        (gp_data.get("complaint_themes") or [])
        + (bd_g_data.get("complaint_themes") or [])
        + (ta_data.get("complaint_themes") or [])
        + (fb_rev_data.get("complaint_themes") or [])
    ))
    merged_praises = list(dict.fromkeys(
        (gp_data.get("praise_themes") or [])
        + (bd_g_data.get("praise_themes") or [])
        + (ta_data.get("praise_themes") or [])
        + (fb_rev_data.get("praise_themes") or [])
    ))

    reviews_data: dict[str, Any] = {
        "google_maps_url": google_maps_url,
        "google_place_id": gp_data.get("place_id") or audit_input.google_place_id.strip(),
        "tripadvisor_url": tripadvisor_url,
        "thefork_url": thefork_url,
        "facebook_reviews_url": fb_rev_data.get("reviews_url", ""),
        "facebook_rating": fb_rev_data.get("rating"),
        "facebook_reviews_count": fb_rev_data.get("reviews_count"),
        "facebook_recommendation_pct": fb_rev_data.get("recommendation_pct"),
        "facebook_recommended_count": fb_rev_data.get("recommended_count", 0),
        "facebook_not_recommended_count": fb_rev_data.get("not_recommended_count", 0),
        "facebook_reviews": fb_rev_data.get("reviews", []),
        "facebook_brightdata_used": bool(fb_rev_data.get("brightdata_used")),
        "facebook_reviews_summary": fb_rev_data.get("summary", ""),
        "google_rating": google_rating,
        "google_reviews_count": google_reviews_count,
        "tripadvisor_rating": tripadvisor_rating,
        "tripadvisor_reviews_count": ta_data.get("reviews_count"),
        "tripadvisor_ranking": ta_data.get("ranking", ""),
        "tripadvisor_reviews": ta_data.get("reviews", []),
        "tripadvisor_unanswered_recent": ta_data.get("unanswered_recent_count"),
        "tripadvisor_terra_used": bool(ta_data),
        "unanswered_reviews_count": unanswered_total,
        "unanswered_negative_reviews": unanswered_negative,
        "notes": audit_input.reviews_notes.strip(),
        "google_places_api_used": bool(gp_data),
        "google_brightdata_used": bool(bd_g_data),
        "google_reviews": effective_google_reviews,
        "positive_reviews_count": effective_pos,
        "neutral_reviews_count": effective_neu,
        "negative_reviews_count": effective_neg,
        "low_rating_google_reviews_count": effective_low,
        "complaint_themes": merged_complaints,
        "praise_themes": merged_praises,
        "google_profile": {
            "has_opening_hours": gp_data.get("has_opening_hours"),
            "photos_count": gp_data.get("photos_count"),
            "has_phone": bool(gp_data.get("phone")),
            "phone": gp_data.get("phone", ""),
            "has_website": bool(gp_data.get("website_uri")),
            "website_uri": gp_data.get("website_uri", ""),
            "formatted_address": gp_data.get("formatted_address", ""),
            "has_editorial_summary": gp_data.get("has_editorial_summary"),
            "reservable": gp_data.get("reservable"),
            "business_status": gp_data.get("business_status", ""),
        },
        "osint_portals_found": len(osint_data.get("review_portal_hits", [])),
    }

    return social_data, reviews_data
