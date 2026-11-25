"""Modelli dati per l'input dell'audit e i risultati strutturati."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AuditInput:
    # Dati principali
    restaurant_name: str
    city: str
    address: str = ""
    cuisine_type: str = ""
    consultant_name: str = "RistoAudit Digital Consulting"
    consultant_contact: str = ""

    # Sito Web & Menu
    website_url: str = ""
    menu_url: str = ""
    known_menu_year: str = ""

    # Social & OSINT Handle
    social_handle: str = ""
    instagram_url: str = ""
    facebook_url: str = ""
    tiktok_url: str = ""
    last_social_post_date: str = ""  # YYYY-MM-DD

    # Recensioni & Reputazione
    google_maps_url: str = ""
    google_place_id: str = ""
    tripadvisor_url: str = ""
    thefork_url: str = ""
    google_rating: str = ""
    google_reviews_count: str = ""
    tripadvisor_rating: str = ""
    unanswered_reviews_count: str = ""
    unanswered_negative_reviews: str = ""
    reviews_notes: str = ""
    google_places_api_key: str = ""
    tripadvisor_terra_api_key: str = ""
    brightdata_api_key: str = ""
    openrouteservice_api_key: str = ""

    # Opzioni OSINT
    run_ominis_search: bool = True
    run_ominis_username: bool = True
    deep_link_check: bool = True


@dataclass
class Finding:
    category: str          # website, menu, social, reviews, osint
    severity: str          # CRITICO, ALTO, MEDIO, BASSO, OTTIMO
    title: str
    description: str
    business_impact: str
    recommendation: str
    evidence: str = ""


@dataclass
class CategoryScore:
    key: str
    label: str
    score: int
    weight: float
    summary: str


@dataclass
class AuditResult:
    audit_id: str
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%d/%m/%Y %H:%M"))
    input_data: dict[str, Any] = field(default_factory=dict)

    overall_score: int = 0
    overall_grade: str = ""
    executive_summary: str = ""

    category_scores: list[CategoryScore] = field(default_factory=list)
    strengths: list[Finding] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    website_data: dict[str, Any] = field(default_factory=dict)
    menu_data: dict[str, Any] = field(default_factory=dict)
    social_data: dict[str, Any] = field(default_factory=dict)
    reviews_data: dict[str, Any] = field(default_factory=dict)
    osint_data: dict[str, Any] = field(default_factory=dict)

    action_plan: dict[str, list[str]] = field(default_factory=dict)
    swot_analysis: dict[str, list[str]] = field(default_factory=dict)
    market_analysis: dict[str, Any] = field(default_factory=dict)
    email_draft: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

