"""Recupero e cache JSON/CSV (stile Outscraper, compatibile Excel) dei primi 50 ristoranti per citta' italiana."""

from __future__ import annotations

import csv
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

ITALIAN_CITIES: list[str] = [
    "Agrigento", "Alessandria", "Ancona", "Aosta", "Arezzo", "Ascoli Piceno", "Asti", "Avellino",
    "Bari", "Barletta", "Belluno", "Benevento", "Bergamo", "Biella", "Bologna", "Bolzano",
    "Brescia", "Brindisi", "Cagliari", "Caltanissetta", "Campobasso", "Caserta", "Catania",
    "Catanzaro", "Chieti", "Como", "Cosenza", "Cremona", "Crotone", "Cuneo", "Enna", "Fermo",
    "Ferrara", "Firenze", "Foggia", "Forlì", "Frosinone", "Genova", "Gorizia", "Grosseto",
    "Imperia", "Isernia", "L'Aquila", "La Spezia", "Latina", "Lecce", "Lecco", "Livorno",
    "Lodi", "Lucca", "Macerata", "Mantova", "Massa", "Matera", "Messina", "Milano", "Modena",
    "Monza", "Napoli", "Novara", "Nuoro", "Oristano", "Padova", "Palermo", "Parma", "Pavia",
    "Perugia", "Pesaro", "Pescara", "Piacenza", "Pisa", "Pistoia", "Pordenone", "Potenza",
    "Prato", "Ragusa", "Ravenna", "Reggio Calabria", "Reggio Emilia", "Rieti", "Rimini", "Roma",
    "Rovigo", "Salerno", "Sassari", "Savona", "Siena", "Siracusa", "Sondrio", "Taranto",
    "Teramo", "Terni", "Torino", "Trapani", "Trento", "Treviso", "Trieste", "Udine",
    "Varese", "Venezia", "Verbania", "Vercelli", "Verona", "Vibo Valentia", "Vicenza", "Viterbo",
]

