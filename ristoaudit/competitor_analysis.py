"""Modulo per il calcolo dell'isocrona (OpenRouteService), benchmarking competitor di zona
e stima della Quota di Mercato Potenziale (QM).
"""

from __future__ import annotations

import math
from typing import Any
import httpx


def fetch_ors_isochrone(
    lat: float,
    lng: float,
    api_key: str,
    minutes: int = 10,
    profile: str = "driving-car",
) -> dict[str, Any]:
    """Richiede l'isocrona a OpenRouteService per il profilo specificato (driving-car)."""
    if not api_key:
        return {}
    url = f"https://api.openrouteservice.org/v2/isochrones/{profile}"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    seconds = minutes * 60
    body = {
        "locations": [[lng, lat]],
        "range": [seconds],
        "range_type": "time",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, headers=headers, json=body)
            if resp.status_code == 200:
                data = resp.json()
                bbox = data.get("bbox", [])
                features = data.get("features", [])
                area_sq_km = None
                reach_radius_km = 3.5  # fallback raggio tipico 10 min auto
                if bbox and len(bbox) >= 4:
                    # Calcola raggio approssimato dal bounding box
                    d_lat = abs(bbox[3] - bbox[1]) * 111.0
                    d_lng = abs(bbox[2] - bbox[0]) * 111.0 * math.cos(math.radians(lat))
                    reach_radius_km = round(max(d_lat, d_lng) / 2.0, 1)
                    area_sq_km = round(math.pi * (reach_radius_km ** 2), 1)

                return {
                    "isochrone_minutes": minutes,
                    "profile": profile,
                    "bbox": bbox,
                    "reach_radius_km": reach_radius_km,
                    "area_sq_km": area_sq_km,
                    "has_polygon": bool(features),
                }
            else:
                print(f"[RistoAudit] ORS Isochrone HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[RistoAudit] Errore OpenRouteService: {exc}")
    return {}


def fetch_nearby_competitors(
    restaurant_name: str,
    city: str,
    cuisine_type: str,
    lat: float,
    lng: float,
    google_places_api_key: str,
    radius_meters: int = 1500,
    max_competitors: int = 5,
) -> list[dict[str, Any]]:
    """Cerca i competitor diretti di zona nell'isocrona tramite Google Places API."""
    if not google_places_api_key or not lat or not lng:
        return []

    query = cuisine_type.strip() if cuisine_type.strip() else "ristorante osteria trattoria"
    if city:
        query += f" {city}"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": google_places_api_key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,places.rating,"
            "places.userRatingCount,places.priceLevel,places.primaryTypeDisplayName,places.location"
        ),
    }
    body = {
        "textQuery": query,
        "languageCode": "it",
        "maxResultCount": 10,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_meters),
            }
        },
    }

    competitors: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post("https://places.googleapis.com/v1/places:searchText", headers=headers, json=body)
            if resp.status_code == 200:
                raw_places = resp.json().get("places", [])
                clean_target = restaurant_name.lower().strip()
                for p in raw_places:
                    name = (p.get("displayName") or {}).get("text", "")
                    if not name or name.lower().strip() == clean_target:
                        continue
                    # Se il nome è quasi identico, salta
                    if clean_target in name.lower() or name.lower() in clean_target:
                        continue

                    rating = p.get("rating")
                    reviews_cnt = p.get("userRatingCount") or 0
                    price_level = p.get("priceLevel", "")
                    addr = p.get("formattedAddress", "")

                    # Stima capienza e coperti giornalieri
                    # In base alle recensioni e livello prezzo: un locale con 2000+ recensioni gira su 80-120 pax/giorno
                    if reviews_cnt > 3000:
                        est_daily_pax = 110
                    elif reviews_cnt > 1500:
                        est_daily_pax = 85
                    elif reviews_cnt > 500:
                        est_daily_pax = 65
                    else:
                        est_daily_pax = 45

                    monthly_pax = est_daily_pax * 30

                    competitors.append({
                        "name": name,
                        "rating": rating,
                        "reviews_count": reviews_cnt,
                        "price_level": price_level,
                        "address": addr,
                        "est_daily_pax": est_daily_pax,
                        "est_monthly_pax": monthly_pax,
                    })
                    if len(competitors) >= max_competitors:
                        break
    except Exception as exc:
        print(f"[RistoAudit] Errore ricerca competitor Places: {exc}")

    return competitors


def calculate_market_share(
    isochrone_info: dict[str, Any],
    competitors: list[dict[str, Any]],
    target_restaurant_name: str = "",
    target_reviews_count: int | None = None,
    estimated_population: int = 50000,
) -> dict[str, Any]:
    """Calcola la quota di mercato potenziale (QM) dei competitor e lo spazio libero nell'isocrona.

    Formula utente:
    QM = (stima pax mensili / clienti in target) * 100
    """
    # Popolazione e clienti target (di default ~18% della popolazione dell'isocrona rappresenta il mercato target)
    target_clients = int(round(estimated_population * 0.18))
    if target_clients <= 0:
        target_clients = 9000

    competitors_qm: list[dict[str, Any]] = []
    total_competitor_monthly_pax = 0

    for c in competitors:
        m_pax = c["est_monthly_pax"]
        total_competitor_monthly_pax += m_pax
        qm_pct = round((m_pax / target_clients) * 100, 1)
        competitors_qm.append({
            "name": c["name"],
            "rating": c["rating"],
            "reviews_count": c["reviews_count"],
            "est_daily_pax": c["est_daily_pax"],
            "est_monthly_pax": m_pax,
            "market_share_pct": qm_pct,
        })

    # Stima target restaurant
    target_daily = 70
    if target_reviews_count:
        if target_reviews_count > 3000:
            target_daily = 100
        elif target_reviews_count > 1000:
            target_daily = 75
        elif target_reviews_count > 300:
            target_daily = 55
        else:
            target_daily = 40

    target_monthly_pax = target_daily * 30
    target_qm_pct = round((target_monthly_pax / target_clients) * 100, 1)

    total_occupied_qm = sum(c["market_share_pct"] for c in competitors_qm)
    available_qm = max(0.0, round(100.0 - total_occupied_qm, 1))

    return {
        "isochrone_minutes": isochrone_info.get("isochrone_minutes", 10),
        "reach_radius_km": isochrone_info.get("reach_radius_km", 3.5),
        "area_sq_km": isochrone_info.get("area_sq_km"),
        "estimated_population": estimated_population,
        "target_clients": target_clients,
        "competitors": competitors_qm,
        "total_competitor_monthly_pax": total_competitor_monthly_pax,
        "total_occupied_market_share_pct": min(100.0, total_occupied_qm),
        "available_market_share_pct": available_qm,
        "target_restaurant": {
            "name": target_restaurant_name,
            "est_daily_pax": target_daily,
            "est_monthly_pax": target_monthly_pax,
            "potential_market_share_pct": target_qm_pct,
        },
    }

