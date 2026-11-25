#!/usr/bin/env python3
"""Server Web locale per RistoAudit (Python stdlib + Jinja2, zero dipendenze extra)."""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import threading
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ristoaudit.city_restaurants import (
    DATA_DIR,
    ITALIAN_CITIES,
    get_city_restaurants,
    update_restaurant_in_city_cache,
)
from ristoaudit.email_draft import save_email_draft
from ristoaudit.models import AuditInput
from ristoaudit.pdf_report import generate_pdf_report
from ristoaudit.scoring import run_full_audit

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
REPORTS_DIR = BASE_DIR / "reports"
SETTINGS_FILE = BASE_DIR / ".ristoaudit_settings.json"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _run_audit_job(job_id: str, audit_input: AuditInput) -> None:
    try:
        result = run_full_audit(audit_input)
        pdf_path = generate_pdf_report(result, REPORTS_DIR)
        email_path = save_email_draft(result, REPORTS_DIR, pdf_path.stem)
        json_path = pdf_path.with_suffix(".json")
        json_path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done",
                "result": result,
                "pdf_filename": pdf_path.name,
                "email_filename": email_path.name,
            })
    except Exception as exc:
        print(f"[RistoAudit] Errore job {job_id}: {exc}")
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(exc)})


def _load_saved_settings() -> dict[str, str]:
    defaults = {
        "google_places_api_key": os.environ.get("GOOGLE_PLACES_API_KEY") or os.environ.get("google_places_api_key", ""),
        "tripadvisor_terra_api_key": os.environ.get("TRIPADVISOR_TERRA_API_KEY") or os.environ.get("tripadvisor_terra_api_key", ""),
        "brightdata_api_key": os.environ.get("BRIGHTDATA_API_KEY") or os.environ.get("brightdata_api_key", ""),
        "openrouteservice_api_key": os.environ.get("OPENROUTESERVICE_API_KEY") or os.environ.get("openrouteservice_api_key", ""),
        "consultant_name": os.environ.get("CONSULTANT_NAME") or os.environ.get("consultant_name") or "RistoAudit",
        "consultant_contact": os.environ.get("CONSULTANT_CONTACT") or os.environ.get("consultant_contact", ""),
    }
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for k in defaults:
                    if saved.get(k):
                        defaults[k] = str(saved[k])
        except Exception:
            pass
    return defaults


