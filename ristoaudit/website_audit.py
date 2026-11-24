"""Audit tecnico OSINT per sito web del ristorante e analisi del menu' (HTML / PDF)."""

from __future__ import annotations

import io
import json
import re
import socket
import ssl
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import certifi
import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SOCIAL_PATTERNS = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[^\"'\s<>]+", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/[^\"'\s<>]+", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[^\"'\s<>]+", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\"'\s<>]+", re.I),
    "tripadvisor": re.compile(r"https?://(?:www\.)?tripadvisor\.[a-z.]+/[^\"'\s<>]+", re.I),
    "thefork": re.compile(r"https?://(?:www\.)?(?:thefork|lafourchette)\.[a-z.]+/[^\"'\s<>]+", re.I),
    "whatsapp": re.compile(r"https?://(?:wa\.me|api\.whatsapp\.com)/[^\"'\s<>]+", re.I),
}

BOOKING_KEYWORDS = (
    "thefork", "quandoo", "plateform", "opentable", "superbexperience",
    "resy", "sevenrooms", "prenota", "prenotazione", "riserva un tavolo",
    "book a table", "wa.me", "whatsapp",
)

MENU_KEYWORDS = ("menu", "menù", "carta", "listino", "piatti", "cucina", "vini", "pizze", "pizzeria")
DIGITAL_MENU_HOSTS = ("leggimenu", "dishcovery", "plateform", "menuhub", "qrmenu", "drive.google.com", "fliphtml5", "issuu.com")
COMMON_MENU_PATHS = ("/menu", "/menu/", "/il-menu", "/il-menu/", "/menu.html", "/carta", "/la-cucina", "/pizzeria")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _check_ssl(hostname: str, port: int = 443) -> dict[str, Any]:
    info: dict[str, Any] = {
        "valid": False,
        "expires_at": "",
        "days_remaining": None,
        "issuer": "",
        "error": "",
    }
    try:
        try:
            ctx = ssl.create_default_context()
        except Exception:
            ctx = ssl.create_default_context(cafile=certifi.where())
        with socket.create_connection((hostname, port), timeout=6.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                if cert:
                    not_after = cert.get("notAfter")
                    if not_after:
                        dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                        days = (dt - datetime.now(timezone.utc)).days
                        info["expires_at"] = dt.strftime("%d/%m/%Y")
                        info["days_remaining"] = days
                        info["valid"] = days > 0
                    issuer = dict(x[0] for x in cert.get("issuer", ()))
                    info["issuer"] = issuer.get("organizationName", issuer.get("commonName", ""))
    except Exception as exc:
        info["error"] = str(exc)
    return info


def _parse_pdf_date(raw: str) -> datetime | None:
    if not raw:
        return None
    m = re.search(r"(\d{4})(\d{2})?(\d{2})?", str(raw))
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2) or 1)
    day = int(m.group(3) or 1)
    try:
        return datetime(year, max(1, min(12, month)), max(1, min(28, day)))
    except ValueError:
        return None


def _parse_http_date(header_val: str) -> datetime | None:
    if not header_val:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(header_val)
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def _parse_iso_date(raw: str) -> datetime | None:
    if not raw:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _extract_html_modified_date(soup: BeautifulSoup) -> tuple[datetime | None, str]:
    """Cerca la data di ultimo aggiornamento nei meta tag CMS (WordPress/Yoast/OG) o JSON-LD."""
    # 1. Meta tags standard (article:modified_time, og:updated_time, last-modified, article:published_time)
    meta_checks = (
        ("property", "article:modified_time", "Meta article:modified_time"),
        ("property", "og:updated_time", "Meta og:updated_time"),
        ("name", "last-modified", "Meta last-modified"),
        ("property", "article:published_time", "Meta article:published_time"),
    )
    for attr, val, label in meta_checks:
        tag = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(val)}$", re.I)})
        if tag and tag.get("content"):
            dt = _parse_iso_date(str(tag["content"]))
            if dt:
                return dt, label

    # 2. JSON-LD dateModified / datePublished
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_json = script.string or ""
        for key, label in (("dateModified", "JSON-LD dateModified"), ("datePublished", "JSON-LD datePublished")):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', raw_json)
            if m:
                dt = _parse_iso_date(m.group(1))
                if dt:
                    return dt, label

    return None, ""


