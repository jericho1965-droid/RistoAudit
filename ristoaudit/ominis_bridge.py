"""Integrazione diretta e approfondita con i moduli Python di AnonCatalyst/Ominis-OSINT.

Utilizza:
- ominis_src.search (ricerca multi-motore DuckDuckGo + Bing)
- ominis_src.username (enumerazione profili social asincrona)
- ominis_src.platforms (riconoscimento piattaforme e handle dagli URL)
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import types
import urllib.parse
from pathlib import Path
from typing import Any

import certifi
import httpx

VENDOR_OMINIS = Path(__file__).resolve().parent.parent / "vendor" / "Ominis-OSINT"

RESTAURANT_USERNAME_TEMPLATES = [
    "https://www.instagram.com/",
    "https://www.facebook.com/",
    "https://www.tiktok.com/@",
    "https://www.youtube.com/@",
    "https://x.com/",
    "https://www.pinterest.com/",
    "https://www.linkedin.com/company/",
    "https://linktr.ee/",
]

# Domini aggregatori/social/directory da escludere quando si cerca il sito web ufficiale
AGGREGATOR_DOMAINS = (
    "tripadvisor.", "thefork.", "lafourchette.", "yelp.", "trustpilot.",
    "facebook.com", "m.facebook.com", "instagram.com", "tiktok.com",
    "youtube.com", "youtu.be", "twitter.com", "x.com", "linkedin.com",
    "pinterest.", "google.", "goo.gl", "maps.app.goo.gl", "apple.com",
    "paginegialle.", "paginebianche.", "tuttocitta.", "virgilio.",
    "sluurpy.", "restaurantguru.", "quandoo.", "justeat.", "deliveroo.",
    "glovoapp.", "ubereats.", "foursquare.", "michelin.", "gamberorosso.",
    "wikipedia.", "booking.com", "expedia.", "groupon.", "cylex.",
    "infobel.", "kompass.", "prontoimprese.", "misterimprese.",
    "italiaonline.", "trovaprezzi.", "reddit.com", "quora.com",
    "bing.com", "duckduckgo.com", "yahoo.com", "amazon.",
)

GENERIC_STOPWORDS = {
    "ristorante", "pizzeria", "trattoria", "osteria", "locanda", "enoteca",
    "bar", "bistrot", "bistro", "pub", "cucina", "pesce", "carne", "grill",
    "da", "di", "del", "della", "dei", "degli", "delle", "al", "alla", "il", "la", "le", "lo",
}


def _patch_httpx_http2_if_needed() -> None:
    """Se il pacchetto opzionale 'h2' non e' installato, degrada http2=True a False.

    Ominis-OSINT (tools_handler.py) passa http2=True a httpx.AsyncClient; senza 'h2'
    ogni ricerca DuckDuckGo/Bing fallirebbe con ImportError.
    """
    try:
        import h2  # noqa: F401
        return
    except ImportError:
        pass

    if getattr(httpx.AsyncClient, "_ristoaudit_http2_patched", False):
        return

    orig_init = httpx.AsyncClient.__init__

    def _safe_init(self, *args, **kwargs):
        if kwargs.get("http2"):
            kwargs["http2"] = False
        return orig_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = _safe_init  # type: ignore[method-assign]
    httpx.AsyncClient._ristoaudit_http2_patched = True  # type: ignore[attr-defined]


def _ensure_ominis_loaded() -> bool:
    """Configura sys.path, certifi SSL, fallback http2 e dipendenze CLI leggere."""
    if not VENDOR_OMINIS.exists():
        return False

    if not os.environ.get("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = certifi.where()

    _patch_httpx_http2_if_needed()

    vendor_str = str(VENDOR_OMINIS)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)

    if "validators" not in sys.modules:
        try:
            import validators  # noqa: F401
        except ImportError:
            shim = types.ModuleType("validators")

            def _url(val: Any) -> bool:
                if not isinstance(val, str) or not val.strip():
                    return False
                parsed = urllib.parse.urlsplit(val.strip())
                return parsed.scheme in ("http", "https") and bool(parsed.netloc)

            shim.url = _url  # type: ignore[attr-defined]
            sys.modules["validators"] = shim

    if "colorama" not in sys.modules:
        try:
            import colorama  # noqa: F401
        except ImportError:
            c_shim = types.ModuleType("colorama")

            class _DummyColors:
                def __getattr__(self, _name: str) -> str:
                    return ""

            c_shim.Fore = _DummyColors()  # type: ignore[attr-defined]
            c_shim.Style = _DummyColors()  # type: ignore[attr-defined]
            c_shim.init = lambda *a, **kw: None  # type: ignore[attr-defined]
            sys.modules["colorama"] = c_shim

    return True


def _core_tokens(restaurant_name: str) -> list[str]:
    words = re.findall(r"[a-zA-ZàèéìòùÀÈÉÌÒÙ0-9]+", (restaurant_name or "").lower())
    core = [w for w in words if w not in GENERIC_STOPWORDS and len(w) >= 2]
    return core or words


def _core_name(restaurant_name: str) -> str:
    tokens = _core_tokens(restaurant_name)
    return " ".join(tokens) if tokens else restaurant_name.strip()


def derive_candidate_handles(restaurant_name: str, explicit_handle: str = "") -> list[str]:
    """Genera 1-2 handle plausibili per la verifica profili social."""
    candidates: list[str] = []
    if explicit_handle:
        clean = re.sub(r"[^A-Za-z0-9._-]", "", explicit_handle.strip().lstrip("@"))
        if clean:
            candidates.append(clean)

    core_slug = "".join(_core_tokens(restaurant_name))
    if core_slug and len(core_slug) >= 3 and core_slug not in [c.lower() for c in candidates]:
        candidates.append(core_slug)

    full_slug = re.sub(r"[^a-zA-Z0-9]+", "", restaurant_name.lower()).strip()
    if full_slug and len(full_slug) >= 3 and full_slug not in [c.lower() for c in candidates]:
        candidates.append(full_slug)

    return candidates[:2]


def _matches_restaurant(url: str, title: str, snippet: str, restaurant_name: str) -> bool:
    """Verifica rigorosamente che un risultato web appartenga davvero al ristorante cercato."""
    core = _core_tokens(restaurant_name)
    if not core:
        return True
    haystack = f"{urllib.parse.unquote(url)} {title}".lower()
    needed = len(core) if len(core) <= 2 else max(2, len(core) - 1)
    matched = sum(1 for tok in core if tok in haystack)
    if matched >= needed:
        return True
    # Fallback su snippet solo se almeno 1 token e' anche nel titolo/url
    if matched >= 1 and snippet:
        full_hay = f"{haystack} {snippet.lower()}"
        if sum(1 for tok in core if tok in full_hay) >= needed:
            return True
    return False


def _is_valid_portal_profile(url: str, portal: str) -> bool:
    """Verifica che l'URL sia una vera scheda ristorante sul portale (non homepage o liste citta')."""
    parsed = urllib.parse.urlsplit(url)
    path = (parsed.path or "").strip()
    low_path = path.lower()
    if not path or path == "/":
        return False
    if portal == "tripadvisor":
        return "restaurant_review" in low_path
    if portal == "thefork":
        # Schede reali TheFork: /ristorante/<nome>-r<id> oppure /restaurant/<nome>-r<id>
        return ("/ristorante/" in low_path or "/restaurant/" in low_path) and "/ristoranti/" not in low_path
    if portal == "instagram":
        bad = ("/p/", "/reel/", "/explore/", "/accounts/", "/tags/", "/stories/")
        return not any(b in low_path for b in bad) and len(path.strip("/").split("/")) == 1
    if portal == "facebook":
        bad = ("/posts/", "/photos/", "/videos/", "/events/", "/login", "/sharer", "/share", "/search", "/groups/")
        return not any(b in low_path for b in bad)
    if portal == "tiktok":
        return "/@" in low_path and "/video/" not in low_path
    if portal == "youtube":
        bad = ("/watch", "/results", "/shorts/")
        return not any(b in low_path for b in bad)
    return True


def _pick_official_website(records: list[dict[str, Any]], restaurant_name: str, city: str) -> str:
    """Identifica con alta precisione il sito web ufficiale del ristorante tra i risultati OSINT."""
    core = _core_tokens(restaurant_name)
    city_low = (city or "").strip().lower()
    best_url = ""
    best_score = 0

    for rec in records:
        url = str(rec.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if not host or any(agg in host for agg in AGGREGATOR_DOMAINS):
            continue
        # Evita file PDF o sottopagine profonde come homepage, ma conserva l'origine del dominio
        title_snip = f"{rec.get('title', '')} {rec.get('snippet', '')}".lower()

        score = 0
        matched_tokens = sum(1 for t in core if t in host)
        if matched_tokens > 0:
            score += 40 + matched_tokens * 25
        title_matches = sum(1 for t in core if t in title_snip)
        if title_matches > 0:
            score += title_matches * 12
        if city_low and (city_low in host or city_low in title_snip):
            score += 15
        if host.endswith(".it"):
            score += 8
        if any(k in title_snip for k in ("ristorante", "pizzeria", "trattoria", "osteria", "menù", "menu", "prenota")):
            score += 10
        # Se il dominio non contiene alcun token del nome e il titolo ha meno di 2 match, scarta
        if matched_tokens == 0 and title_matches < len(core):
            continue

        if score > best_score:
            best_score = score
            # Normalizza alla root del sito se e' una pagina interna (es. /menu -> root)
            best_url = f"{parsed.scheme}://{parsed.netloc}/"

    return best_url if best_score >= 45 else ""


async def _run_search_query(query: str, max_results: int = 12, scan_pages: bool = False) -> dict[str, Any]:
    from ominis_src import config as ominis_config
    from ominis_src import search as ominis_search

    settings = ominis_config.Settings(
        query=query,
        engines=("duckduckgo", "bing"),
        language="it",
        country="it",
        max_results=max_results,
        scan_pages=scan_pages,
        scan_limit=min(4, max_results),
        timeout=12.0,
    )
    report = await ominis_search.run(settings)
    return {
        "query": query,
        "records": [
            {
                "title": r.title,
                "url": r.url,
                "engine": r.engine,
                "snippet": r.snippet,
                "platform": r.platform,
                "category": r.category,
                "handle": r.handle,
                "status": r.status,
            }
            for r in report.records
        ],
        "profiles": report.profiles,
        "mentions": report.mentions,
        "errors": report.errors,
    }


async def _run_username_scan(handle: str) -> dict[str, Any]:
    from ominis_src import username as ominis_username

    outcome = await ominis_username.run(
        handle,
        platforms=RESTAURANT_USERNAME_TEMPLATES,
        concurrency=6,
        timeout=10.0,
    )
    return {
        "handle": handle,
        "checked": len(outcome.hits),
        "found_count": len(outcome.found),
        "found": [
            {
                "platform": h.platform or urllib.parse.urlsplit(h.url).netloc,
                "url": h.url,
                "status": h.status,
                "title": h.title,
                "description": h.description,
                "match": h.match,
                "confidence": h.confidence,
            }
            for h in outcome.found
        ],
        "all_checked": [
            {
                "platform": h.platform or urllib.parse.urlsplit(h.url).netloc,
                "url": h.url,
                "status": h.status,
                "found": h.found,
                "error": h.error,
            }
            for h in outcome.hits
        ],
    }


def run_ominis_recon(
    restaurant_name: str,
    city: str,
    social_handle: str = "",
    run_search: bool = True,
    run_username: bool = True,
) -> dict[str, Any]:
    """Esegue una ricognizione OSINT approfondita con Ominis-OSINT (sito, menu, social, recensioni)."""
    result: dict[str, Any] = {
        "ominis_available": False,
        "queries_executed": [],
        "search_records": [],
        "discovered_website_url": "",
        "discovered_profiles": [],
        "discovered_social_urls": {},
        "review_portal_hits": [],
        "menu_osint_hits": [],
        "username_scans": [],
        "errors": [],
    }

    if not _ensure_ominis_loaded():
        result["errors"].append("Modulo Ominis-OSINT non trovato in vendor/Ominis-OSINT.")
        return result

    result["ominis_available"] = True
    core = _core_name(restaurant_name)

    async def _orchestrate() -> None:
        tasks = []
        if run_search:
            q_site = f"{core} {city} ristorante"
            q_menu = f"{core} {city} menu"
            q_reviews = f"{core} {city} tripadvisor OR thefork OR recensioni"
            q_social = f"{core} {city} instagram OR facebook"
            result["queries_executed"] = [q_site, q_menu, q_reviews, q_social]
            tasks.append(("search_site", _run_search_query(q_site, max_results=12, scan_pages=False)))
            tasks.append(("search_menu", _run_search_query(q_menu, max_results=10, scan_pages=False)))
            tasks.append(("search_reviews", _run_search_query(q_reviews, max_results=10, scan_pages=False)))
            tasks.append(("search_social", _run_search_query(q_social, max_results=10, scan_pages=False)))

        if run_username:
            handles = derive_candidate_handles(restaurant_name, social_handle)
            for h in handles[:1]:
                tasks.append((f"username:{h}", _run_username_scan(h)))

        if not tasks:
            return

        gathered = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)

        seen_urls: set[str] = set()
        seen_profiles: set[str] = set()
        review_domains = (
            "tripadvisor.", "thefork.", "yelp.", "trustpilot.",
            "google.com/maps", "sluurpy.", "restaurantguru.", "quandoo.",
        )

        for (label, _), payload in zip(tasks, gathered):
            if isinstance(payload, Exception):
                result["errors"].append(f"{label}: {payload}")
                continue

            if label.startswith("search_"):
                for err in payload.get("errors", []):
                    result["errors"].append(f"{label}: {err}")

                for rec in payload.get("records", []):
                    url = rec.get("url", "")
                    title = rec.get("title") or ""
                    snippet = rec.get("snippet") or ""
                    if not url or url in seen_urls:
                        continue
                    # Scarta subito pagine generiche che non riguardano davvero questo ristorante
                    if not _matches_restaurant(url, title, snippet, restaurant_name):
                        continue
                    seen_urls.add(url)
                    result["search_records"].append(rec)

                    low_url = url.lower()
                    low_title = title.lower()
                    if any(dom in low_url for dom in review_domains):
                        # Per TripAdvisor e TheFork accetta solo schede ristorante reali
                        if "tripadvisor." in low_url and not _is_valid_portal_profile(url, "tripadvisor"):
                            pass
                        elif ("thefork." in low_url or "lafourchette." in low_url) and not _is_valid_portal_profile(url, "thefork"):
                            pass
                        else:
                            result["review_portal_hits"].append(rec)

                    if (
                        "menu" in low_url or "menù" in low_url or "carta" in low_url
                        or low_url.endswith(".pdf") or "menu" in low_title or "menù" in low_title
                    ):
                        result["menu_osint_hits"].append(rec)

                    # Estrazione rigorosa profili social dai risultati di ricerca
                    if "instagram.com/" in low_url and _is_valid_portal_profile(url, "instagram"):
                        result["discovered_social_urls"].setdefault("instagram", url)
                    elif "facebook.com/" in low_url and _is_valid_portal_profile(url, "facebook"):
                        result["discovered_social_urls"].setdefault("facebook", url)
                    elif "tiktok.com/" in low_url and _is_valid_portal_profile(url, "tiktok"):
                        result["discovered_social_urls"].setdefault("tiktok", url)
                    elif "youtube.com/" in low_url and _is_valid_portal_profile(url, "youtube"):
                        result["discovered_social_urls"].setdefault("youtube", url)

                for prof in payload.get("profiles", []):
                    purl = str(prof.get("profile_url") or prof.get("url") or "")
                    if purl and purl not in seen_profiles:
                        seen_profiles.add(purl)
                        result["discovered_profiles"].append(prof)

            elif label.startswith("username:"):
                result["username_scans"].append(payload)

        result["discovered_website_url"] = _pick_official_website(
            result["search_records"], restaurant_name, city
        )

    try:
        asyncio.run(asyncio.wait_for(_orchestrate(), timeout=45.0))
    except asyncio.TimeoutError:
        result["errors"].append("Timeout parziale durante la ricerca web (45s).")
    except Exception as exc:
        result["errors"].append(f"Errore ricerca web: {exc}")

    return result