def _save_settings(
    api_key: str,
    ta_api_key: str,
    bd_api_key: str,
    consultant_name: str,
    consultant_contact: str,
) -> None:
    payload = {
        "google_places_api_key": api_key.strip(),
        "tripadvisor_terra_api_key": ta_api_key.strip(),
        "brightdata_api_key": bd_api_key.strip(),
        "consultant_name": consultant_name.strip() or "RistoAudit",
        "consultant_contact": consultant_contact.strip(),
    }
    try:
        SETTINGS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _form_val(params: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = params.get(key)
    return vals[0].strip() if vals else default


def _form_bool(params: dict[str, list[str]], key: str, default: bool = False) -> bool:
    if key not in params:
        return default
    return params[key][0].lower() in ("1", "true", "on", "yes")


class RistoAuditHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        print(f"[RistoAudit] {self.address_string()} - {format % args}")

    def _require_auth(self) -> bool:
        """Se RISTOAUDIT_PASSWORD e' impostata nell'ambiente, protegge l'app con Basic Auth."""
        expected_pwd = os.environ.get("RISTOAUDIT_PASSWORD", "").strip()
        if not expected_pwd:
            return True
        expected_user = os.environ.get("RISTOAUDIT_USER", "admin").strip() or "admin"
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8", errors="replace")
                user, _, pwd = decoded.partition(":")
                if hmac.compare_digest(user, expected_user) and hmac.compare_digest(pwd, expected_pwd):
                    return True
            except Exception:
                pass
        body = b"Accesso riservato. Inserisci username e password."
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="RistoAudit Area Riservata"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_HEAD(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_html(self, content: str, status: int = 200) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json({"status": "ok", "service": "RistoAudit"})
            return

        if not self._require_auth():
            return

        if path == "/" or path == "/index.html":
            recent_pdfs = sorted(
                REPORTS_DIR.glob("*.pdf"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:8]
            tpl = jinja_env.get_template("index.html")
            self._send_html(
                tpl.render(
                    recent_pdfs=[p.name for p in recent_pdfs],
                    saved=_load_saved_settings(),
                    cities=ITALIAN_CITIES,
                    error="",
                )
            )
            return

        if path == "/api/restaurants":
            qs = urllib.parse.parse_qs(parsed.query)
            city_q = (qs.get("city") or [""])[0].strip()
            refresh_q = (qs.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
            saved = _load_saved_settings()
            data = get_city_restaurants(
                city=city_q,
                google_places_api_key=saved.get("google_places_api_key", ""),
                force_refresh=refresh_q,
                limit=50,
            )
            self._send_json(data)
            return

        if path == "/assets/ristoaudit-logo.png":
            logo_candidates = [BASE_DIR / "ristoaudit-logo.png", REPORTS_DIR / "ristoaudit-logo.png"]
            logo_file = next((p for p in logo_candidates if p.exists()), None)
            if not logo_file:
                self.send_error(HTTPStatus.NOT_FOUND, "Logo non trovato")
                return
            data = logo_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/job/"):
            job_id = path[len("/job/"):].strip("/")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_error(HTTPStatus.NOT_FOUND, "Analisi non trovata")
                return
            if job["status"] == "done":
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/result/{job_id}")
                self.end_headers()
                return
            tpl = jinja_env.get_template("loading.html")
            self._send_html(tpl.render(
                job_id=job_id,
                restaurant_name=job.get("restaurant_name", ""),
                city=job.get("city", ""),
            ))
            return

        if path.startswith("/status/"):
            job_id = path[len("/status/"):].strip("/")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json({"status": "error", "error": "Job non trovato"}, status=404)
                return
            self._send_json({"status": job["status"], "error": job.get("error", "")})
            return

        if path.startswith("/result/"):
            job_id = path[len("/result/"):].strip("/")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job or job["status"] != "done":
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/job/{job_id}" if job else "/")
                self.end_headers()
                return
            tpl = jinja_env.get_template("result.html")
            self._send_html(tpl.render(
                result=job["result"],
                pdf_filename=job["pdf_filename"],
                email_filename=job.get("email_filename", ""),
            ))
            return

        if path.startswith("/download-data/"):
            filename = urllib.parse.unquote(path[len("/download-data/"):])
            if "/" in filename or "\\" in filename or ".." in filename or not (
                filename.endswith(".json") or filename.endswith(".csv")
            ):
                self.send_error(HTTPStatus.BAD_REQUEST, "Nome file non valido")
                return
            data_file = DATA_DIR / filename
            if not data_file.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "File dati non trovato")
                return
            content = data_file.read_bytes()
            ctype = "text/csv; charset=utf-8" if filename.endswith(".csv") else "application/json; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path.startswith("/download-email/"):
            filename = urllib.parse.unquote(path[len("/download-email/"):])
            if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".txt"):
                self.send_error(HTTPStatus.BAD_REQUEST, "Nome file non valido")
                return
            email_file = REPORTS_DIR / filename
            if not email_file.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Bozza email non trovata")
                return
            data = email_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/download/"):
            filename = urllib.parse.unquote(path[len("/download/"):])
            if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".pdf"):
                self.send_error(HTTPStatus.BAD_REQUEST, "Nome file non valido")
                return
            pdf_file = REPORTS_DIR / filename
            if not pdf_file.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Report PDF non trovato")
                return
            data = pdf_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Pagina non trovata")

    def do_POST(self) -> None:
        if not self._require_auth():
            return

        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/restaurants/update":
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw_body)
            except Exception:
                self._send_json({"updated": False, "error": "JSON non valido"}, status=400)
                return
            res = update_restaurant_in_city_cache(
                city=str(body.get("city") or ""),
                updates=body,
                overwrite_empty=True,
            )
            self._send_json(res)
            return

        if parsed.path != "/audit":
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint non trovato")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        params = urllib.parse.parse_qs(raw_body, keep_blank_values=True)

        restaurant_name = _form_val(params, "restaurant_name")
        city = _form_val(params, "city")

        if not restaurant_name or not city:
            tpl = jinja_env.get_template("index.html")
            self._send_html(
                tpl.render(
                    recent_pdfs=[],
                    saved=_load_saved_settings(),
                    cities=ITALIAN_CITIES,
                    error="Inserisci almeno il Nome del Ristorante e la Città.",
                ),
                status=400,
            )
            return

        saved = _load_saved_settings()
        api_key = _form_val(params, "google_places_api_key") or saved["google_places_api_key"]
        ta_api_key = _form_val(params, "tripadvisor_terra_api_key") or saved["tripadvisor_terra_api_key"]
        bd_api_key = _form_val(params, "brightdata_api_key") or saved["brightdata_api_key"]
        consultant_name = _form_val(params, "consultant_name") or saved["consultant_name"]
        consultant_contact = _form_val(params, "consultant_contact") or saved["consultant_contact"]

        if _form_bool(params, "remember_settings"):
            _save_settings(api_key, ta_api_key, bd_api_key, consultant_name, consultant_contact)

        # Il campo google_maps_or_place_id accetta sia un link Google Maps sia un Place ID (ChIJ...)
        maps_or_pid = _form_val(params, "google_maps_url")
        explicit_pid = _form_val(params, "google_place_id")
        if not explicit_pid and maps_or_pid.startswith("ChIJ"):
            explicit_pid = maps_or_pid
            maps_or_pid = ""

        audit_input = AuditInput(
            restaurant_name=restaurant_name,
            city=city,
            address=_form_val(params, "address"),
            cuisine_type=_form_val(params, "cuisine_type"),
            consultant_name=consultant_name,
            consultant_contact=consultant_contact,
            website_url=_form_val(params, "website_url"),
            menu_url=_form_val(params, "menu_url"),
            known_menu_year=_form_val(params, "known_menu_year"),
            social_handle=_form_val(params, "social_handle"),
            instagram_url=_form_val(params, "instagram_url"),
            facebook_url=_form_val(params, "facebook_url"),
            tiktok_url=_form_val(params, "tiktok_url"),
            last_social_post_date=_form_val(params, "last_social_post_date"),
            google_maps_url=maps_or_pid,
            google_place_id=explicit_pid,
            tripadvisor_url=_form_val(params, "tripadvisor_url"),
            thefork_url=_form_val(params, "thefork_url"),
            google_rating=_form_val(params, "google_rating"),
            google_reviews_count=_form_val(params, "google_reviews_count"),
            tripadvisor_rating=_form_val(params, "tripadvisor_rating"),
            unanswered_reviews_count=_form_val(params, "unanswered_reviews_count"),
            unanswered_negative_reviews=_form_val(params, "unanswered_negative_reviews"),
            reviews_notes=_form_val(params, "reviews_notes"),
            google_places_api_key=api_key,
            tripadvisor_terra_api_key=ta_api_key,
            brightdata_api_key=bd_api_key,
            openrouteservice_api_key=saved.get("openrouteservice_api_key", ""),
            run_ominis_search=_form_bool(params, "run_ominis_search", default=True),
            run_ominis_username=_form_bool(params, "run_ominis_username", default=True),
            deep_link_check=_form_bool(params, "deep_link_check", default=True),
        )

        # Salva automaticamente eventuali campi compilati manualmente nel JSON/CSV della citta'
        update_restaurant_in_city_cache(
            city=city,
            updates={
                "restaurant_name": restaurant_name,
                "google_place_id": explicit_pid,
                "google_maps_url": maps_or_pid,
                "address": audit_input.address,
                "cuisine_type": audit_input.cuisine_type,
                "website_url": audit_input.website_url,
                "menu_url": audit_input.menu_url,
                "instagram_url": audit_input.instagram_url,
                "facebook_url": audit_input.facebook_url,
                "tiktok_url": audit_input.tiktok_url,
                "social_handle": audit_input.social_handle,
                "tripadvisor_url": audit_input.tripadvisor_url,
                "thefork_url": audit_input.thefork_url,
                "reviews_notes": audit_input.reviews_notes,
            },
            overwrite_empty=False,
        )

        job_id = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "running",
                "restaurant_name": restaurant_name,
                "city": city,
                "error": "",
            }
        threading.Thread(target=_run_audit_job, args=(job_id, audit_input), daemon=True).start()

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/job/{job_id}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    env_port = os.environ.get("PORT", "").strip()
    default_host = "0.0.0.0" if env_port else "127.0.0.1"
    default_port = int(env_port) if env_port.isdigit() else 8765

    parser = argparse.ArgumentParser(description="RistoAudit - Analisi Presenza Digitale Ristoranti")
    parser.add_argument("--host", default=default_host, help=f"Host di ascolto (default: {default_host})")
    parser.add_argument("--port", type=int, default=default_port, help=f"Porta HTTP (default: {default_port})")
    parser.add_argument("--open", action="store_true", help="Apri automaticamente il browser")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), RistoAuditHandler)
    url = f"http://{args.host}:{args.port}"
    auth_state = "ATTIVA (Basic Auth)" if os.environ.get("RISTOAUDIT_PASSWORD") else "Disattiva (uso locale)"
    print(f"\n  RistoAudit attivo su: {url}")
    print(f"  Protezione password: {auth_state}")
    print(f"  I report PDF vengono salvati in: {REPORTS_DIR}\n")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer arrestato.")
        server.server_close()


if __name__ == "__main__":
    main()
