"""Generatore di report PDF esecutivi in italiano per i ristoranti (ReportLab)."""

from __future__ import annotations

import html
import io
import re
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import AuditResult

# Palette colori consulenziale moderna
NAVY = colors.HexColor("#1E293B")
DARK_SLATE = colors.HexColor("#0F172A")
ACCENT = colors.HexColor("#C2410C")
MUTED = colors.HexColor("#64748B")
LIGHT_BG = colors.HexColor("#F8FAFC")
BORDER = colors.HexColor("#E2E8F0")

SEV_COLORS = {
    "CRITICO": colors.HexColor("#DC2626"),
    "ALTO": colors.HexColor("#EA580C"),
    "MEDIO": colors.HexColor("#D97706"),
    "BASSO": colors.HexColor("#2563EB"),
    "OTTIMO": colors.HexColor("#16A34A"),
}

SEV_BG = {
    "CRITICO": colors.HexColor("#FEF2F2"),
    "ALTO": colors.HexColor("#FFF7ED"),
    "MEDIO": colors.HexColor("#FFFBEB"),
    "BASSO": colors.HexColor("#EFF6FF"),
    "OTTIMO": colors.HexColor("#F0FDF4"),
}


def _register_fonts() -> tuple[str, str]:
    """Registra font Unicode su macOS/Linux se disponibili, con fallback Helvetica."""
    candidates = [
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for reg_path, bold_path in candidates:
        if Path(reg_path).exists() and Path(bold_path).exists():
            try:
                if "RistoSans" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("RistoSans", reg_path))
                    pdfmetrics.registerFont(TTFont("RistoSans-Bold", bold_path))
                return "RistoSans", "RistoSans-Bold"
            except Exception:
                pass
    return "Helvetica", "Helvetica-Bold"


def _score_color(score: int) -> colors.Color:
    if score >= 80:
        return colors.HexColor("#16A34A")
    if score >= 60:
        return colors.HexColor("#D97706")
    if score >= 40:
        return colors.HexColor("#EA580C")
    return colors.HexColor("#DC2626")


def _esc(text: Any) -> str:
    return html.escape(str(text or ""))


def _score_bar(score: int, width: float = 160, height: float = 10) -> Drawing:
    d = Drawing(width, height + 2)
    d.add(Rect(0, 1, width, height, fillColor=BORDER, strokeColor=None, rx=3, ry=3))
    fill_w = max(2, width * (max(0, min(100, score)) / 100.0))
    d.add(Rect(0, 1, fill_w, height, fillColor=_score_color(score), strokeColor=None, rx=3, ry=3))
    return d


def _score_badge_drawing(score: int, grade: str, font_bold: str) -> Drawing:
    w, h = 115, 74
    d = Drawing(w, h)
    col = _score_color(score)
    d.add(Rect(0, 0, w, h, fillColor=col, strokeColor=None, rx=8, ry=8))
    d.add(String(w / 2, 34, f"{score}/100", fontName=font_bold, fontSize=24, fillColor=colors.white, textAnchor="middle"))
    d.add(String(w / 2, 13, grade.upper(), fontName=font_bold, fontSize=10, fillColor=colors.white, textAnchor="middle"))
    return d


def _load_logo_flowable(target_height_mm: float = 34.0) -> RLImage | None:
    """Carica ristoaudit-logo.png, ritaglia i margini bianchi e lo ridimensiona nitido per il PDF."""
    base_dir = Path(__file__).resolve().parent.parent
    candidates = [base_dir / "ristoaudit-logo.png", base_dir / "reports" / "ristoaudit-logo.png"]
    logo_path = next((p for p in candidates if p.exists()), None)
    if not logo_path:
        return None
    try:
        im = PILImage.open(logo_path).convert("RGBA")
        # Componi su sfondo bianco e ritaglia i margini vuoti per renderlo piu' grande e nitido
        bg = PILImage.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        # Trova bounding box dei pixel non bianchi
        diff = bg.convert("L").point(lambda p: 255 if p < 248 else 0)
        bbox = diff.getbbox()
        if bbox:
            pad = 24
            x0 = max(0, bbox[0] - pad)
            y0 = max(0, bbox[1] - pad)
            x1 = min(bg.width, bbox[2] + pad)
            y1 = min(bg.height, bbox[3] + pad)
            bg = bg.crop((x0, y0, x1, y1))
        bg.thumbnail((640, 640), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        bg.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        aspect = bg.width / max(1, bg.height)
        h_pt = target_height_mm * mm
        w_pt = h_pt * aspect
        return RLImage(buf, width=w_pt, height=h_pt)
    except Exception:
        return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "ristorante").lower()).strip("_")
    return slug[:40] or "ristorante"