def _extract_years(text: str) -> list[int]:
    current_year = datetime.now().year
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", text)]
    return sorted({y for y in years if 2015 <= y <= current_year + 1})


def analyze_menu(
    client: httpx.Client,
    menu_url: str,
    known_menu_year: str = "",
) -> dict[str, Any]:
    """Analizza una pagina menu HTML o un file PDF per verificare aggiornamento e fruibilita'."""
    data: dict[str, Any] = {
        "found": False,
        "url": menu_url,
        "format": "non_trovato",  # pdf, html, image_only, non_trovato
        "status_code": 0,
        "last_modified": "",
        "pdf_created": "",
        "pdf_modified": "",
        "estimated_year": None,
        "age_months": None,
        "is_outdated": False,
        "has_prices": False,
        "has_allergens": False,
        "is_scanned_image_pdf": False,
        "page_count": 0,
        "years_in_text": [],
        "error": "",
    }

    if known_menu_year and known_menu_year.isdigit():
        data["estimated_year"] = int(known_menu_year)

    if not menu_url:
        return data

    try:
        resp = client.get(menu_url, timeout=12.0, follow_redirects=True)
        data["status_code"] = resp.status_code
        data["url"] = str(resp.url)
        if resp.status_code >= 400:
            data["error"] = f"HTTP {resp.status_code}"
            return data

        data["found"] = True
        ctype = resp.headers.get("content-type", "").lower()
        lm_dt = _parse_http_date(resp.headers.get("last-modified", ""))
        if lm_dt:
            data["last_modified"] = lm_dt.strftime("%d/%m/%Y")

        is_pdf = "application/pdf" in ctype or str(resp.url).lower().split("?")[0].endswith(".pdf")
        if is_pdf:
            data["format"] = "pdf"
            try:
                reader = PdfReader(io.BytesIO(resp.content))
                data["page_count"] = len(reader.pages)
                meta = reader.metadata or {}
                c_dt = _parse_pdf_date(meta.get("/CreationDate", ""))
                m_dt = _parse_pdf_date(meta.get("/ModDate", ""))
                if c_dt:
                    data["pdf_created"] = c_dt.strftime("%d/%m/%Y")
                if m_dt:
                    data["pdf_modified"] = m_dt.strftime("%d/%m/%Y")

                text_parts = []
                for page in reader.pages[:15]:
                    text_parts.append(page.extract_text() or "")
                full_text = "\n".join(text_parts)

                if len(full_text.strip()) < 40:
                    data["is_scanned_image_pdf"] = True
                else:
                    data["has_prices"] = bool(re.search(r"€|\beur\b|\b\d+[.,]\d{2}\b", full_text, re.I))
                    data["has_allergens"] = bool(
                        re.search(r"allergen|glutine|lattosio|crostacei|frutta a guscio|celiach", full_text, re.I)
                    )
                    data["years_in_text"] = _extract_years(full_text)

                best_dt = m_dt or c_dt or lm_dt
                if best_dt:
                    months = (datetime.now().year - best_dt.year) * 12 + (datetime.now().month - best_dt.month)
                    data["age_months"] = max(0, months)
                    if not data["estimated_year"]:
                        data["estimated_year"] = best_dt.year
            except Exception as exc:
                data["error"] = f"Errore lettura PDF: {exc}"
        else:
            data["format"] = "html"
            soup = BeautifulSoup(resp.text[:500_000], "lxml")

            # Estrai data di modifica da meta CMS / JSON-LD prima di rimuovere gli script
            html_dt, html_dt_src = _extract_html_modified_date(soup)

            # Cerca eventuale PDF del menu linkato dentro la pagina HTML /menu/
            embedded_pdf_url = ""
            for a_tag in soup.find_all("a", href=True):
                href = str(a_tag.get("href") or "").strip()
                if ".pdf" in href.lower():
                    embedded_pdf_url = urllib.parse.urljoin(str(resp.url), href)
                    break

            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(" ", strip=True)
            imgs = soup.find_all("img")
            if len(text) < 250 and len(imgs) >= 1:
                data["format"] = "image_only"
            data["has_prices"] = bool(re.search(r"€|\beur\b|\b\d+[.,]\d{2}\b", text, re.I))
            data["has_allergens"] = bool(
                re.search(r"allergen|glutine|lattosio|crostacei|frutta a guscio|celiach", text, re.I)
            )
            data["years_in_text"] = _extract_years(text)

            best_dt = lm_dt or html_dt
            if html_dt and not data["last_modified"]:
                data["last_modified"] = html_dt.strftime("%d/%m/%Y")
                data["date_source"] = html_dt_src

            # Se non c'e' ancora alcuna data ma la pagina HTML linka un PDF del menu, ispeziona i metadati del PDF!
            if not best_dt and embedded_pdf_url:
                try:
                    r_pdf = client.get(embedded_pdf_url, timeout=8.0, follow_redirects=True)
                    if r_pdf.status_code == 200 and ("pdf" in r_pdf.headers.get("content-type", "").lower() or embedded_pdf_url.lower().endswith(".pdf")):
                        pdf_lm = _parse_http_date(r_pdf.headers.get("last-modified", ""))
                        reader = PdfReader(io.BytesIO(r_pdf.content))
                        meta = reader.metadata or {}
                        c_dt = _parse_pdf_date(meta.get("/CreationDate", ""))
                        m_dt = _parse_pdf_date(meta.get("/ModDate", ""))
                        if c_dt:
                            data["pdf_created"] = c_dt.strftime("%d/%m/%Y")
                        if m_dt:
                            data["pdf_modified"] = m_dt.strftime("%d/%m/%Y")
                        best_dt = m_dt or c_dt or pdf_lm
                        if best_dt:
                            data["date_source"] = "PDF allegato nella pagina menù"
                except Exception:
                    pass

            if best_dt:
                months = (datetime.now().year - best_dt.year) * 12 + (datetime.now().month - best_dt.month)
                data["age_months"] = max(0, months)
                if not data["estimated_year"]:
                    data["estimated_year"] = best_dt.year

        # Determina se il menu e' troppo vecchio (> 12 mesi o anno < anno corrente - 1)
        now_year = datetime.now().year
        if data["years_in_text"] and not data["estimated_year"]:
            data["estimated_year"] = max(data["years_in_text"])
        if data["age_months"] is not None and data["age_months"] >= 12:
            data["is_outdated"] = True
        elif data["estimated_year"] and data["estimated_year"] <= now_year - 2:
            data["is_outdated"] = True

    except Exception as exc:
        data["error"] = str(exc)

    return data


