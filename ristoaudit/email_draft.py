"""Generatore automatico della bozza email commerciale per il ristoratore.

Produce un'email professionale, autorevole e personalizzata sui risultati reali
dell'audit RistoAudit, a nome di Luca Perini (titolare di Take'n'Go), completa
di sommario testuale del report e riferimento al PDF completo in allegato.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from .models import AuditResult


def _format_social_channel_line(label: str, ch: dict[str, Any]) -> str:
    if not ch or not ch.get("present"):
        return f"  • {label}: Non rilevato (0/5)"
    rec = ch.get("recency") or {}
    score_str = rec.get("score_str") or "N/D"
    last_date = str(rec.get("last_date") or "")[:10]
    days_ago = rec.get("days_ago")
    if last_date and len(last_date) == 10 and last_date[4] == "-":
        d_it = f"{last_date[8:10]}/{last_date[5:7]}/{last_date[0:4]}"
        if days_ago is not None:
            return f"  • {label}: Presente ({score_str} — ultimo post il {d_it}, {days_ago} gg fa)"
        return f"  • {label}: Presente ({score_str} — ultimo post il {d_it})"
    if days_ago is not None:
        return f"  • {label}: Presente ({score_str} — ultima attività {days_ago} gg fa)"
    return f"  • {label}: Presente ({score_str} — data ultimo post non esposta pubblicamente)"


def _format_website_menu_summary(website_data: dict[str, Any], menu_data: dict[str, Any]) -> str:
    if not website_data.get("provided"):
        site_txt = "Sito web ufficiale non rilevato"
    elif not website_data.get("reachable"):
        site_txt = "Sito web irraggiungibile o con errore tecnico"
    else:
        parts = ["Sito web attivo"]
        if not website_data.get("https"):
            parts.append("senza HTTPS")
        if not website_data.get("mobile_viewport"):
            parts.append("non ottimizzato per smartphone")
        if not website_data.get("has_booking_cta"):
            parts.append("senza prenotazione online evidente")
        site_txt = ", ".join(parts)

    if not menu_data.get("found"):
        menu_txt = "menù digitale non trovato online"
    elif menu_data.get("is_outdated"):
        yr = menu_data.get("detected_year")
        menu_txt = f"menù digitale datato (riferito al {yr})" if yr else "menù digitale non aggiornato"
    elif menu_data.get("is_pdf"):
        menu_txt = "menù presente in formato PDF"
    else:
        menu_txt = "menù digitale presente e consultabile online"

    return f"{site_txt}; {menu_txt}."


def generate_outreach_email(result: AuditResult) -> dict[str, str]:
    """Genera oggetto, corpo testuale completo e link mailto per il ristoratore."""
    inp = result.input_data or {}
    restaurant_name = str(inp.get("restaurant_name") or "il vostro ristorante").strip()
    city = str(inp.get("city") or "").strip()
    consultant_contact = str(inp.get("consultant_contact") or "").strip()

    city_suffix = f" ({city})" if city else ""
    subject = (
        f"Audit digitale gratuito per {restaurant_name}{city_suffix} "
        f"– Valutazione {result.overall_score}/100 e report completo in allegato"
    )

    # Punteggi per area
    area_lines = [
        f"  • {c.label}: {c.score}/100"
        for c in (result.category_scores or [])
    ]

    # Fotografia rapida
    rev = result.reviews_data or {}
    soc_ch = (result.social_data or {}).get("channels") or {}
    snapshot_lines: list[str] = []

    if rev.get("google_rating") is not None:
        g_cnt = rev.get("google_reviews_count")
        cnt_txt = f" ({g_cnt:,} recensioni)".replace(",", ".") if isinstance(g_cnt, int) else ""
        unans = rev.get("unanswered_reviews_count")
        unans_txt = f" — {unans} recensioni recenti senza risposta" if isinstance(unans, int) and unans > 0 else ""
        snapshot_lines.append(f"  • Google Maps: {rev['google_rating']}/5{cnt_txt}{unans_txt}")
    if rev.get("tripadvisor_rating") is not None:
        ta_rank = f" ({rev['tripadvisor_ranking']})" if rev.get("tripadvisor_ranking") else ""
        snapshot_lines.append(f"  • TripAdvisor: {rev['tripadvisor_rating']}/5{ta_rank}")

    snapshot_lines.append(_format_social_channel_line("Instagram", soc_ch.get("instagram") or {}))
    snapshot_lines.append(_format_social_channel_line("Facebook", soc_ch.get("facebook") or {}))
    snapshot_lines.append(f"  • Sito & Menù: {_format_website_menu_summary(result.website_data or {}, result.menu_data or {})}")

    mkt = result.market_analysis or {}
    if mkt.get("available_market_share_pct") is not None and mkt.get("competitors"):
        snapshot_lines.append(
            f"  • Opportunità locale ({mkt.get('isochrone_minutes', 10)} min in auto): "
            f"{mkt['available_market_share_pct']}% di quota di mercato ancora disponibile in zona"
        )

    # Punti di forza (max 3)
    strength_lines = [f"  ✓ {s.title}" for s in (result.strengths or [])[:3]]

    # Criticità prioritarie (max 4, ordinate per severità)
    sev_order = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "BASSO": 3}
    sorted_findings = sorted(
        result.findings or [],
        key=lambda f: sev_order.get(str(f.severity).upper(), 9),
    )
    finding_lines = [f"  ! [{f.severity}] {f.title}" for f in sorted_findings[:4]]

    summary_blocks: list[str] = [
        "============================================================",
        f"SOMMARIO REPORT RISTOAUDIT — {restaurant_name.upper()}{city_suffix.upper()}",
        "============================================================",
        f"Punteggio Complessivo Presenza Online: {result.overall_score}/100 ({result.overall_grade})",
    ]
    if area_lines:
        summary_blocks.append("\nPunteggi per area analizzata:\n" + "\n".join(area_lines))
    if snapshot_lines:
        summary_blocks.append("\nFotografia sintetica dei canali:\n" + "\n".join(snapshot_lines))
    if strength_lines:
        summary_blocks.append("\nPrincipali punti di forza rilevati:\n" + "\n".join(strength_lines))
    if finding_lines:
        summary_blocks.append("\nPrincipali criticità su cui intervenire prioritariamente:\n" + "\n".join(finding_lines))
    summary_blocks.append("============================================================")

    summary_text = "\n".join(summary_blocks)

    signature_lines = [
        "Luca Perini",
        "Titolare — Take'n'Go",
        "Sistemi di Informatizzazione per la Ristorazione | RistoAudit",
        "email: lperini@takengo.it - cell: 3382244854"
    ]
    if consultant_contact:
        signature_lines.append(consultant_contact)
    signature = "\n".join(signature_lines)

    body = (
        f"Gentile Titolare / Direzione di {restaurant_name},\n\n"
        f"oggi oltre l'80% dei clienti sceglie dove pranzare o cenare molto prima di entrare nel locale: "
        f"cerca su Google Maps, legge le recensioni, consulta il menù dallo smartphone e verifica l'attività "
        f"sui social. Ogni imperfezione lungo questo percorso — una scheda Google incompleta, un menù poco "
        f"leggibile da mobile, profili social non presidiati o recensioni senza risposta — rischia di far "
        f"perdere silenziosamente ogni settimana prenotazioni e coperti a vantaggio dei locali concorrenti.\n\n"
        f"Mi presento: sono Luca Perini, titolare di Take'n'Go, realtà specializzata nella fornitura di "
        f"sistemi di informatizzazione e soluzioni digitali per i ristoranti.\n\n"
        f"Proprio per aiutare i ristoratori a proteggere e far crescere il proprio lavoro anche sul web, "
        f"abbiamo sviluppato RistoAudit: un'analisi tecnica e strategica pensata esclusivamente per la "
        f"ristorazione che misura in modo oggettivo l'efficacia di sito web, menù digitale, canali social, "
        f"recensioni online e posizionamento rispetto ai competitor della zona. Disporre di questi dati "
        f"significa sapere con precisione dove intervenire per trasformare le ricerche online in nuovi clienti in sala.\n\n"
        f"Abbiamo realizzato un report gratuito e personalizzato sulla presenza online di {restaurant_name}, "
        f"che ho il piacere di condividere con voi. Sarei molto lieto di incontrarvi senza alcun impegno "
        f"— presso il vostro locale oppure con una breve call conoscitiva di 15-20 minuti — per illustrarvi "
        f"meglio il funzionamento di RistoAudit e presentarvi i nostri servizi dedicati alla risoluzione "
        f"concreta delle criticità riscontrate.\n\n"
        f"Di seguito trovate un breve sommario dei principali risultati emersi dall'analisi:\n\n"
        f"{summary_text}\n\n"
        f"In allegato a questa email troverete il report completo in formato PDF, contenente il dettaglio "
        f"tecnico di ogni area analizzata, la matrice S.W.O.T., lo studio dell'area competitiva e il piano "
        f"d'azione operativo.\n\n"
        f"Resto a completa disposizione per fissare un incontro senza impegno nel giorno e nell'orario "
        f"per voi più comodi: sarà sufficiente rispondere a questa email o contattarmi direttamente.\n\n"
        f"Cordiali saluti,\n\n"
        f"{signature}\n"
    )

    recipient_email = str((result.website_data or {}).get("contact_email") or "").strip()
    mailto_params = urllib.parse.urlencode(
        {"subject": subject, "body": body},
        quote_via=urllib.parse.quote,
    )
    mailto_url = f"mailto:{recipient_email}?{mailto_params}"

    full_text = f"OGGETTO: {subject}\n" + (f"A: {recipient_email}\n" if recipient_email else "") + "\n" + body

    return {
        "subject": subject,
        "body": body,
        "recipient_email": recipient_email,
        "mailto_url": mailto_url,
        "full_text": full_text,
    }


def save_email_draft(result: AuditResult, output_dir: Path, pdf_stem: str) -> Path:
    """Salva la bozza email in formato .txt accanto al report PDF."""
    output_dir.mkdir(parents=True, exist_ok=True)
    draft = result.email_draft or generate_outreach_email(result)
    email_path = output_dir / f"{pdf_stem}_email.txt"
    email_path.write_text(draft.get("full_text", ""), encoding="utf-8")
    return email_path