def generate_pdf_report(result: AuditResult, output_dir: Path | str) -> Path:
    """Genera il file PDF completo e restituisce il percorso del file creato."""
    out_path = Path(output_dir) if isinstance(output_dir, str) else output_dir
    out_path.mkdir(parents=True, exist_ok=True)
    inp = result.input_data
    slug = _slugify(inp.get("restaurant_name", "ristorante"))
    pdf_path = out_path / f"RistoAudit_{slug}_{result.audit_id}.pdf"

    font_reg, font_bold = _register_fonts()
    styles = getSampleStyleSheet()

    s_title = ParagraphStyle("RTitle", parent=styles["Normal"], fontName=font_bold, fontSize=22, leading=26, textColor=colors.white)
    s_subtitle = ParagraphStyle("RSub", parent=styles["Normal"], fontName=font_reg, fontSize=10.5, leading=14, textColor=colors.HexColor("#CBD5E1"))
    s_h2 = ParagraphStyle("RH2", parent=styles["Normal"], fontName=font_bold, fontSize=13, leading=17, textColor=NAVY, spaceBefore=12, spaceAfter=6)
    s_body = ParagraphStyle("RBody", parent=styles["Normal"], fontName=font_reg, fontSize=9.5, leading=13.5, textColor=DARK_SLATE)
    s_small = ParagraphStyle("RSmall", parent=styles["Normal"], fontName=font_reg, fontSize=8.5, leading=11.5, textColor=MUTED)
    s_bold = ParagraphStyle("RBold", parent=styles["Normal"], fontName=font_bold, fontSize=9.5, leading=13, textColor=DARK_SLATE)
    s_cell = ParagraphStyle("RCell", parent=styles["Normal"], fontName=font_reg, fontSize=8.5, leading=11.5, textColor=DARK_SLATE)
    s_cell_b = ParagraphStyle("RCellB", parent=styles["Normal"], fontName=font_bold, fontSize=8.5, leading=11.5, textColor=DARK_SLATE)
    s_th = ParagraphStyle("RTH", parent=styles["Normal"], fontName=font_bold, fontSize=8.5, leading=11, textColor=colors.white)

    consultant = inp.get("consultant_name") or "RistoAudit Digital Consulting"
    rest_name = inp.get("restaurant_name") or "Ristorante"

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        canvas.setFont(font_reg, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 9.5 * mm, f"{consultant}  |  Audit Presenza Digitale: {rest_name}")
        canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, f"Pagina {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=f"Audit Presenza Digitale - {rest_name}",
        author=consultant,
    )

    story: list[Any] = []
    usable_w = A4[0] - 36 * mm

    # =========================================================================
    # HERO HEADER BANNER CON LOGO GRANDE RISTOAUDIT
    # =========================================================================
    city_line = inp.get("city", "")
    if inp.get("address"):
        city_line = f"{inp['address']}, {city_line}" if city_line else inp["address"]
    if inp.get("cuisine_type"):
        city_line += f"  •  Cucina: {inp['cuisine_type']}"

    logo_img = _load_logo_flowable(target_height_mm=34.0)
    header_mid = [
        Paragraph("REPORT PRESENZA DIGITALE PER LA RISTORAZIONE", ParagraphStyle("Tag", fontName=font_bold, fontSize=8, textColor=colors.HexColor("#FDBA74"), spaceAfter=4)),
        Paragraph(_esc(rest_name), s_title),
        Spacer(1, 3),
        Paragraph(_esc(city_line), s_subtitle),
        Spacer(1, 6),
        Paragraph(f"Data report: {_esc(result.created_at)}   |   Preparato da: {_esc(consultant)}", s_subtitle),
    ]
    badge = _score_badge_drawing(result.overall_score, result.overall_grade, font_bold)

    if logo_img is not None:
        logo_card = Table([[logo_img]], colWidths=[logo_img.drawWidth + 10])
        logo_card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        logo_col_w = logo_img.drawWidth + 18
        badge_col_w = 120
        mid_col_w = usable_w - logo_col_w - badge_col_w
        hero_tbl = Table([[logo_card, header_mid, badge]], colWidths=[logo_col_w, mid_col_w, badge_col_w])
        hero_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (2, 0), (2, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ]))
    else:
        hero_tbl = Table([[header_mid, badge]], colWidths=[usable_w - 125, 125])
        hero_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 14),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ]))
    story.append(hero_tbl)
    story.append(Spacer(1, 10))

    # =========================================================================
    # PRESENTAZIONE RISTOAUDIT
    # =========================================================================
    contact_suffix = f" ({_esc(inp['consultant_contact'])})" if inp.get("consultant_contact") else ""
    intro_html = (
        f"<b>Chi siamo — Benvenuti in RistoAudit</b><br/>"
        f"<b>RistoAudit</b> è il servizio specializzato nell'analisi e nell'ottimizzazione della presenza online "
        f"dei ristoranti: valutiamo sito web, menù digitale, canali social, recensioni e visibilità sui motori di ricerca "
        f"per aiutare i locali ad attrarre nuovi clienti e aumentare le prenotazioni.<br/><br/>"
        f"<b>Questo report viene offerto gratuitamente al vostro ristorante</b> per offrirvi una fotografia chiara, "
        f"concreta e immediata dei punti di forza e delle aree in cui oggi state perdendo visibilità o coperti. "
        f"Noi di <b>RistoAudit</b> vi offriamo i nostri servizi dedicati per <b>agire direttamente sulle criticità "
        f"evidenziate</b> in questo documento — dall'aggiornamento del sito e del menù alla gestione di social e recensioni. "
        f"<b>Sentitevi liberi di contattarci per ogni informazione{contact_suffix}.</b>"
    )
    intro_box = Table([[Paragraph(intro_html, s_body)]], colWidths=[usable_w])
    intro_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7ED")),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#FDBA74")),
        ("LINEBEFORE", (0, 0), (0, -1), 3.5, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(intro_box)
    story.append(Spacer(1, 8))

    # =========================================================================
    # EXECUTIVE SUMMARY
    # =========================================================================
    exec_box = Table([[Paragraph(f"<b>Sintesi Esecutiva:</b> {_esc(result.executive_summary)}", s_body)]], colWidths=[usable_w])
    exec_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
        ("LINEBEFORE", (0, 0), (0, -1), 3.5, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(exec_box)
    story.append(Spacer(1, 10))

    # =========================================================================
    # SCORECARD 5 PILASTRI
    # =========================================================================
    story.append(Paragraph("1. Valutazione per Area Strategica", s_h2))
    sc_rows = [[
        Paragraph("Area di Analisi", s_th),
        Paragraph("Punteggio", s_th),
        Paragraph("Stato Visivo", s_th),
        Paragraph("Ambito Valutato", s_th),
    ]]
    for c in result.category_scores:
        sc_rows.append([
            Paragraph(_esc(c.label), s_cell_b),
            Paragraph(f"<b>{c.score}/100</b>", ParagraphStyle("ScNum", parent=s_cell_b, textColor=_score_color(c.score), alignment=TA_CENTER)),
            _score_bar(c.score, width=110, height=9),
            Paragraph(_esc(c.summary), s_small),
        ])

    sc_tbl = Table(sc_rows, colWidths=[usable_w * 0.28, usable_w * 0.12, usable_w * 0.24, usable_w * 0.36])
    sc_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(sc_tbl)
    story.append(Spacer(1, 10))

    # =========================================================================
    # PUNTI DI FORZA RILEVATI
    # =========================================================================
    if result.strengths:
        story.append(Paragraph("2. Punti di Forza Rilevati (Cosa sta già funzionando bene)", s_h2))
        for st in result.strengths:
            st_html = (
                f"<b><font color='#15803D'>[PUNTO DI FORZA]</font> {_esc(st.title)}</b><br/>"
                f"{_esc(st.description)}<br/>"
                f"<b>Valore per il locale:</b> {_esc(st.business_impact)}"
            )
            st_card = Table([[Paragraph(st_html, s_cell)]], colWidths=[usable_w])
            st_card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F0FDF4")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#BBF7D0")),
                ("LINEBEFORE", (0, 0), (0, -1), 3.5, colors.HexColor("#16A34A")),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(st_card)
            story.append(Spacer(1, 5))
        story.append(Spacer(1, 4))

    # =========================================================================
    # CRITICITA' E OPPORTUNITA' RILEVATE
    # =========================================================================
    crit_sec_num = "3" if result.strengths else "2"
    story.append(Paragraph(f"{crit_sec_num}. Criticità e Opportunità Rilevate (in ordine di urgenza)", s_h2))
    if not result.findings:
        story.append(Paragraph("Nessuna criticità rilevante riscontrata.", s_body))
    else:
        for f in result.findings:
            sev_c = SEV_COLORS.get(f.severity, NAVY)
            bg_c = SEV_BG.get(f.severity, LIGHT_BG)
            ev_html = f"<br/><font color='#64748B'><i>Dettaglio tecnico rilevato: {_esc(f.evidence)}</i></font>" if f.evidence else ""
            card_html = (
                f"<b><font color='{sev_c.hexval()}'>[{_esc(f.severity)}]</font> {_esc(f.title)}</b><br/>"
                f"<b>Situazione rilevata:</b> {_esc(f.description)}{ev_html}<br/>"
                f"<b><font color='#991B1B'>Perché è un problema per il tuo ristorante:</font></b> {_esc(f.business_impact)}<br/>"
                f"<b><font color='#1E3A8A'>Come intervenire subito:</font></b> {_esc(f.recommendation)}"
            )
            card = Table([[Paragraph(card_html, s_cell)]], colWidths=[usable_w])
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg_c),
                ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                ("LINEBEFORE", (0, 0), (0, -1), 4.0, sev_c),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(card)
            story.append(Spacer(1, 6))

    story.append(PageBreak())

    # =========================================================================
    # DETTAGLIO TECNICO: SITO WEB & MENU'
    # =========================================================================
    story.append(Paragraph("3. Dettaglio Tecnico: Sito Web &amp; Menù Digitale", s_h2))
    w = result.website_data
    m = result.menu_data

    def _yn(val: bool) -> str:
        return "Si" if val else "No"

    ssl_str = "Valido" if w.get("ssl", {}).get("valid") else ("Non valido / Assente" if w.get("provided") else "N/D")
    if w.get("ssl", {}).get("expires_at"):
        ssl_str += f" (scad. {w['ssl']['expires_at']})"

    tech_rows = [
        [Paragraph("Parametro Sito Web", s_th), Paragraph("Esito", s_th), Paragraph("Parametro Menù Digitale", s_th), Paragraph("Esito", s_th)],
        [Paragraph("URL analizzato", s_cell_b), Paragraph(_esc(w.get("final_url") or "Non fornito"), s_cell),
         Paragraph("Menù rilevato", s_cell_b), Paragraph(_yn(bool(m.get("found"))), s_cell)],
        [Paragraph("Stato HTTP / Tempo", s_cell_b), Paragraph(f"{w.get('status_code') or 'N/D'} ({w.get('load_time_ms') or 0} ms)", s_cell),
         Paragraph("Formato menù", s_cell_b), Paragraph(_esc(str(m.get("format", "N/D")).upper()), s_cell)],
        [Paragraph("HTTPS / SSL", s_cell_b), Paragraph(_esc(ssl_str), s_cell),
         Paragraph("Stato aggiornamento", s_cell_b), Paragraph("OBSOLETO" if m.get("is_outdated") else ("Recente / OK" if m.get("found") else "N/D"), s_cell)],
        [Paragraph("Mobile Viewport", s_cell_b), Paragraph(_yn(bool(w.get("has_viewport"))), s_cell),
         Paragraph("Data PDF / HTTP", s_cell_b), Paragraph(_esc(m.get("pdf_modified") or m.get("last_modified") or str(m.get("estimated_year") or "N/D")), s_cell)],
        [Paragraph("Prenotazione Online", s_cell_b), Paragraph(_yn(bool(w.get("has_booking_cta"))), s_cell),
         Paragraph("Prezzi visibili", s_cell_b), Paragraph(_yn(bool(m.get("has_prices"))), s_cell)],
        [Paragraph("Schema.org Restaurant", s_cell_b), Paragraph(_yn(bool(w.get("has_restaurant_schema"))), s_cell),
         Paragraph("Allergeni citati", s_cell_b), Paragraph(_yn(bool(m.get("has_allergens"))), s_cell)],
        [Paragraph("Telefono / Mappa", s_cell_b), Paragraph(f"Tel: {_yn(bool(w.get('has_phone')))} | Map: {_yn(bool(w.get('has_maps_embed')))}", s_cell),
         Paragraph("PDF solo immagine", s_cell_b), Paragraph(_yn(bool(m.get("is_scanned_image_pdf"))), s_cell)],
        [Paragraph("Link rotti rilevati", s_cell_b), Paragraph(str(len(w.get("broken_links", []))), s_cell),
         Paragraph("Anno Copyright Sito", s_cell_b), Paragraph(str(w.get("copyright_year") or "N/D"), s_cell)],
    ]
    tech_tbl = Table(tech_rows, colWidths=[usable_w * 0.22, usable_w * 0.28, usable_w * 0.22, usable_w * 0.28])
    tech_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tech_tbl)
    story.append(Spacer(1, 10))

    # =========================================================================
    # SOCIAL & RECENSIONI
    # =========================================================================
    story.append(Paragraph("4. Canali Social &amp; Reputazione Online", s_h2))
    soc_ch = result.social_data.get("channels", {})
    rev = result.reviews_data

    soc_rows = [[
        Paragraph("Canale", s_th),
        Paragraph("Presenza", s_th),
        Paragraph("Voto Attività", s_th),
        Paragraph("Ultima Interazione", s_th),
        Paragraph("URL / Presidio", s_th),
    ]]
    source_labels = {
        "modulo": "Fornito",
        "sito_web": "Sito Web",
        "ricerca_web": "Ricerca Web",
        "assente": "Assente",
    }
    for cname in ("instagram", "facebook", "tiktok", "youtube", "whatsapp"):
        ch = soc_ch.get(cname, {})
        raw_src = str(ch.get("source", "assente"))
        rec = ch.get("recency") or {}
        score_val = rec.get("score")
        if not ch.get("present"):
            score_col = "#64748B"
            score_txt = "0/5"
            time_txt = "Non presente"
        elif score_val is not None:
            if score_val >= 4:
                score_col = "#16A34A"
            elif score_val == 3:
                score_col = "#D97706"
            elif score_val >= 1:
                score_col = "#EA580C"
            else:
                score_col = "#DC2626"
            score_txt = rec.get("score_str", f"{score_val}/5")
            if rec.get("days_ago") is not None:
                d_ago = rec["days_ago"]
                if d_ago <= 7:
                    time_txt = f"Ultima settimana ({d_ago} gg fa)"
                elif d_ago <= 35:
                    time_txt = f"{rec.get('weeks_ago', 2)} sett. fa ({d_ago} gg)"
                else:
                    time_txt = f"Oltre 5 sett. fa ({rec.get('weeks_ago', 5)} sett.)"
            else:
                time_txt = rec.get("status_label") or "Data non pubblica"
        else:
            score_col = "#64748B"
            score_txt = "N/D"
            time_txt = "Non rilevata"

        soc_rows.append([
            Paragraph(cname.capitalize(), s_cell_b),
            Paragraph("Presente" if ch.get("present") else "Assente", s_cell),
            Paragraph(f"<font color='{score_col}'><b>{score_txt}</b></font>", ParagraphStyle("ScCell", parent=s_cell, alignment=TA_CENTER)),
            Paragraph(_esc(time_txt), s_small),
            Paragraph(_esc((ch.get("url") or "—")[:48]), s_small),
        ])

    soc_tbl = Table(soc_rows, colWidths=[usable_w * 0.16, usable_w * 0.13, usable_w * 0.13, usable_w * 0.28, usable_w * 0.30])
    soc_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(soc_tbl)
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "<font color='#64748B' size='7'><b>Sistema di voto attività social:</b> 5/5 = post nell'ultima settimana • 4/5 = 2 settimane fa • 3/5 = 3 settimane fa • 2/5 = 4 settimane fa • 1/5 = 5 settimane fa • 0/5 = oltre 5 settimane fa o canale assente.</font>",
        s_small
    ))
    story.append(Spacer(1, 6))

    gprof = rev.get("google_profile", {})
    photo_str = "Presente (Galleria attiva)" if (gprof.get("has_photos") or gprof.get("main_image") or (rev.get("google_reviews_count") or 0) > 20) else (f"{gprof.get('photos_count')} foto" if gprof.get("photos_count") else "Da verificare")
    if gprof.get("photos_count"):
        photo_str = f"Presente ({gprof['photos_count']} foto)"

    fb_bit = f"  •  <b>Facebook:</b> {_esc(rev['facebook_reviews_summary'])}" if rev.get("facebook_reviews_summary") else ""
    rev_summary = (
        f"<b>Valutazione Google:</b> {rev.get('google_rating') or 'N/D'}/5 "
        f"({rev.get('google_reviews_count') or 'N/D'} recensioni)  •  "
        f"<b>Foto Google:</b> {photo_str}  •  "
        f"<b>TripAdvisor:</b> {rev.get('tripadvisor_rating') or 'N/D'}/5{fb_bit}  •  "
        f"<b>Recensioni senza risposta:</b> {rev.get('unanswered_reviews_count') if rev.get('unanswered_reviews_count') is not None else 'N/D'} "
        f"(di cui negative: {rev.get('unanswered_negative_reviews') if rev.get('unanswered_negative_reviews') is not None else 'N/D'})"
    )
    story.append(Paragraph(rev_summary, s_body))
    if rev.get("notes"):
        story.append(Spacer(1, 3))
        story.append(Paragraph(f"<b>Note qualitative sulle recensioni:</b> {_esc(rev['notes'])}", s_small))

    google_revs = rev.get("google_reviews") or []
    if google_revs:
        story.append(Spacer(1, 5))
        pos_c = rev.get("positive_reviews_count", 0)
        neu_c = rev.get("neutral_reviews_count", 0)
        neg_c = rev.get("negative_reviews_count", 0)
        sent_line = (
            f"<b>Analisi automatica ultime {len(google_revs)} recensioni Google:</b> "
            f"<font color='#16A34A'><b>{pos_c} Positive</b></font>  •  "
            f"<font color='#D97706'><b>{neu_c} Neutre</b></font>  •  "
            f"<font color='#DC2626'><b>{neg_c} Negative</b></font>"
        )
        themes = rev.get("complaint_themes") or []
        if themes:
            sent_line += f"<br/><b>Temi critici rilevati nei testi:</b> {_esc(', '.join(themes))}"
        story.append(Paragraph(sent_line, s_body))
        story.append(Spacer(1, 4))
        gr_rows = [[
            Paragraph("Autore / Data", s_th),
            Paragraph("Voto / Esito", s_th),
            Paragraph("Estratto Recensione", s_th),
        ]]
        for gr in google_revs[:5]:
            rating_val = gr.get("rating")
            rating_str = f"{rating_val}/5" if rating_val is not None else "N/D"
            sent = str(gr.get("sentiment") or "neutra").upper()
            r_col = (
                colors.HexColor("#DC2626") if sent == "NEGATIVA"
                else (colors.HexColor("#D97706") if sent == "NEUTRA" else colors.HexColor("#16A34A"))
            )
            author_block = f"<b>{_esc(gr.get('author') or 'Utente Google')}</b>"
            if gr.get("relative_time"):
                author_block += f"<br/><font color='#64748B'>{_esc(gr['relative_time'])}</font>"
            excerpt = (gr.get("text") or "Nessun testo inserito.")[:220]
            if len(gr.get("text") or "") > 220:
                excerpt += "..."
            reply_tag = ""
            if gr.get("has_owner_reply") is True:
                reply_tag = "<br/><font size='6.5' color='#16A34A'>Risposta: Sì</font>"
            elif gr.get("has_owner_reply") is False:
                reply_tag = "<br/><font size='6.5' color='#DC2626'>Senza risposta</font>"
            gr_rows.append([
                Paragraph(author_block, s_small),
                Paragraph(f"<b>{rating_str}</b><br/><font size='7'>{sent}</font>{reply_tag}", ParagraphStyle("GRat", parent=s_cell_b, textColor=r_col, alignment=TA_CENTER)),
                Paragraph(_esc(excerpt), s_small),
            ])
        gr_tbl = Table(gr_rows, colWidths=[usable_w * 0.24, usable_w * 0.14, usable_w * 0.62])
        gr_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(gr_tbl)

    ta_revs = rev.get("tripadvisor_reviews") or []
    if ta_revs:
        story.append(Spacer(1, 6))
        ta_head = f"<b>Ultime recensioni rilevate su TripAdvisor</b>"
        if rev.get("tripadvisor_ranking"):
            ta_head += f" ({_esc(rev['tripadvisor_ranking'])})"
        story.append(Paragraph(ta_head, s_body))
        story.append(Spacer(1, 3))
        ta_rows = [[
            Paragraph("Autore / Data", s_th),
            Paragraph("Voto / Risposta", s_th),
            Paragraph("Estratto Recensione", s_th),
        ]]
        for tr in ta_revs[:5]:
            rv = tr.get("rating")
            r_str = f"{rv}/5" if rv is not None else "N/D"
            rep_str = "Risposta: Sì" if tr.get("has_owner_reply") else "Senza risposta"
            rep_col = colors.HexColor("#16A34A") if tr.get("has_owner_reply") else colors.HexColor("#DC2626")
            excerpt = (tr.get("text") or "Nessun testo.")[:210]
            if len(tr.get("text") or "") > 210:
                excerpt += "..."
            ta_rows.append([
                Paragraph(f"<b>{_esc(tr.get('author') or 'Utente')}</b><br/><font color='#64748B'>{_esc(tr.get('published_date') or '')}</font>", s_small),
                Paragraph(f"<b>{r_str}</b><br/><font size='7' color='{rep_col.hexval()}'>{rep_str}</font>", ParagraphStyle("TRat", parent=s_cell_b, alignment=TA_CENTER)),
                Paragraph(_esc(excerpt), s_small),
            ])
        ta_tbl = Table(ta_rows, colWidths=[usable_w * 0.24, usable_w * 0.16, usable_w * 0.60])
        ta_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(ta_tbl)

    fb_revs = rev.get("facebook_reviews") or []
    if fb_revs:
        story.append(Spacer(1, 6))
        fb_head = "<b>Ultime recensioni rilevate su Facebook</b>"
        if rev.get("facebook_reviews_summary"):
            fb_head += f" ({_esc(rev['facebook_reviews_summary'])})"
        story.append(Paragraph(fb_head, s_body))
        story.append(Spacer(1, 3))
        fb_rows = [[
            Paragraph("Autore / Data", s_th),
            Paragraph("Esito", s_th),
            Paragraph("Estratto Recensione", s_th),
        ]]
        for fr in fb_revs[:5]:
            rec_flag = fr.get("recommends")
            esito_str = "Consiglia" if rec_flag is True else ("Non consiglia" if rec_flag is False else "Recensione")
            esito_col = colors.HexColor("#16A34A") if rec_flag is True else (colors.HexColor("#DC2626") if rec_flag is False else MUTED)
            excerpt = (fr.get("text") or "Nessun testo inserito.")[:210]
            if len(fr.get("text") or "") > 210:
                excerpt += "..."
            fb_rows.append([
                Paragraph(f"<b>{_esc(fr.get('author') or 'Utente Facebook')}</b><br/><font color='#64748B'>{_esc(fr.get('date') or '')}</font>", s_small),
                Paragraph(f"<b>{esito_str}</b>", ParagraphStyle("FRat", parent=s_cell_b, textColor=esito_col, alignment=TA_CENTER)),
                Paragraph(_esc(excerpt), s_small),
            ])
        fb_tbl = Table(fb_rows, colWidths=[usable_w * 0.24, usable_w * 0.16, usable_w * 0.60])
        fb_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(fb_tbl)

    story.append(Spacer(1, 8))

    # =========================================================================
    # PRESENZA SUI MOTORI DI RICERCA E PORTALI WEB
    # =========================================================================
    story.append(Paragraph("5. Presenza sui Motori di Ricerca e Portali Web", s_h2))
    web_records = result.osint_data.get("search_records", [])[:10]
    if web_records:
        o_rows = [[Paragraph("Titolo / Pagina", s_th), Paragraph("URL Rilevato", s_th)]]
        for rec in web_records:
            o_rows.append([
                Paragraph(_esc((rec.get("title") or "")[:65]), s_cell),
                Paragraph(_esc((rec.get("url") or "")[:75]), s_small),
            ])
        o_tbl = Table(o_rows, colWidths=[usable_w * 0.48, usable_w * 0.52])
        o_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(o_tbl)
    else:
        story.append(Paragraph("Nessuna traccia web aggiuntiva rilevata sui motori di ricerca in questa verifica.", s_small))
    story.append(Spacer(1, 10))

    # =========================================================================
    # ANALISI S.W.O.T.
    # =========================================================================
    if result.swot_analysis:
        story.append(Paragraph("6. Analisi S.W.O.T. del Locale", s_h2))
        story.append(Paragraph(
            "L'analisi SWOT valuta la posizione competitiva del ristorante: cosa fanno bene i competitor, dove mostrano debolezze "
            "e come inserirsi nelle quote di mercato disponibili del quartiere.",
            s_small,
        ))
        story.append(Spacer(1, 5))
        swot_cells = []
        # Definizione colori box SWOT
        swot_box_colors = {
            "Strengths": ("#16A34A", "#F0FDF4"),
            "Weaknesses": ("#DC2626", "#FEF2F2"),
            "Opportunities": ("#2563EB", "#EFF6FF"),
            "Threats": ("#D97706", "#FFFBEB"),
        }
        swot_items = list(result.swot_analysis.items())
        # Costruisce griglia 2x2
        row1, row2 = [], []
        for i, (quadrant_title, items) in enumerate(swot_items):
            q_key = quadrant_title.split()[0]
            hdr_col, bg_col = swot_box_colors.get(q_key, ("#0F172A", "#F8FAFC"))
            content = f"<b><font color='{hdr_col}'>{_esc(quadrant_title)}</font></b><br/>"
            for item in items:
                content += f"&bull; {_esc(item)}<br/>"
            p = Paragraph(content, s_small)
            if i < 2:
                row1.append(p)
            else:
                row2.append(p)
        
        if row1:
            swot_tbl = Table([row1, row2], colWidths=[usable_w * 0.50, usable_w * 0.50])
            swot_tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#F0FDF4")),
                ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#FEF2F2")),
                ("BACKGROUND", (0, 1), (0, 1), colors.HexColor("#EFF6FF")),
                ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#FFFBEB")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(swot_tbl)
            story.append(Spacer(1, 10))

    # =========================================================================
    # ISOCRONA & ANALISI QUOTA DI MERCATO COMPETITOR
    # =========================================================================
    if result.market_analysis and result.market_analysis.get("competitors"):
        ma = result.market_analysis
        story.append(Paragraph("7. Analisi Isocrona & Quota di Mercato Competitor (QM)", s_h2))
        iso_min = ma.get("isochrone_minutes", 10)
        pop_target = ma.get("target_clients", 9000)
        occ_qm = ma.get("total_occupied_market_share_pct", 0)
        free_qm = ma.get("available_market_share_pct", 0)

        intro_mkt = (
            f"<b>Bacino Isocrona {iso_min} minuti (in auto):</b> raggio stimato ~{ma.get('reach_radius_km', 3.5)} km • "
            f"Clienti in target stimati: <b>{pop_target:,}</b> (su bacino di {ma.get('estimated_population', 50000):,} ab.)<br/>"
            f"<b>Quota Mercato Totale Occupata dai competitor mappati:</b> <b>{occ_qm}%</b>  •  "
            f"<b>Quota Mercato Libera/Disponibile:</b> <font color='#16A34A'><b>{free_qm}%</b></font>"
        )
        story.append(Paragraph(intro_mkt, s_body))
        story.append(Spacer(1, 5))

        mkt_rows = [[
            Paragraph("Competitor nell'Isocrona", s_th),
            Paragraph("Rating Google", s_th),
            Paragraph("Stima Pax / Giorno", s_th),
            Paragraph("Pax Mensili", s_th),
            Paragraph("Quota Mercato (QM)", s_th),
        ]]

        for c in ma["competitors"]:
            mkt_rows.append([
                Paragraph(f"<b>{_esc(c['name'])}</b>", s_small),
                Paragraph(f"{c.get('rating') or 'N/D'}/5 ({c.get('reviews_count', 0)})", s_small),
                Paragraph(f"{c['est_daily_pax']} pax", s_small),
                Paragraph(f"{c['est_monthly_pax']:,}", s_small),
                Paragraph(f"<b>{c['market_share_pct']}%</b>", ParagraphStyle("QMPct", parent=s_cell_b, textColor=NAVY, alignment=TA_CENTER)),
            ])

        tgt = ma.get("target_restaurant", {})
        if tgt:
            mkt_rows.append([
                Paragraph(f"<b>{_esc(tgt.get('name') or 'Vostro Locale')} (Target)</b>", ParagraphStyle("TgtName", parent=s_small, textColor=colors.HexColor("#C2410C"))),
                Paragraph("—", s_small),
                Paragraph(f"{tgt.get('est_daily_pax', 70)} pax", s_small),
                Paragraph(f"{tgt.get('est_monthly_pax', 2100):,}", s_small),
                Paragraph(f"<b>{tgt.get('potential_market_share_pct', 0)}% (Potenziale)</b>", ParagraphStyle("TgtQM", parent=s_cell_b, textColor=colors.HexColor("#C2410C"), alignment=TA_CENTER)),
            ])

        mkt_tbl = Table(mkt_rows, colWidths=[usable_w * 0.36, usable_w * 0.16, usable_w * 0.16, usable_w * 0.14, usable_w * 0.18])
        mkt_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, LIGHT_BG]),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFF7ED")),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(mkt_tbl)
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "<font color='#64748B' size='7'><b>Formula Quota Mercato:</b> QM = (Stima pax mensili / Clienti in target) &times; 100. "
            "Aiuta a comprendere le crepe e la torta già suddivisa nel quartiere per posizionare l'offerta gastronomica.</font>",
            s_small
        ))
        story.append(Spacer(1, 10))

    # =========================================================================
    # PIANO D'AZIONE 7 / 30 / 90 GIORNI
    # =========================================================================
    sec_count = 6
    if result.swot_analysis:
        sec_count += 1
    if result.market_analysis and result.market_analysis.get("competitors"):
        sec_count += 1
    story.append(Paragraph(f"{sec_count}. Piano d'Azione Consigliato per il Ristorante", s_h2))
    for phase, actions in result.action_plan.items():
        story.append(Paragraph(f"<b>{_esc(phase)}</b>", s_bold))
        for act in actions:
            story.append(Paragraph(f"&bull;  {_esc(act)}", s_body))
        story.append(Spacer(1, 5))

    if inp.get("consultant_contact"):
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER))
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"<b>Prossimo passo:</b> Per approfondire questo report o attivare il piano di miglioramento digitale, contatta <b>{_esc(consultant)}</b> ({_esc(inp['consultant_contact'])}).",
            s_body,
        ))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return pdf_path