def run_website_and_menu_audit(
    website_url: str,
    menu_url: str = "",
    known_menu_year: str = "",
    deep_link_check: bool = True,
    osint_menu_candidates: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Esegue l'audit tecnico completo del sito web e del menu'."""
    site_url = normalize_url(website_url)
    explicit_menu_url = normalize_url(menu_url)

    site: dict[str, Any] = {
        "provided": bool(site_url),
        "url": site_url,
        "final_url": site_url,
        "reachable": False,
        "status_code": 0,
        "load_time_ms": 0,
        "uses_https": False,
        "ssl": {},
        "title": "",
        "meta_description": "",
        "h1_count": 0,
        "h1_texts": [],
        "has_viewport": False,
        "has_og_tags": False,
        "has_restaurant_schema": False,
        "schema_types": [],
        "has_phone": False,
        "has_email": False,
        "has_maps_embed": False,
        "has_booking_cta": False,
        "has_opening_hours": False,
        "has_privacy_cookie": False,
        "copyright_year": None,
        "copyright_outdated": False,
        "has_robots_txt": False,
        "has_sitemap_xml": False,
        "social_links_on_site": {},
        "broken_links": [],
        "checked_links_count": 0,
        "discovered_menu_url": "",
        "error": "",
    }

    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }

    try:
        ssl_ctx = ssl.create_default_context()
    except Exception:
        ssl_ctx = certifi.where()

    with httpx.Client(headers=headers, verify=ssl_ctx, follow_redirects=True, timeout=12.0) as client:
        if site_url:
            t0 = time.perf_counter()
            try:
                resp = client.get(site_url)
                site["load_time_ms"] = int((time.perf_counter() - t0) * 1000)
                site["status_code"] = resp.status_code
                site["final_url"] = str(resp.url)
                site["uses_https"] = str(resp.url).startswith("https://")
                site["reachable"] = 200 <= resp.status_code < 400

                parsed = urllib.parse.urlsplit(site["final_url"])
                if site["uses_https"] and parsed.hostname:
                    site["ssl"] = _check_ssl(parsed.hostname)

                if site["reachable"]:
                    html = resp.text[:1_500_000]
                    soup = BeautifulSoup(html, "lxml")

                    # SEO base
                    title_tag = soup.find("title")
                    site["title"] = title_tag.get_text(" ", strip=True) if title_tag else ""
                    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
                    site["meta_description"] = (md.get("content") or "").strip() if md else ""
                    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)]
                    site["h1_count"] = len(h1s)
                    site["h1_texts"] = h1s[:3]
                    site["has_viewport"] = bool(soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}))
                    site["has_og_tags"] = bool(soup.find("meta", attrs={"property": re.compile(r"^og:", re.I)}))

                    # Schema.org JSON-LD
                    schema_types: list[str] = []
                    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
                        try:
                            obj = json.loads(script.string or "{}")
                            items = obj if isinstance(obj, list) else [obj]
                            for it in items:
                                if isinstance(it, dict):
                                    t = it.get("@type")
                                    if isinstance(t, list):
                                        schema_types.extend(str(x) for x in t)
                                    elif t:
                                        schema_types.append(str(t))
                                    for g in it.get("@graph", []) if isinstance(it.get("@graph"), list) else []:
                                        if isinstance(g, dict) and g.get("@type"):
                                            schema_types.append(str(g.get("@type")))
                        except Exception:
                            pass
                    site["schema_types"] = sorted(set(schema_types))
                    site["has_restaurant_schema"] = any(
                        t.lower() in ("restaurant", "foodestablishment", "localbusiness", "barorpub", "bakery", "cafeorcoffeeshop")
                        for t in site["schema_types"]
                    )

                    # Elementi essenziali ristorante
                    body_text = soup.get_text(" ", strip=True)
                    low_html = html.lower()
                    site["has_phone"] = bool(
                        soup.find("a", href=re.compile(r"^tel:", re.I))
                        or re.search(r"(\+39\s*)?0\d{1,3}[\s.-]?\d{5,8}|\b3\d{2}[\s.-]?\d{6,7}\b", body_text)
                    )
                    site["has_email"] = bool(
                        soup.find("a", href=re.compile(r"^mailto:", re.I))
                        or re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", body_text)
                    )
                    site["has_maps_embed"] = bool(
                        "maps.google" in low_html or "google.com/maps" in low_html or "goo.gl/maps" in low_html
                    )
                    site["has_booking_cta"] = any(k in low_html for k in BOOKING_KEYWORDS)
                    site["has_opening_hours"] = bool(
                        re.search(r"orari|aperto|chiuso|luned[iì]|marted[iì]|domenica|pranzo|cena", body_text, re.I)
                    )
                    site["has_privacy_cookie"] = bool(
                        re.search(r"privacy|cookie|p\.?\s*iva|partita\s*iva", body_text, re.I)
                    )

                    # Copyright year check
                    cp_matches = re.findall(r"(?:©|copyright|&copy;)\s*(?:20\d{2}\s*[-–]\s*)?(20\d{2})", body_text, re.I)
                    if cp_matches:
                        max_cp = max(int(y) for y in cp_matches)
                        site["copyright_year"] = max_cp
                        site["copyright_outdated"] = max_cp < datetime.now().year - 1

                    # Estrazione social links + auto-discovery menu link
                    candidate_links: list[str] = []
                    for a in soup.find_all("a", href=True):
                        href = str(a.get("href") or "").strip()
                        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                            continue
                        abs_url = urllib.parse.urljoin(site["final_url"], href)
                        a_text = a.get_text(" ", strip=True).lower()

                        for sname, pat in SOCIAL_PATTERNS.items():
                            if pat.match(abs_url) and sname not in site["social_links_on_site"]:
                                site["social_links_on_site"][sname] = abs_url

                        # Scoperta link menu' (sia sul dominio sia su piattaforme menu digitali / PDF)
                        if not site["discovered_menu_url"]:
                            low_abs = abs_url.lower()
                            is_menu_hint = any(k in low_abs for k in MENU_KEYWORDS) or any(k in a_text for k in MENU_KEYWORDS)
                            is_digital_host = any(h in low_abs for h in DIGITAL_MENU_HOSTS)
                            if is_menu_hint and (parsed.netloc in abs_url or low_abs.endswith(".pdf") or is_digital_host):
                                site["discovered_menu_url"] = abs_url
                            elif is_digital_host:
                                site["discovered_menu_url"] = abs_url

                        if len(candidate_links) < 12 and abs_url.startswith(("http://", "https://")):
                            if abs_url not in candidate_links and abs_url != site["final_url"]:
                                candidate_links.append(abs_url)

                    # Verifica robots.txt e sitemap.xml
                    base_origin = f"{parsed.scheme}://{parsed.netloc}"
                    for fname, field_key in (("robots.txt", "has_robots_txt"), ("sitemap.xml", "has_sitemap_xml")):
                        try:
                            r_aux = client.head(f"{base_origin}/{fname}", timeout=4.0)
                            site[field_key] = r_aux.status_code == 200
                        except Exception:
                            pass

                    # Verifica link rotti (inclusi i social link dal sito)
                    if deep_link_check:
                        to_check = list(dict.fromkeys(candidate_links[:10] + list(site["social_links_on_site"].values())))
                        site["checked_links_count"] = len(to_check)
                        for link in to_check:
                            try:
                                r_lnk = client.head(link, timeout=5.0, follow_redirects=True)
                                if r_lnk.status_code in (404, 410, 500, 502, 503):
                                    site["broken_links"].append({"url": link, "status": r_lnk.status_code})
                            except Exception:
                                pass

            except Exception as exc:
                site["error"] = str(exc)

        # Analisi menu' (1: esplicito, 2: scoperto in homepage, 3: percorsi comuni sul sito, 4: risultati ricerca web)
        target_menu = explicit_menu_url or site.get("discovered_menu_url", "")
        if not target_menu and site.get("reachable") and site.get("final_url"):
            parsed_root = urllib.parse.urlsplit(site["final_url"])
            base_origin = f"{parsed_root.scheme}://{parsed_root.netloc}"
            for rel_path in COMMON_MENU_PATHS:
                probe_url = base_origin + rel_path
                try:
                    r_m = client.head(probe_url, timeout=4.0, follow_redirects=True)
                    if r_m.status_code == 200:
                        target_menu = str(r_m.url)
                        site["discovered_menu_url"] = target_menu
                        break
                except Exception:
                    pass

        if not target_menu and osint_menu_candidates:
            # Preferisci un URL del dominio del sito o un PDF o un servizio menu digitale
            site_host = urllib.parse.urlsplit(site.get("final_url", "")).netloc.lower() if site.get("final_url") else ""
            for cand in osint_menu_candidates:
                low_c = cand.lower()
                if (site_host and site_host in low_c) or low_c.endswith(".pdf") or any(h in low_c for h in DIGITAL_MENU_HOSTS):
                    target_menu = cand
                    break
            if not target_menu:
                target_menu = osint_menu_candidates[0]

        menu_data = analyze_menu(client, target_menu, known_menu_year=known_menu_year)

    return site, menu_data