OUTSCRAPER_CSV_COLUMNS: list[str] = [
    "query", "name", "name_for_emails", "subtypes", "category", "type", "phone",
    "website", "address", "street", "city", "state", "postal_code", "country", "country_code",
    "domain", "company_facebook", "company_instagram", "company_x", "company_youtube",
    "tiktok_url", "email", "website_title", "website_description", "website_generator",
    "website_has_gtm", "website_has_fb_pixel", "latitude", "longitude",
    "rating", "reviews", "reviews_link", "business_status", "working_hours",
    "range", "reservation_links", "menu_link", "tripadvisor_url", "thefork_url",
    "about", "description", "location_link", "place_id", "cid",
    # Campi RistoAudit usati per auto-popolamento form
    "restaurant_name", "cuisine_type", "google_maps_url", "google_place_id",
    "website_url", "menu_url", "instagram_url", "facebook_url", "social_handle",
    "google_rating", "google_reviews_count", "reviews_notes",
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _city_slug(city: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", city.lower().strip()).strip("_")
    return slug or "citta"


def _extract_handle_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        path = urllib.parse.urlsplit(url).path.strip("/")
        if not path:
            return ""
        first = path.split("/")[0].lstrip("@").strip()
        if first.lower() in ("p", "reel", "stories", "explore", "pages", "profile.php", "sharer", "share"):
            return ""
        return first
    except Exception:
        return ""


def _extract_cid_from_url(url: str) -> str:
    m = re.search(r"[?&]cid=(\d+)", url or "")
    return m.group(1) if m else ""


def _parse_address_components(components: list[dict[str, Any]]) -> dict[str, str]:
    parsed = {"street": "", "street_num": "", "postal_code": "", "state": "", "country": "", "country_code": ""}
    for comp in components or []:
        types = comp.get("types") or []
        long_t = str(comp.get("longText") or "")
        short_t = str(comp.get("shortText") or "")
        if "route" in types:
            parsed["street"] = long_t
        elif "street_number" in types:
            parsed["street_num"] = long_t
        elif "postal_code" in types:
            parsed["postal_code"] = long_t
        elif "administrative_area_level_2" in types:
            parsed["state"] = long_t
        elif "country" in types:
            parsed["country"] = long_t
            parsed["country_code"] = short_t
    if parsed["street"] and parsed["street_num"]:
        parsed["street"] = f"{parsed['street']}, {parsed['street_num']}"
    return parsed


def _enrich_from_website(item: dict[str, Any]) -> dict[str, Any]:
    """Estrae social, email, meta SEO, GTM/Pixel, menu, prenotazioni, TripAdvisor e TheFork dal sito."""
    site_url = (item.get("website_url") or "").strip()
    if not site_url:
        return item

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    try:
        with httpx.Client(timeout=3.5, follow_redirects=True, verify=False) as client:
            resp = client.get(site_url, headers=headers)
            if resp.status_code >= 400 or not resp.text:
                return item
            html = resp.text[:300_000]
            low_html = html.lower()
            soup = BeautifulSoup(html, "html.parser")
            base_url = str(resp.url)

            if soup.title and soup.title.string:
                item["website_title"] = " ".join(soup.title.string.split())[:200]
            meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            if meta_desc and meta_desc.get("content"):
                item["website_description"] = " ".join(str(meta_desc["content"]).split())[:350]
            meta_gen = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
            if meta_gen and meta_gen.get("content"):
                item["website_generator"] = " ".join(str(meta_gen["content"]).split())[:100]

            item["website_has_gtm"] = 1 if ("googletagmanager.com" in low_html or "gtm-" in low_html) else 0
            item["website_has_fb_pixel"] = 1 if ("connect.facebook.net" in low_html or "fbq(" in low_html) else 0

            reservation_urls: list[str] = []
            for a in soup.find_all("a", href=True):
                href = str(a.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:")):
                    continue
                if href.lower().startswith("mailto:") and not item["email"]:
                    em = href[7:].split("?")[0].strip()
                    if _EMAIL_RE.match(em):
                        item["email"] = em
                    continue
                if href.lower().startswith("tel:"):
                    continue

                abs_url = urllib.parse.urljoin(base_url, href)
                low_href = abs_url.lower()
                link_text = a.get_text(" ", strip=True).lower()

                if "instagram.com/" in low_href and not item["instagram_url"]:
                    if not any(b in low_href for b in ("/p/", "/reel/", "/explore/")):
                        item["instagram_url"] = abs_url
                        item["company_instagram"] = abs_url
                elif ("facebook.com/" in low_href or "fb.com/" in low_href) and not item["facebook_url"]:
                    if not any(b in low_href for b in ("/sharer", "/share", "/posts/", "/photos/")):
                        item["facebook_url"] = abs_url
                        item["company_facebook"] = abs_url
                elif ("twitter.com/" in low_href or "x.com/" in low_href) and not item["company_x"]:
                    if "/intent/" not in low_href and "/share" not in low_href:
                        item["company_x"] = abs_url
                elif ("youtube.com/" in low_href or "youtu.be/" in low_href) and not item["company_youtube"]:
                    item["company_youtube"] = abs_url
                elif "tiktok.com/" in low_href and not item["tiktok_url"]:
                    item["tiktok_url"] = abs_url
                elif "tripadvisor." in low_href and "restaurant_review" in low_href and not item["tripadvisor_url"]:
                    item["tripadvisor_url"] = abs_url
                elif ("thefork." in low_href or "lafourchette." in low_href) and (
                    "/ristorante/" in low_href or "/restaurant/" in low_href
                ):
                    if not item["thefork_url"]:
                        item["thefork_url"] = abs_url
                    reservation_urls.append(abs_url)
                elif any(b in low_href for b in ("plateform.app", "quandoo.", "resengo.", "opentable.", "tablein.", "prenota")):
                    reservation_urls.append(abs_url)
                elif not item["menu_url"]:
                    if (
                        "menu" in low_href or "menù" in low_href or "carta" in low_href
                        or "menu" in link_text or "menù" in link_text or "la nostra carta" in link_text
                    ) and urllib.parse.urlsplit(abs_url).netloc == urllib.parse.urlsplit(base_url).netloc:
                        item["menu_url"] = abs_url
                        item["menu_link"] = abs_url

            if not item["email"]:
                for m in _EMAIL_RE.findall(soup.get_text(" ", strip=True)):
                    if not any(bad in m.lower() for bad in ("example.", "wixpress.", "sentry.", ".png", ".jpg")):
                        item["email"] = m
                        break

            if reservation_urls:
                item["reservation_links"] = ", ".join(dict.fromkeys(reservation_urls[:3]))

            if not item["social_handle"]:
                item["social_handle"] = (
                    _extract_handle_from_url(item["instagram_url"])
                    or _extract_handle_from_url(item["facebook_url"])
                    or _extract_handle_from_url(item["tiktok_url"])
                )
    except Exception:
        pass
    return item


def _save_csv_export(csv_path: Path, restaurants: list[dict[str, Any]]) -> None:
    """Salva i ristoranti in formato CSV UTF-8-SIG (compatibile con Excel) con colonne Outscraper + RistoAudit."""
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTSCRAPER_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in restaurants:
            row = dict(r)
            if isinstance(row.get("working_hours"), (dict, list)):
                row["working_hours"] = json.dumps(row["working_hours"], ensure_ascii=False)
            if isinstance(row.get("about"), (dict, list)):
                row["about"] = json.dumps(row["about"], ensure_ascii=False)
            writer.writerow(row)


def get_city_restaurants(
    city: str,
    google_places_api_key: str,
    force_refresh: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Restituisce i primi 50 ristoranti di una citta', salvando sia JSON sia CSV (Excel) in locale."""
    city_clean = city.strip()
    if not city_clean:
        return {"city": "", "restaurants": [], "error": "Città non specificata."}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slug = _city_slug(city_clean)
    cache_file = DATA_DIR / f"restaurants_{slug}.json"
    csv_file = DATA_DIR / f"restaurants_{slug}.csv"

    if cache_file.exists() and not force_refresh:
        try:
            cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached_data, dict) and isinstance(cached_data.get("restaurants"), list):
                revs = cached_data["restaurants"]
                # Se il CSV non esiste ancora o mancavano i campi Outscraper, rigenera il CSV se possibile
                if revs and not csv_file.exists():
                    _save_csv_export(csv_file, revs)
                return {
                    "city": cached_data.get("city", city_clean),
                    "cached": True,
                    "count": len(revs),
                    "file": cache_file.name,
                    "csv_file": csv_file.name,
                    "restaurants": revs,
                }
        except Exception:
            pass

    key = (google_places_api_key or "").strip()
    if not key:
        return {
            "city": city_clean,
            "restaurants": [],
            "error": "Google Places API Key non configurata in .ristoaudit_settings.json.",
        }

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,places.addressComponents,"
            "places.nationalPhoneNumber,places.rating,places.userRatingCount,"
            "places.googleMapsUri,places.websiteUri,places.primaryTypeDisplayName,places.types,"
            "places.location,places.businessStatus,places.regularOpeningHours,places.priceLevel,"
            "places.editorialSummary,places.reservable,places.dineIn,places.takeout,places.delivery,"
            "nextPageToken"
        ),
    }

    restaurants: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_token: str | None = None
    pages = 0
    query_str = f"ristoranti a {city_clean}"

    try:
        with httpx.Client(timeout=15.0) as client:
            while len(restaurants) < limit and pages < 4:
                body: dict[str, Any] = {
                    "textQuery": query_str,
                    "includedType": "restaurant",
                    "languageCode": "it",
                    "pageSize": 20,
                }
                if page_token:
                    body["pageToken"] = page_token
                resp = client.post(url, headers=headers, json=body)
                pages += 1
                if resp.status_code != 200:
                    print(f"[RistoAudit] Places searchText HTTP {resp.status_code}: {resp.text[:250]}")
                    break

                data = resp.json()
                for p in data.get("places", []):
                    if len(restaurants) >= limit:
                        break
                    pid = str(p.get("id") or "").strip()
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                    name = str((p.get("displayName") or {}).get("text") or "").strip()
                    if not name:
                        continue

                    maps_uri = str(p.get("googleMapsUri") or "").strip()
                    if not maps_uri:
                        maps_uri = f"https://www.google.com/maps/place/?q=place_id:{pid}"

                    site_uri = str(p.get("websiteUri") or "").strip()
                    domain = urllib.parse.urlsplit(site_uri).netloc.removeprefix("www.") if site_uri else ""
                    addr_parts = _parse_address_components(p.get("addressComponents") or [])
                    loc = p.get("location") or {}
                    cat = str((p.get("primaryTypeDisplayName") or {}).get("text") or "").strip()
                    subtypes = ", ".join(p.get("types") or [])
                    hours_desc = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
                    about_obj = {
                        "reservable": p.get("reservable"),
                        "dine_in": p.get("dineIn"),
                        "takeout": p.get("takeout"),
                        "delivery": p.get("delivery"),
                    }

                    restaurants.append({
                        # Schema Outscraper
                        "query": query_str,
                        "name": name,
                        "name_for_emails": name,
                        "subtypes": subtypes,
                        "category": cat,
                        "type": cat,
                        "phone": str(p.get("nationalPhoneNumber") or "").strip(),
                        "website": site_uri,
                        "address": str(p.get("formattedAddress") or "").strip(),
                        "street": addr_parts["street"],
                        "city": city_clean,
                        "state": addr_parts["state"],
                        "postal_code": addr_parts["postal_code"],
                        "country": addr_parts["country"] or "Italy",
                        "country_code": addr_parts["country_code"] or "IT",
                        "domain": domain,
                        "company_facebook": "",
                        "company_instagram": "",
                        "company_x": "",
                        "company_youtube": "",
                        "tiktok_url": "",
                        "email": "",
                        "website_title": "",
                        "website_description": "",
                        "website_generator": "",
                        "website_has_gtm": 0,
                        "website_has_fb_pixel": 0,
                        "latitude": loc.get("latitude"),
                        "longitude": loc.get("longitude"),
                        "rating": p.get("rating"),
                        "reviews": p.get("userRatingCount"),
                        "reviews_link": f"https://search.google.com/local/reviews?placeid={pid}&hl=it&gl=IT",
                        "business_status": str(p.get("businessStatus") or ""),
                        "working_hours": " | ".join(hours_desc) if hours_desc else "",
                        "range": str(p.get("priceLevel") or ""),
                        "reservation_links": "",
                        "menu_link": "",
                        "tripadvisor_url": "",
                        "thefork_url": "",
                        "about": about_obj,
                        "description": str((p.get("editorialSummary") or {}).get("text") or "").strip(),
                        "location_link": maps_uri,
                        "place_id": pid,
                        "cid": _extract_cid_from_url(maps_uri),
                        # Campi RistoAudit per auto-popolamento form
                        "restaurant_name": name,
                        "cuisine_type": cat,
                        "google_maps_url": maps_uri,
                        "google_place_id": pid,
                        "website_url": site_uri,
                        "menu_url": "",
                        "instagram_url": "",
                        "facebook_url": "",
                        "social_handle": "",
                        "google_rating": p.get("rating"),
                        "google_reviews_count": p.get("userRatingCount"),
                        "reviews_notes": "",
                    })

                page_token = data.get("nextPageToken")
                if not page_token:
                    break
    except Exception as exc:
        print(f"[RistoAudit] Errore recupero ristoranti per {city_clean}: {exc}")
        return {"city": city_clean, "restaurants": [], "error": str(exc)}

    if restaurants:
        with ThreadPoolExecutor(max_workers=20) as pool:
            restaurants = list(pool.map(_enrich_from_website, restaurants))

        # Preserva eventuali campi compilati manualmente nel file JSON precedente
        if cache_file.exists():
            try:
                old_data = json.loads(cache_file.read_text(encoding="utf-8"))
                old_by_pid = {
                    str(r.get("google_place_id") or r.get("place_id") or ""): r
                    for r in (old_data.get("restaurants") or [])
                    if isinstance(r, dict)
                }
                manual_keys = (
                    "instagram_url", "company_instagram", "facebook_url", "company_facebook",
                    "tiktok_url", "social_handle", "website_url", "website", "menu_url",
                    "menu_link", "tripadvisor_url", "thefork_url", "cuisine_type", "category",
                    "type", "reviews_notes", "google_maps_url", "location_link", "email",
                )
                for r in restaurants:
                    pid_k = str(r.get("google_place_id") or r.get("place_id") or "")
                    old_r = old_by_pid.get(pid_k)
                    if old_r:
                        for mk in manual_keys:
                            if not r.get(mk) and old_r.get(mk):
                                r[mk] = old_r[mk]
            except Exception:
                pass

        payload = {
            "city": city_clean,
            "count": len(restaurants),
            "restaurants": restaurants,
        }
        try:
            cache_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            _save_csv_export(csv_file, restaurants)
        except Exception as exc:
            print(f"[RistoAudit] Errore salvataggio locale {cache_file}/{csv_file}: {exc}")

    return {
        "city": city_clean,
        "cached": False,
        "count": len(restaurants),
        "file": cache_file.name,
        "csv_file": csv_file.name,
        "restaurants": restaurants,
    }


def _apply_updates_to_row(row: dict[str, Any], updates: dict[str, Any], overwrite_empty: bool = False) -> None:
    """Aggiorna i campi RistoAudit e i corrispondenti campi Outscraper su una riga ristorante."""
    field_pairs = [
        ("restaurant_name", ("name", "name_for_emails")),
        ("cuisine_type", ("category", "type")),
        ("google_maps_url", ("location_link",)),
        ("website_url", ("website",)),
        ("menu_url", ("menu_link",)),
        ("instagram_url", ("company_instagram",)),
        ("facebook_url", ("company_facebook",)),
        ("tiktok_url", ()),
        ("social_handle", ()),
        ("tripadvisor_url", ()),
        ("thefork_url", ()),
        ("reviews_notes", ()),
        ("address", ()),
    ]
    for primary_key, mirror_keys in field_pairs:
        if primary_key not in updates:
            continue
        raw_val = updates.get(primary_key)
        val = str(raw_val).strip() if raw_val is not None else ""
        if not val and not overwrite_empty:
            continue
        row[primary_key] = val
        for mk in mirror_keys:
            row[mk] = val
        if primary_key == "website_url":
            row["domain"] = urllib.parse.urlsplit(val).netloc.removeprefix("www.") if val else ""


def update_restaurant_in_city_cache(
    city: str,
    updates: dict[str, Any],
    overwrite_empty: bool = False,
) -> dict[str, Any]:
    """Salva i campi compilati manualmente dall'operatore nel file JSON e CSV della citta'."""
    city_clean = (city or "").strip()
    if not city_clean:
        return {"updated": False, "reason": "missing_city"}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slug = _city_slug(city_clean)
    cache_file = DATA_DIR / f"restaurants_{slug}.json"
    csv_file = DATA_DIR / f"restaurants_{slug}.csv"

    if not cache_file.exists():
        return {"updated": False, "reason": "city_file_not_found"}

    try:
        cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
        restaurants = cached_data.get("restaurants")
        if not isinstance(restaurants, list):
            return {"updated": False, "reason": "invalid_cache"}
    except Exception as exc:
        return {"updated": False, "reason": str(exc)}

    target_pid = str(updates.get("google_place_id") or updates.get("place_id") or "").strip()
    target_name = str(updates.get("restaurant_name") or updates.get("name") or "").strip().lower()

    matched_row: dict[str, Any] | None = None
    for r in restaurants:
        r_pid = str(r.get("google_place_id") or r.get("place_id") or "").strip()
        r_name = str(r.get("restaurant_name") or r.get("name") or "").strip().lower()
        if target_pid and r_pid == target_pid:
            matched_row = r
            break
        if target_name and r_name == target_name:
            matched_row = r
            break

    if matched_row is None:
        return {"updated": False, "reason": "restaurant_not_found"}

    _apply_updates_to_row(matched_row, updates, overwrite_empty=overwrite_empty)

    payload = {
        "city": cached_data.get("city", city_clean),
        "count": len(restaurants),
        "restaurants": restaurants,
    }
    cache_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _save_csv_export(csv_file, restaurants)

    return {
        "updated": True,
        "file": cache_file.name,
        "csv_file": csv_file.name,
        "restaurant": matched_row,
    }
