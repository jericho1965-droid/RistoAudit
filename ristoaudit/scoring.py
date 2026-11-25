"""Orchestratore dell'audit, motore di scoring (0-100), punti di forza e criticita' ad alto impatto."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from .models import AuditInput, AuditResult, CategoryScore, Finding
from .email_draft import generate_outreach_email
from .competitor_analysis import (
    calculate_market_share,
    fetch_nearby_competitors,
    fetch_ors_isochrone,
)
from .ominis_bridge import run_ominis_recon
from .reputation_audit import (
    fetch_google_maps_brightdata_reviews,
    fetch_google_places_data,
    run_reputation_and_social_audit,
)
from .website_audit import run_website_and_menu_audit


def _clamp(val: float) -> int:
    return max(0, min(100, int(round(val))))


def _grade_for_score(score: int) -> str:
    if score >= 85:
        return "Eccellente"
    if score >= 70:
        return "Buono"
    if score >= 50:
        return "Migliorabile"
    return "Critico"


def run_full_audit(audit_input: AuditInput) -> AuditResult:
    """Esegue tutte le fasi dell'analisi approfondita e costruisce il report strutturato."""
    audit_id = uuid.uuid4().hex[:10]
    findings: list[Finding] = []
    strengths: list[Finding] = []
    warnings: list[str] = []

    # 0. Google Places API (New): recupera scheda, rating, recensioni e sito web ufficiale se presente
    gp_data = fetch_google_places_data(
        name=audit_input.restaurant_name,
        city=audit_input.city,
        address=audit_input.address,
        google_maps_url=audit_input.google_maps_url,
        google_place_id=audit_input.google_place_id,
        api_key=audit_input.google_places_api_key,
    )
    if gp_data:
        if not audit_input.website_url.strip() and gp_data.get("website_uri"):
            audit_input.website_url = gp_data["website_uri"]
        if not audit_input.address.strip() and gp_data.get("formatted_address"):
            audit_input.address = gp_data["formatted_address"]
        if not audit_input.google_maps_url.strip() and gp_data.get("maps_uri"):
            audit_input.google_maps_url = gp_data["maps_uri"]

    # Avvia SUBITO Bright Data Google Maps in background mentre girano Sito Web e Menù (guadagna +15s gratis!)
    bg_pool = ThreadPoolExecutor(max_workers=1)
    prefetched_bd_google_future = None
    # Preferisce maps_uri (URL con cid) da Google Places che supera al 100% la validazione di categoria di Bright Data
    bd_google_target_url = ""
    if gp_data and gp_data.get("maps_uri"):
        bd_google_target_url = gp_data["maps_uri"].strip()
    elif audit_input.google_maps_url.strip():
        bd_google_target_url = audit_input.google_maps_url.strip()
    elif gp_data and gp_data.get("place_id"):
        bd_google_target_url = f"https://www.google.com/maps/place/?q=place_id:{gp_data['place_id']}"

    if bd_google_target_url and audit_input.brightdata_api_key.strip():
        prefetched_bd_google_future = bg_pool.submit(
            fetch_google_maps_brightdata_reviews,
            bd_google_target_url,
            audit_input.brightdata_api_key,
        )

    # 1. Ricerca Web Approfondita PRIMA del sito web: scopre sito ufficiale, menu indicizzati, social e portali!
    osint_data = run_ominis_recon(
        restaurant_name=audit_input.restaurant_name,
        city=audit_input.city,
        social_handle=audit_input.social_handle,
        run_search=audit_input.run_ominis_search,
        run_username=audit_input.run_ominis_username,
    )
    if osint_data.get("errors"):
        warnings.extend(osint_data["errors"][:5])

    # Se il sito web non era fornito ne' presente su Google, usa quello scoperto dalla ricerca web!
    if not audit_input.website_url.strip() and osint_data.get("discovered_website_url"):
        audit_input.website_url = osint_data["discovered_website_url"]

    osint_menu_candidates = [h.get("url", "") for h in osint_data.get("menu_osint_hits", []) if h.get("url")]

    # 2. Analisi Tecnica Sito Web & Menù Digitale (con fallback sui menu trovati sul web)
    website_data, menu_data = run_website_and_menu_audit(
        website_url=audit_input.website_url,
        menu_url=audit_input.menu_url,
        known_menu_year=audit_input.known_menu_year,
        deep_link_check=audit_input.deep_link_check,
        osint_menu_candidates=osint_menu_candidates,
    )

    # 3. Social + Recensioni & Reputazione
    try:
        social_data, reviews_data = run_reputation_and_social_audit(
            audit_input=audit_input,
            website_data=website_data,
            osint_data=osint_data,
            gp_data=gp_data,
            prefetched_bd_google_future=prefetched_bd_google_future,
        )
    finally:
        bg_pool.shutdown(wait=False)
    print(
        f"[RistoAudit] SUMMARY '{audit_input.restaurant_name}': "
        f"GP={reviews_data.get('google_places_api_used')}({len(gp_data.get('reviews') or [])}r) | "
        f"BD_G={reviews_data.get('google_brightdata_used')}({len(reviews_data.get('google_reviews') or [])}r) | "
        f"TA={reviews_data.get('tripadvisor_terra_used')}({len(reviews_data.get('tripadvisor_reviews') or [])}r) | "
        f"BD_FB={reviews_data.get('facebook_brightdata_used')}({len(reviews_data.get('facebook_reviews') or [])}r) | "
        f"Site={website_data.get('final_url') or 'NONE'} | Menu={menu_data.get('found')}"
    )

    # =========================================================================
    # 1. SITO WEB & ESPERIENZA MOBILE (peso 25%)
    # =========================================================================
    web_score = 100
    if not website_data["provided"]:
        web_score = 15
        findings.append(Finding(
            category="website",
            severity="CRITICO",
            title="Assenza di un sito web ufficiale rilevabile online",
            description=(
                f"Dalle verifiche effettuate su Google e sul web non risulta alcun sito web ufficiale collegato a "
                f"{audit_input.restaurant_name} ({audit_input.city})."
            ),
            business_impact=(
                "Ogni giorno decine di clienti e turisti cercano il ristorante su Google prima di decidere dove cenare: "
                "senza un sito web autorevole e ben indicizzato, fino al 40-50% di questi utenti abbandona la scelta "
                "e prenota in un altro locale della zona che mostra subito ambiente, piatti e bottone di prenotazione."
            ),
            recommendation=(
                "Realizzare un sito web veloce, mobile-first e orientato alle prenotazioni dirette, completo di galleria "
                "fotografica, menù digitale interattivo, mappa, orari e collegamento diretto con Google Business Profile."
            ),
        ))
    elif not website_data["reachable"]:
        web_score = 10
        findings.append(Finding(
            category="website",
            severity="CRITICO",
            title="Sito web irraggiungibile o bloccato da errore server",
            description=(
                f"Il sito web {website_data['url']} ha restituito un errore tecnico "
                f"({website_data.get('error') or 'HTTP ' + str(website_data.get('status_code'))}) impedendo l'accesso ai visitatori."
            ),
            business_impact=(
                "Per un cliente che clicca dal proprio smartphone, un sito offline equivale a trovare la saracinesca abbassata: "
                "comunica immediatamente chiusura definitiva o grave trascuratezza e dirotta le prenotazioni alla concorrenza."
            ),
            recommendation="Ripristinare immediatamente hosting, certificato SSL e configurazione DNS, attivando un monitoraggio continuo.",
            evidence=website_data.get("error") or f"HTTP {website_data.get('status_code')}",
        ))
    else:
        # Punti di forza Sito Web
        if website_data["uses_https"] and website_data.get("ssl", {}).get("valid", True):
            strengths.append(Finding(
                category="website",
                severity="OTTIMO",
                title="Sito web attivo e protetto con protocollo sicuro HTTPS",
                description=(
                    f"Il sito ufficiale ({website_data['final_url']}) è regolarmente raggiungibile e protetto da certificato SSL valido"
                    + (f" (scadenza {website_data['ssl']['expires_at']})." if website_data.get("ssl", {}).get("expires_at") else ".")
                ),
                business_impact="Garantisce sicurezza ai visitatori su Chrome/Safari ed evita penalizzazioni di base su Google.",
                recommendation="Mantenere attivo il rinnovo automatico del certificato SSL.",
            ))
        if website_data["has_viewport"] and website_data["load_time_ms"] <= 2000:
            strengths.append(Finding(
                category="website",
                severity="OTTIMO",
                title=f"Buona velocità di risposta ({website_data['load_time_ms']} ms) e supporto mobile",
                description="Il sito risponde rapidamente e include la configurazione viewport per l'adattamento agli schermi smartphone.",
                business_impact="Riduce l'abbandono immediato dei clienti che consultano il ristorante da telefono prima di uscire a cena.",
                recommendation="Continuare a ottimizzare le immagini dei piatti per preservare la rapidità di caricamento.",
            ))

        # Criticità Sito Web
        if not website_data["uses_https"] or not website_data.get("ssl", {}).get("valid", True):
            web_score -= 22
            findings.append(Finding(
                category="website",
                severity="CRITICO",
                title="Sito segnalato come 'Non Sicuro' (Certificato SSL/HTTPS mancante o scaduto)",
                description=(
                    f"Il dominio {website_data['final_url']} non utilizza una connessione HTTPS valida. "
                    "I browser moderni mostrano una schermata di allarme rosso o la dicitura 'Non sicuro' nella barra degli indirizzi."
                ),
                business_impact=(
                    "Oltre il 70% degli utenti chiude immediatamente una pagina segnalata come non sicura. "
                    "Inoltre Google penalizza drasticamente i siti senza HTTPS nei risultati di ricerca locali a " + audit_input.city + "."
                ),
                recommendation="Installare subito un certificato SSL valido e impostare il reindirizzamento permanente da HTTP a HTTPS.",
            ))
        if not website_data["has_viewport"]:
            web_score -= 18
            findings.append(Finding(
                category="website",
                severity="ALTO",
                title="Sito non ottimizzato per smartphone (esperienza mobile compromessa)",
                description="Nella pagina principale manca il meta tag viewport necessario per adattare testi, foto e pulsanti agli schermi dei telefoni.",
                business_impact=(
                    "Nella ristorazione oltre l'80% delle ricerche avviene da smartphone, spesso mentre il cliente è già in giro. "
                    "Dover fare zoom continuo per leggere orari o telefono fa perdere prenotazioni in meno di 10 secondi."
                ),
                recommendation="Aggiornare il layout con un design responsive mobile-first e pulsanti grandi cliccabili con il pollice.",
            ))
        if website_data["load_time_ms"] > 2500:
            web_score -= 10
            findings.append(Finding(
                category="website",
                severity="MEDIO",
                title=f"Tempi di caricamento troppo lenti ({website_data['load_time_ms']} ms)",
                description=f"La homepage ha impiegato {website_data['load_time_ms']} millisecondi solo per la risposta iniziale (soglia consigliata < 1.500 ms).",
                business_impact=(
                    "Secondo gli studi Google, oltre la metà degli utenti mobile abbandona un sito se impiega più di 3 secondi ad aprirsi: "
                    "significa perdere clienti interessati prima ancora che vedano le foto del locale."
                ),
                recommendation="Ottimizzare peso delle immagini (formato WebP), attivare sistemi di caching e verificare le prestazioni del server.",
            ))
        if not website_data["has_booking_cta"]:
            web_score -= 12
            findings.append(Finding(
                category="website",
                severity="ALTO",
                title="Nessun sistema di prenotazione online o pulsante 'Prenota' immediato",
                description="In homepage non è stato rilevato un sistema di prenotazione tavolo digitale (form dedicato, WhatsApp Business o gestionale sala).",
                business_impact=(
                    "Sempre più clienti (soprattutto giovani, famiglie e turisti stranieri) prenotano fuori orario di servizio o evitano di telefonare. "
                    "Non offrire una prenotazione in 2 clic significa regalare coperti ogni settimana ai ristoranti vicini."
                ),
                recommendation="Inserire un pulsante ben visibile 'Prenota un tavolo' in alto nel sito, collegato a WhatsApp Business o a un sistema senza commissioni.",
            ))
        if not website_data["has_phone"]:
            web_score -= 10
            findings.append(Finding(
                category="website",
                severity="ALTO",
                title="Numero di telefono non trovato o non cliccabile da smartphone",
                description="Non è stato rilevato un numero di telefono formattato come link diretto (tel:) nella pagina principale.",
                business_impact="Chi vuole chiamare al volo per un tavolo last-minute si innervosisce se deve copiare e incollare il numero manualmente.",
                recommendation="Posizionare il numero di telefono cliccabile nell'intestazione del sito e nel footer.",
            ))
        if not website_data["meta_description"] or not website_data["title"]:
            web_score -= 8
            findings.append(Finding(
                category="website",
                severity="MEDIO",
                title="Tag SEO fondamentali incompleti (Title o Meta Description mancanti)",
                description="Mancano il titolo SEO o la descrizione breve che Google mostra nei risultati di ricerca.",
                business_impact=(
                    f"Quando un utente cerca 'ristorante a {audit_input.city}', un risultato senza descrizione curata "
                    "ottiene molti meno clic rispetto ai concorrenti che presentano subito specialità e punti di forza."
                ),
                recommendation=f"Scrivere Title e Meta Description persuasivi includendo nome del locale, cucina tipica, zona e invito a prenotare a {audit_input.city}.",
            ))
        if not website_data["has_restaurant_schema"]:
            web_score -= 8
            findings.append(Finding(
                category="website",
                severity="MEDIO",
                title="Dati strutturati Google per Ristoranti (Schema.org) assenti",
                description="Il codice del sito non contiene il markup JSON-LD 'Restaurant / LocalBusiness' usato da Google per capire tipo di cucina, orari, fascia di prezzo e menù.",
                business_impact=(
                    "Senza dati strutturati il ristorante perde posizionamento nelle ricerche locali di Google e nelle risposte degli assistenti vocali/AI."
                ),
                recommendation="Integrare il codice JSON-LD Schema.org/Restaurant completo di indirizzo, telefono, orari, cucina e link al menù.",
            ))
        if website_data["copyright_outdated"]:
            web_score -= 6
            findings.append(Finding(
                category="website",
                severity="MEDIO",
                title=f"Sito visibilmente datato (Copyright fermo al {website_data['copyright_year']})",
                description=f"Nel piè di pagina compare ancora l'anno {website_data['copyright_year']} come ultimo riferimento di aggiornamento.",
                business_impact=(
                    "Un sito fermo a diversi anni fa genera subito il dubbio che prezzi, orari o addirittura la gestione del locale non siano più attuali."
                ),
                recommendation="Rinfrescare l'anno di copyright, le foto stagionali e i contenuti principali del sito.",
            ))
        if website_data["broken_links"]:
            web_score -= min(15, 5 * len(website_data["broken_links"]))
            sample_bad = ", ".join(f"{b['url']} (HTTP {b['status']})" for b in website_data["broken_links"][:3])
            findings.append(Finding(
                category="website",
                severity="ALTO",
                title=f"Rilevati {len(website_data['broken_links'])} collegamenti rotti (pagine 404/errore) sul sito",
                description="Alcuni link presenti nelle pagine del sito o verso i social puntano a indirizzi inesistenti o in errore.",
                business_impact=(
                    "Un link rotto interrompe bruscamente la navigazione del cliente proprio mentre sta cercando informazioni per prenotare, "
                    "danneggiando sia la credibilità del locale sia il posizionamento su Google."
                ),
                recommendation="Riparare o sostituire immediatamente tutti i collegamenti interrotti rilevati.",
                evidence=sample_bad,
            ))
        if not website_data["has_privacy_cookie"]:
            web_score -= 5
            findings.append(Finding(
                category="website",
                severity="BASSO",
                title="Informativa Privacy, Cookie Policy o Partita IVA non chiaramente visibili",
                description="Nel footer della homepage non risultano chiaramente rilevabili i riferimenti legali obbligatori.",
                business_impact="Espone l'attività a possibili contestazioni e sanzioni amministrative per mancata conformità GDPR e normativa italiana.",
                recommendation="Inserire nel footer Partita IVA, ragione sociale, Privacy Policy e Cookie Policy aggiornate.",
            ))

    web_score = _clamp(web_score)

    # =========================================================================
    # 2. MENÙ DIGITALE & AGGIORNAMENTO (peso 20%)
    # =========================================================================
    menu_score = 100
    if not menu_data["found"]:
        menu_score = 20
        findings.append(Finding(
            category="menu",
            severity="CRITICO",
            title="Menù digitale non trovato o non consultabile online",
            description="Non è stato possibile individuare un menù consultabile direttamente dal sito web o dai principali risultati di ricerca.",
            business_impact=(
                "Il menù è in assoluto la pagina più visitata del sito di un ristorante (oltre il 75% dei visitatori lo apre prima di prenotare). "
                "Non trovarlo blocca la decisione di chi ha preferenze alimentari, budget definito o sta scegliendo per un gruppo."
            ),
            recommendation="Pubblicare un menù digitale mobile-friendly direttamente sul sito e collegarlo nella scheda Google Business Profile.",
        ))
    else:
        # Punti di forza Menù & Verifica Anzianità esplicita
        menu_date_label = menu_data.get("pdf_modified") or menu_data.get("last_modified") or (
            str(menu_data["estimated_year"]) if menu_data.get("estimated_year") else ""
        )
        if not menu_data["is_outdated"]:
            if menu_data.get("age_months") is not None:
                strengths.append(Finding(
                    category="menu",
                    severity="OTTIMO",
                    title=f"Menù digitale raggiungibile e aggiornato di recente ({menu_date_label})",
                    description=(
                        f"Il menù online ({menu_data['url']}, formato {str(menu_data['format']).upper()}) risulta aggiornato "
                        f"circa {menu_data['age_months']} mesi fa ({menu_date_label})"
                        + (", con prezzi visibili" if menu_data.get("has_prices") else "")
                        + (" e allergeni indicati." if menu_data.get("has_allergens") else ".")
                    ),
                    business_impact="Un menù aggiornato elimina il rischio di sorprese su prezzi o piatti al tavolo e aumenta la fiducia di chi prenota.",
                    recommendation="Mantenere questa buona pratica aggiornando il menù a ogni cambio stagione.",
                ))
            else:
                strengths.append(Finding(
                    category="menu",
                    severity="OTTIMO",
                    title=f"Menù digitale raggiungibile online ({str(menu_data['format']).upper()})",
                    description=(
                        f"Il menù è stato individuato e risulta accessibile online ({menu_data['url']})"
                        + (" con prezzi espressi chiaramente" if menu_data.get("has_prices") else "")
                        + (" e indicazione allergeni." if menu_data.get("has_allergens") else ".")
                    ),
                    business_impact="Permette ai clienti di scoprire l'offerta gastronomica prima di prenotare, aumentando la conversione.",
                    recommendation="Assicurarsi che ogni cambio stagionale di piatti o prezzi venga aggiornato contestualmente anche online.",
                ))
                menu_score -= 6
                findings.append(Finding(
                    category="menu",
                    severity="MEDIO",
                    title="Data di aggiornamento del menù online non rilevabile (anzianità non verificabile)",
                    description=(
                        "La pagina del menù non espone una data di ultimo aggiornamento (né nei metadati del sito né nel testo visibile)."
                    ),
                    business_impact=(
                        "Quando il cliente non vede la stagione o l'anno del menù online, teme che prezzi o piatti siano vecchi e non aggiornati alla sala, "
                        "riducendo la fiducia prima della prenotazione."
                    ),
                    recommendation="Indicare chiaramente in cima o in fondo al menù la dicitura 'Menù aggiornato a [Mese/Stagione Anno]' e aggiornare la pagina a ogni variazione di listino.",
                ))

        if menu_data["is_outdated"]:
            menu_score -= 35
            age_desc = (
                f"circa {menu_data['age_months']} mesi fa"
                if menu_data.get("age_months") is not None
                else f"anno stimato {menu_data.get('estimated_year')}"
            )
            findings.append(Finding(
                category="menu",
                severity="CRITICO",
                title="Menù digitale obsoleto / non aggiornato da troppo tempo",
                description=f"I metadati del documento o i riferimenti nel testo indicano che il menù online risale a {age_desc}.",
                business_impact=(
                    "Un menù vecchio è una delle prime cause di recensioni negative da 1-2 stelle: quando il cliente arriva al tavolo "
                    "e trova piatti non più disponibili o prezzi più alti rispetto a quelli visti online, si sente ingannato."
                ),
                recommendation="Aggiornare immediatamente il menù online con listino e piatti attuali, eliminando i vecchi file PDF ancora indicizzati.",
                evidence=f"Data PDF: {menu_data.get('pdf_modified') or 'N/D'} | Aggiornamento server: {menu_data.get('last_modified') or 'N/D'}",
            ))
        if menu_data["format"] == "pdf":
            menu_score -= 10
            if menu_data["is_scanned_image_pdf"]:
                menu_score -= 20
                findings.append(Finding(
                    category="menu",
                    severity="ALTO",
                    title="Menù in PDF scansionato (solo immagine: scomodo da telefono e invisibile a Google)",
                    description="Il file PDF del menù non contiene testo leggibile ma solo pagine fotografate o scansionate.",
                    business_impact=(
                        "Su smartphone costringe il cliente a pizzicare e ingrandire continuamente lo schermo, rendendo frustrante la lettura. "
                        "Inoltre Google non riesce a leggere i nomi dei vostri piatti tipici, facendovi perdere ricerche locali."
                    ),
                    recommendation="Trasformare il menù in una pagina web HTML mobile-first con categorie chiare, prezzi leggibili e foto dei piatti di punta.",
                ))
            else:
                findings.append(Finding(
                    category="menu",
                    severity="BASSO",
                    title="Menù pubblicato solo come file PDF da scaricare",
                    description="Il menù è consultabile esclusivamente tramite download/apertura di un file PDF anziché come pagina web nativa.",
                    business_impact="Da smartphone un PDF è meno fluido di una pagina web dedicata e si indicizza peggio su Google per i singoli piatti.",
                    recommendation="Affiancare al PDF una pagina menù HTML ottimizzata per smartphone e facile da aggiornare in 2 minuti.",
                ))
        elif menu_data["format"] == "image_only":
            menu_score -= 25
            findings.append(Finding(
                category="menu",
                severity="ALTO",
                title="Menù composto solo da immagini (non leggibile dai motori di ricerca)",
                description="La pagina del menù contiene pochissimo testo reale e si basa quasi solo su immagini caricate.",
                business_impact="Lento da caricare con connessione mobile, difficile da leggere e invisibile alle ricerche Google per singoli piatti.",
                recommendation="Pubblicare il menù in formato testuale HTML strutturato con sezioni chiare.",
            ))
        if not menu_data["has_prices"]:
            menu_score -= 12
            findings.append(Finding(
                category="menu",
                severity="MEDIO",
                title="Prezzi non visibili nel menù online",
                description="Nel testo del menù online non risultano indicati chiaramente i prezzi delle portate.",
                business_impact=(
                    "La mancanza di prezzi genera diffidenza: molti potenziali clienti presumono che il locale sia troppo caro "
                    "e scelgono un ristorante concorrente più trasparente."
                ),
                recommendation="Esporre con chiarezza i prezzi aggiornati di tutte le portate, bevande principali e coperto.",
            ))
        if not menu_data["has_allergens"]:
            menu_score -= 10
            findings.append(Finding(
                category="menu",
                severity="MEDIO",
                title="Indicazione allergeni e opzioni alimentari non rilevata nel menù",
                description="Nel menù digitale non risultano riferimenti chiari agli allergeni (Reg. UE 1169/2011) o a opzioni senza glutine/vegetariane.",
                business_impact=(
                    "Basta una sola persona celiaca, intollerante o vegetariana in una tavolata da 6-8 persone per far spostare l'intera prenotazione "
                    "verso un locale che comunica chiaramente allergeni e alternative."
                ),
                recommendation="Aggiungere la legenda allergeni completa ed evidenziare opzioni senza glutine, senza lattosio e vegetariane.",
            ))

    menu_score = _clamp(menu_score)

    # =========================================================================
    # 3. PRESENZA SOCIAL & ATTIVITÀ (peso 20%)
    # =========================================================================
    social_score = 100
    channels = social_data["channels"]
    present_socials = [c.capitalize() for c in ("instagram", "facebook", "tiktok", "youtube") if channels[c]["present"]]

    # Punti di forza Social (dettagliati!)
    if channels["instagram"]["present"] and channels["facebook"]["present"]:
        strengths.append(Finding(
            category="social",
            severity="OTTIMO",
            title=f"Presidio attivo sui principali canali social ({', '.join(present_socials)})",
            description=(
                f"Il ristorante è presente con profili ufficiali su {', '.join(present_socials)} "
                f"(Instagram: {channels['instagram']['url']} | Facebook: {channels['facebook']['url']})."
            ),
            business_impact=(
                "Avere sia Instagram (vetrina visiva per piatti, reel e pubblico 18-45) sia Facebook (pubblico locale 35+, eventi e famiglie) "
                "costituisce una base solida per generare passaparola digitale e fidelizzare i clienti."
            ),
            recommendation="Potenziare la costanza editoriale con brevi Reel dietro le quinte, piatti stagionali e link diretto alla prenotazione in bio.",
        ))
    elif present_socials:
        strengths.append(Finding(
            category="social",
            severity="OTTIMO",
            title=f"Presenza social avviata su {', '.join(present_socials)}",
            description=f"È stato rilevato un presidio ufficiale attivo su {', '.join(present_socials)}.",
            business_impact="Offre già un canale diretto per mostrare piatti e atmosfera del locale ai potenziali clienti.",
            recommendation="Completare l'ecosistema collegando i profili al sito web e mantenendo una pubblicazione regolare.",
        ))

    ig_rec = channels.get("instagram", {}).get("recency") or {}
    fb_rec = channels.get("facebook", {}).get("recency") or {}

    # Punti di forza recency / attività social (voti 4/5 o 5/5)
    if channels.get("instagram", {}).get("present") and ig_rec.get("score", 0) >= 4:
        strengths.append(Finding(
            category="social",
            severity="OTTIMO",
            title=f"Canale Instagram attivo e aggiornato ({ig_rec.get('score_str', '5/5')})",
            description=f"Ultima attività rilevata {ig_rec.get('description', '')}.",
            business_impact="Mantiene vivo l'interesse dei clienti abituali e attira nuovi avventori mostrando con costanza i piatti e l'atmosfera del locale.",
            recommendation="Mantenere questo ritmo di pubblicazione alternando Reel su piatti signature, dietro le quinte e stories quotidiane.",
        ))

    if channels.get("facebook", {}).get("present") and fb_rec.get("score", 0) >= 4:
        strengths.append(Finding(
            category="social",
            severity="OTTIMO",
            title=f"Pagina Facebook attiva e presidiata ({fb_rec.get('score_str', '5/5')})",
            description=f"Ultima attività rilevata {fb_rec.get('description', '')}.",
            business_impact="Fidelizza il pubblico locale e le famiglie per pranzi domenicali, ricorrenze e cene di gruppo.",
            recommendation="Continuare a condividere eventi, novità del menù e rispondere tempestivamente alle interazioni della community.",
        ))

    # Criticità Social: Presenza di base
    if not channels["instagram"]["present"]:
        social_score -= 30
        findings.append(Finding(
            category="social",
            severity="CRITICO",
            title="Profilo Instagram ufficiale non rilevato",
            description="Non è stato individuato un profilo Instagram ufficiale associato al ristorante.",
            business_impact=(
                "Oggi Instagram è il vero 'menù visivo' della ristorazione: migliaia di persone scelgono dove andare a cena guardando foto e brevi video dei piatti. "
                "Non esserci rende il locale invisibile a tutta la fascia 20-45 anni e ai turisti."
            ),
            recommendation="Creare un profilo Instagram Business curato con bio chiara, link prenotazione/menù, storie in evidenza e 2-3 contenuti a settimana.",
        ))
    if not channels["facebook"]["present"]:
        social_score -= 20
        findings.append(Finding(
            category="social",
            severity="MEDIO",
            title="Pagina Facebook ufficiale non rilevata",
            description="Non risulta una pagina Facebook ufficiale attiva per il ristorante.",
            business_impact="Si perde visibilità sul pubblico locale over 35, ideale per pranzi domenicali, cene di gruppo, ricorrenze e menù festivi.",
            recommendation="Attivare la Pagina Facebook e collegarla a Instagram tramite Meta Business Suite per pubblicare in un clic su entrambi.",
        ))
    if not channels["tiktok"]["present"]:
        social_score -= 10
        findings.append(Finding(
            category="social",
            severity="BASSO",
            title="Assenza su TikTok (canale ad altissima visibilità organica gratuita)",
            description="Non risulta un profilo TikTok attivo per il locale.",
            business_impact=(
                f"I video brevi di 15-30 secondi (preparazione piatti, impasto pizza, impiattamento) geolocalizzati su {audit_input.city} "
                "raggiungono gratuitamente migliaia di utenti locali senza dover investire in pubblicità."
            ),
            recommendation=f"Ripubblicare gli stessi brevi Reel di Instagram anche su TikTok con hashtag e geolocalizzazione su {audit_input.city}.",
        ))

    # Criticità Social: Tempo trascorso dall'ultima interazione (Sistema di voto 0-5)
    # Instagram Recency
    if channels.get("instagram", {}).get("present"):
        ig_score = ig_rec.get("score")
        if ig_score == 0:
            social_score -= 25
            findings.append(Finding(
                category="social",
                severity="CRITICO",
                title=f"Profilo Instagram inattivo da oltre 5 settimane (Punteggio attività: 0/5)",
                description=(
                    f"L'ultima attività o interazione rilevata su Instagram risale a "
                    f"{ig_rec.get('weeks_ago', 'oltre 5')} settimane fa ({ig_rec.get('days_ago', 'N/D')} giorni fa). "
                    f"Il canale non registra pubblicazioni recenti."
                ),
                business_impact=(
                    "Un profilo social fermo da più di un mese è percepito negativamente dai clienti: molti pensano che il ristorante abbia chiuso, "
                    "abbia cambiato gestione o non curi l'offerta. Inoltre l'algoritmo di Meta penalizza drasticamente la visibilità organica "
                    "dei profili inattivi, azzerando la portata verso follower e nuovi clienti."
                ),
                recommendation=(
                    "Riattivare subito il canale con almeno 2-3 post/reel a settimana e stories quotidiane per risalire al punteggio di 5/5 "
                    "e recuperare visibilità nell'algoritmo locale."
                ),
            ))
        elif ig_score in (1, 2):
            social_score -= 15
            findings.append(Finding(
                category="social",
                severity="ALTO",
                title=f"Frequenza di pubblicazione discontinua su Instagram ({ig_rec.get('weeks_ago', 4)} settimane fa • Punteggio: {ig_score}/5)",
                description=(
                    f"L'ultimo post o interazione rilevata su Instagram risale a circa {ig_rec.get('weeks_ago', 4)} settimane fa "
                    f"({ig_rec.get('days_ago', 'N/D')} giorni fa)."
                ),
                business_impact="La pubblicazione a singhiozzo azzera la copertura organica e non crea fidelizzazione nei clienti che cercano dove andare a cena nel weekend.",
                recommendation="Pianificare una routine editoriale fissa (almeno 2 contenuti a settimana) per raggiungere la fascia di voto 5/5.",
            ))

    # Facebook Recency
    if channels.get("facebook", {}).get("present"):
        fb_score = fb_rec.get("score")
        if fb_score == 0:
            social_score -= 20
            findings.append(Finding(
                category="social",
                severity="CRITICO",
                title=f"Pagina Facebook inattiva da oltre 5 settimane (Punteggio attività: 0/5)",
                description=(
                    f"L'ultima attività o interazione rilevata sulla pagina Facebook risale a "
                    f"{fb_rec.get('weeks_ago', 'oltre 5')} settimane fa ({fb_rec.get('days_ago', 'N/D')} giorni fa)."
                ),
                business_impact="Si interrompe il dialogo con la clientela locale e il pubblico over 35, che usa Facebook per informarsi su orari, festività e pranzi della domenica.",
                recommendation="Sincronizzare la Pagina Facebook a Instagram tramite Meta Business Suite per ripubblicare i contenuti in automatico e tornare a 5/5.",
            ))
        elif fb_score in (1, 2):
            social_score -= 10
            findings.append(Finding(
                category="social",
                severity="MEDIO",
                title=f"Pubblicazione Facebook irregolare ({fb_rec.get('weeks_ago', 4)} settimane fa • Punteggio: {fb_score}/5)",
                description=f"L'ultima attività su Facebook risale a circa {fb_rec.get('weeks_ago', 4)} settimane fa ({fb_rec.get('days_ago', 'N/D')} giorni fa).",
                business_impact="La bassa frequenza di aggiornamento riduce la fiducia dei clienti e le visualizzazioni del menù.",
                recommendation="Mantenere un calendario costante collegando la condivisione diretta tra Instagram e Facebook.",
            ))

    if website_data["reachable"] and not any(channels[c]["linked_on_website"] for c in ("instagram", "facebook")):
        social_score -= 10
        findings.append(Finding(
            category="social",
            severity="MEDIO",
            title="Profili social non collegati sul sito web",
            description="Nel sito web non sono stati rilevati link cliccabili verso le pagine Instagram o Facebook del locale.",
            business_impact="Si spezza il percorso del cliente: chi visita il sito non riesce a vedere foto recenti e atmosfera del locale sui social.",
            recommendation="Inserire le icone cliccabili dei profili social nell'intestazione, nel footer e nella pagina contatti del sito.",
        ))

    social_score = _clamp(social_score)

    # =========================================================================
    # 4. RECENSIONI & REPUTAZIONE ONLINE (peso 20%)
    # =========================================================================
    rev_score = 100
    unanswered = reviews_data.get("unanswered_reviews_count")
    unanswered_neg = reviews_data.get("unanswered_negative_reviews")
    g_rating = reviews_data.get("google_rating")
    g_count = reviews_data.get("google_reviews_count")
    praise_themes = reviews_data.get("praise_themes") or []
    gprof = reviews_data.get("google_profile") or {}

    # Punti di forza Recensioni & Reputazione (dettagliati!)
    if g_rating is not None and g_rating >= 4.2:
        praise_str = f" I clienti apprezzano in particolare: {', '.join(praise_themes)}." if praise_themes else ""
        strengths.append(Finding(
            category="reviews",
            severity="OTTIMO",
            title=f"Ottima reputazione media su Google ({g_rating:.1f}/5" + (f" su {g_count} recensioni)" if g_count else ")"),
            description=(
                f"Il ristorante gode di una valutazione media superiore alla soglia di eccellenza locale ({g_rating:.1f}/5).{praise_str}"
            ),
            business_impact=(
                "Una media sopra 4.2 stelle con un buon volume di recensioni è il principale motore di fiducia su Google Maps "
                "e permette di convertire le ricerche locali in tavoli prenotati."
            ),
            recommendation="Valorizzare le migliori recensioni anche sul sito web e sui social, continuando a stimolare feedback dai clienti soddisfatti.",
        ))
    elif reviews_data.get("positive_reviews_count", 0) >= 3:
        praise_str = f" Punti più lodati: {', '.join(praise_themes)}." if praise_themes else ""
        strengths.append(Finding(
            category="reviews",
            severity="OTTIMO",
            title=f"Prevalenza di recensioni positive tra le più recenti su Google ({reviews_data['positive_reviews_count']} su {len(reviews_data.get('google_reviews') or [])})",
            description=f"La maggior parte delle recensioni pubbliche in evidenza su Google è positiva (4-5 stelle).{praise_str}",
            business_impact="Rafforza la prima impressione di chi consulta la scheda Google Maps prima di prenotare.",
            recommendation="Ringraziare pubblicamente i clienti che lasciano recensioni positive per stimolare il ritorno in sala.",
        ))

    if reviews_data["tripadvisor_url"] or reviews_data["thefork_url"]:
        portals = [p for p, u in (("TripAdvisor", reviews_data["tripadvisor_url"]), ("TheFork", reviews_data["thefork_url"])) if u]
        strengths.append(Finding(
            category="reviews",
            severity="OTTIMO",
            title=f"Presenza rilevata sui portali di ristorazione ({', '.join(portals)})",
            description=f"Il ristorante è presente e indicizzato su {', '.join(portals)}, canali chiave per turisti e prenotazioni online.",
            business_impact="Amplia la visibilità del locale oltre Google Maps, intercettando pubblico turistico e nuovi clienti in città.",
            recommendation="Mantenere aggiornati foto, orari e risposte alle recensioni anche su questi portali.",
        ))

    if reviews_data.get("google_places_api_used") and gprof.get("has_opening_hours") and gprof.get("has_phone"):
        strengths.append(Finding(
            category="reviews",
            severity="OTTIMO",
            title="Scheda Google Maps completa di orari e telefono",
            description="La scheda Google Business Profile espone correttamente gli orari di apertura e il recapito telefonico.",
            business_impact="Facilita le chiamate dirette e le indicazioni stradali per chi cerca il ristorante da smartphone.",
            recommendation="Verificare periodicamente la coerenza degli orari in occasione di festività o chiusure straordinarie.",
        ))

    # Criticità Recensioni
    if not reviews_data["google_maps_url"] and g_rating is None:
        rev_score -= 20
        findings.append(Finding(
            category="reviews",
            severity="ALTO",
            title="Scheda Google Business Profile non verificata o non collegata correttamente",
            description="Non è stato possibile collegare in modo completo i dati della scheda Google Maps del ristorante.",
            business_impact=(
                "Google Maps genera fino al 60-70% delle nuove scoperte di un ristorante: una scheda incompleta o non presidiata "
                "taglia fuori il locale dalle prime 3 posizioni sulla mappa della città."
            ),
            recommendation="Rivendicare e ottimizzare al 100% la scheda Google Business Profile (categorie, orari, foto, link sito e menù).",
        ))

    if unanswered is not None and unanswered > 0:
        rev_score -= min(25, 10 + (10 if unanswered >= 10 else unanswered))
        findings.append(Finding(
            category="reviews",
            severity="CRITICO" if unanswered >= 5 else "ALTO",
            title=f"Mancata risposta alle recensioni online ({unanswered} recensioni senza replica)",
            description=(
                f"Tra le recensioni pubbliche analizzate risultano ben {unanswered} recensioni a cui la direzione non ha dato riscontro. "
                "Ogni recensione lasciata senza risposta rappresenta una grave criticità: rispondere a una recensione positiva consolida "
                "la fedeltà del cliente soddisfatto stimolando il ritorno, mentre rispondere a una critica permette di recuperare "
                "un cliente insoddisfatto prima che sia perso per sempre."
            ),
            business_impact=(
                "Lasciare recensioni senza risposta comunica disinteresse e scarsa attenzione all'esperienza del cliente: "
                "oltre l'80% dei potenziali clienti legge le risposte del proprietario prima di scegliere dove mangiare. "
                "Rispondere a una recensione positiva fidelizza e crea ambasciatori del locale; rispondere a una critica argina danni di reputazione "
                "e converte un'insoddisfazione in un'opportunità di riscatto."
            ),
            recommendation="Introdurre una routine settimanale per replicare puntualmente a tutte le recensioni: ringraziare calorosamente chi lascia un giudizio positivo e affrontare con massima cura, empatia e trasparenza ogni critica.",
        ))

    if unanswered_neg is not None and unanswered_neg > 0:
        rev_score -= min(35, 15 + unanswered_neg * 4)
        findings.append(Finding(
            category="reviews",
            severity="CRITICO",
            title=f"{unanswered_neg} recensioni negative lasciate senza alcuna risposta",
            description=f"Risultano {unanswered_neg} recensioni negative alle quali la direzione del ristorante non ha mai replicato.",
            business_impact=(
                "Rispondere a una recensione negativa significa cercare di recuperare un cliente che altrimenti andrebbe perso per sempre "
                "e dimostrare agli oltre 8 clienti su 10 che leggono le critiche prima di prenotare che la direzione ascolta e sa risolvere i problemi. "
                "Lasciare una recensione negativa senza risposta equivale a confermare la critica davanti a centinaia di futuri clienti ogni mese."
            ),
            recommendation="Rispondere entro 48 ore a tutte le recensioni negative con tono empatico, professionale e orientato alla soluzione (mai difensivo), invitando il cliente a tornare per una nuova esperienza.",
        ))

    if g_rating is not None and g_rating < 4.2:
        rev_score -= 20 if g_rating < 3.8 else 12
        findings.append(Finding(
            category="reviews",
            severity="ALTO" if g_rating < 3.8 else "MEDIO",
            title=f"Valutazione media Google sotto la soglia competitiva ({g_rating:.1f}/5)",
            description=f"Il punteggio medio su Google ({g_rating:.1f}/5) è inferiore alla soglia di 4.3+ stelle ricercata dagli utenti.",
            business_impact=(
                "Su Google Maps moltissimi utenti attivano il filtro 'Valutazione 4.5+' o scartano automaticamente i locali sotto 4.2: "
                "anche solo +0.3 stelle possono aumentare sensibilmente chiamate e prenotazioni."
            ),
            recommendation="Introdurre un sistema semplice in sala (es. QR code su scontrino/tavolo) per invitare i clienti soddisfatti a lasciare una recensione reale.",
        ))

    if g_count is not None and g_count < 50:
        rev_score -= 10
        findings.append(Finding(
            category="reviews",
            severity="MEDIO",
            title=f"Volume di recensioni ancora ridotto ({g_count} recensioni su Google)",
            description="Il numero totale di recensioni è contenuto rispetto ai principali concorrenti della zona.",
            business_impact="Un basso numero di recensioni riduce la forza della prova sociale e penalizza il posizionamento nelle ricerche locali.",
            recommendation="Avviare una raccolta continuativa di recensioni dai clienti abituali e soddisfatti.",
        ))

    if reviews_data.get("facebook_brightdata_used"):
        fb_pct = reviews_data.get("facebook_recommendation_pct")
        fb_yes = reviews_data.get("facebook_recommended_count", 0)
        fb_no = reviews_data.get("facebook_not_recommended_count", 0)
        fb_revs = reviews_data.get("facebook_reviews") or []
        fb_unans = sum(1 for r in fb_revs if not r.get("has_owner_reply"))
        if fb_pct is not None and fb_pct >= 85 and fb_yes >= 3:
            strengths.append(Finding(
                category="reviews",
                severity="OTTIMO",
                title=f"Ottimo indice di gradimento su Facebook ({fb_pct}% consiglia il locale)",
                description=f"Tra le ultime recensioni analizzate su Facebook, {fb_yes} clienti consigliano il ristorante (contro {fb_no} non consigliato).",
                business_impact="Un alto tasso di raccomandazione su Facebook rafforza la fiducia del pubblico locale e favorisce il passaparola.",
                recommendation="Continuare a interagire con i commenti e valorizzare le migliori recensioni anche nei post della pagina.",
            ))
        elif fb_no >= 2 or (fb_pct is not None and fb_pct < 80):
            rev_score -= 12
            findings.append(Finding(
                category="reviews",
                severity="ALTO" if fb_no >= 3 else "MEDIO",
                title=f"Recensioni critiche rilevate su Facebook ({fb_no} 'Non consiglia')",
                description=f"Nell'analisi delle ultime recensioni Facebook emergono {fb_no} valutazioni negative ('Non consiglia').",
                business_impact="Su Facebook le recensioni negative restano ben visibili agli amici e ai contatti locali, frenando il passaparola in città.",
                recommendation="Rispondere in modo cordiale e costruttivo a ogni recensione negativa su Facebook e stimolare i clienti soddisfatti a lasciare un riscontro.",
            ))
        if fb_revs and fb_unans >= 2:
            rev_score -= 6
            findings.append(Finding(
                category="reviews",
                severity="MEDIO",
                title=f"Recensioni Facebook senza risposta dalla pagina ({fb_unans} su {len(fb_revs)} recenti)",
                description=f"Tra le ultime {len(fb_revs)} recensioni pubblicate su Facebook, {fb_unans} non mostrano una risposta o interazione da parte del ristorante.",
                business_impact=(
                    "Rispondere a una recensione positiva significa fidelizzare un cliente che probabilmente tornerà e generare passaparola "
                    "nel feed dei suoi amici su Facebook; rispondere a una negativa significa invece cercare di recuperare un cliente che altrimenti andrebbe perso per sempre."
                ),
                recommendation="Rispondere regolarmente anche alle recensioni su Facebook (ringraziando per nome chi consiglia il locale e gestendo con empatia eventuali segnalazioni).",
            ))

    if reviews_data.get("tripadvisor_terra_used"):
        ta_rat = reviews_data.get("tripadvisor_rating")
        ta_cnt = reviews_data.get("tripadvisor_reviews_count")
        ta_rk = reviews_data.get("tripadvisor_ranking")
        ta_unans = reviews_data.get("tripadvisor_unanswered_recent")
        if ta_rat is not None and ta_rat >= 4.0:
            rk_str = f" ({ta_rk})" if ta_rk else ""
            strengths.append(Finding(
                category="reviews",
                severity="OTTIMO",
                title=f"Solida valutazione su TripAdvisor ({ta_rat:.1f}/5" + (f" su {ta_cnt} recensioni)" if ta_cnt else ")"),
                description=f"Su TripAdvisor il ristorante mantiene una media di {ta_rat:.1f}/5{rk_str}, dato molto rilevante per il pubblico turistico.",
                business_impact="Una scheda TripAdvisor ben posizionata porta prenotazioni spontanee di turisti e visitatori con scontrino medio alto.",
                recommendation="Continuare a rispondere regolarmente alle recensioni su TripAdvisor mantenendo aggiornate foto e descrizione.",
            ))
        elif ta_rat is not None and ta_rat < 4.0:
            rev_score -= 10
            findings.append(Finding(
                category="reviews",
                severity="MEDIO",
                title=f"Valutazione TripAdvisor sotto la media competitiva ({ta_rat:.1f}/5)",
                description=f"Il punteggio medio rilevato su TripAdvisor è {ta_rat:.1f}/5" + (f" ({ta_rk})." if ta_rk else "."),
                business_impact="I turisti su TripAdvisor tendono a scartare i locali sotto le 4 bolle, scegliendo i primi classificati in zona.",
                recommendation="Intervenire sui temi critici emersi nelle recensioni recenti e rispondere pubblicamente con cura a ogni recensione.",
            ))
        if ta_unans is not None and ta_unans >= 2:
            rev_score -= 12
            findings.append(Finding(
                category="reviews",
                severity="ALTO",
                title=f"{ta_unans} recensioni recenti su TripAdvisor prive di risposta della direzione",
                description=f"Tra le ultime {len(reviews_data.get('tripadvisor_reviews') or [])} recensioni pubblicate su TripAdvisor, {ta_unans} non hanno ricevuto risposta dal ristorante.",
                business_impact=(
                    "Rispondere a una recensione positiva fidelizza un cliente soddisfatto che probabilmente tornerà o consiglierà il ristorante, "
                    "mentre rispondere a una recensione negativa permette di cercare di recuperare un cliente che altrimenti andrebbe perso per sempre "
                    "e dimostra cura e professionalità ai nuovi viaggiatori su TripAdvisor."
                ),
                recommendation="Impostare una risposta personalizzata per tutte le nuove recensioni su TripAdvisor entro 48-72 ore.",
            ))

    if not reviews_data["tripadvisor_url"] and not reviews_data["thefork_url"]:
        rev_score -= 12
        findings.append(Finding(
            category="reviews",
            severity="MEDIO",
            title="Presenza non rilevata su TripAdvisor e TheFork",
            description="Dalle ricerche online non risultano schede ufficiali del ristorante su TripAdvisor né su TheFork.",
            business_impact=(
                f"In una città come {audit_input.city}, turisti e nuovi clienti usano intensamente TripAdvisor e TheFork per scoprire e prenotare ristoranti: "
                "non presidiare questi canali significa perdere una fetta preziosa di coperti ogni settimana."
            ),
            recommendation="Rivendicare gratuitamente la scheda TripAdvisor (aggiornando foto e descrizione) e valutare l'attivazione strategica su TheFork.",
        ))
    elif not reviews_data["thefork_url"]:
        rev_score -= 7
        findings.append(Finding(
            category="reviews",
            severity="MEDIO",
            title="Assenza su TheFork (canale di prenotazione online non presidiato)",
            description=f"Non risulta una scheda attiva per {audit_input.restaurant_name} su TheFork.",
            business_impact=(
                f"Migliaia di clienti e turisti a {audit_input.city} usano TheFork come primo strumento per prenotare un tavolo in pochi secondi. "
                "Non essere presenti lascia campo libero ai ristoranti concorrenti della zona, soprattutto nelle serate infrasettimanali."
            ),
            recommendation="Valutare l'inserimento su TheFork per intercettare nuovi clienti e riempire i tavoli nelle fasce orarie o giornate meno affollate.",
        ))
    elif not reviews_data["tripadvisor_url"]:
        rev_score -= 7
        findings.append(Finding(
            category="reviews",
            severity="MEDIO",
            title="Scheda TripAdvisor non rilevata",
            description=f"Non è stata individuata una scheda ristorante verificabile su TripAdvisor per {audit_input.restaurant_name}.",
            business_impact=(
                f"TripAdvisor rimane uno dei primi portali consultati dai turisti italiani e stranieri in visita a {audit_input.city}: "
                "l'assenza riduce la visibilità verso una clientela ad alta capacità di spesa."
            ),
            recommendation="Creare o rivendicare gratuitamente la scheda TripAdvisor curando foto, descrizione multilingua e risposte alle recensioni.",
        ))

    if reviews_data.get("google_places_api_used"):
        if gprof.get("has_opening_hours") is False:
            rev_score -= 10
            findings.append(Finding(
                category="reviews",
                severity="ALTO",
                title="Orari di apertura mancanti sulla scheda Google Maps",
                description="La scheda Google del ristorante non mostra gli orari di apertura settimanali.",
                business_impact="I clienti che cercano un locale 'aperto ora' vengono esclusi automaticamente dai risultati di Google Maps.",
                recommendation="Compilare subito orari di pranzo, cena, giorno di chiusura e aperture festive su Google Business Profile.",
            ))
        # Foto e contenuti visivi su Google Maps
        has_g_photos = gprof.get("has_photos") or bool(gprof.get("main_image")) or ((reviews_data.get("google_reviews_count") or 0) > 20)
        if has_g_photos:
            p_desc = "La scheda Google Maps dispone di immagine principale e galleria fotografica curata con scatti del locale e dei piatti."
            if gprof.get("photos_count"):
                p_desc = f"La scheda Google Maps dispone di immagine principale e galleria fotografica attiva ({gprof['photos_count']} foto rilevate)."
            strengths.append(Finding(
                category="reviews",
                severity="OTTIMO",
                title="Galleria fotografica attiva e visibile su Google Maps",
                description=p_desc,
                business_impact="Le schede Google Maps con foto curate generano fino al 42% in più di richieste di indicazioni stradali e visite al sito web.",
                recommendation="Continuare a caricare periodicamente nuove foto in alta risoluzione dei piatti del menù stagionale e dell'ambiente.",
            ))
        elif gprof.get("has_photos") is False and (gprof.get("photos_count") == 0) and not gprof.get("main_image") and (reviews_data.get("google_reviews_count") or 0) < 5:
            rev_score -= 8
            findings.append(Finding(
                category="reviews",
                severity="MEDIO",
                title="Galleria fotografica scarna o assente su Google Maps",
                description="La scheda Google dispone di pochissime fotografie ufficiali o ne risulta priva.",
                business_impact="Le schede prive di foto di piatti e sala subiscono un forte calo di interesse da parte di turisti e nuovi clienti.",
                recommendation="Caricare almeno 15-20 foto luminose e aggiornate di piatti signature, interni, esterno e staff.",
            ))
        if not gprof.get("has_phone"):
            rev_score -= 8
            findings.append(Finding(
                category="reviews",
                severity="ALTO",
                title="Numero di telefono mancante sulla scheda Google Maps",
                description="Non risulta alcun numero di telefono pubblico sulla scheda Google del ristorante.",
                business_impact="Blocca le chiamate immediate con un tap da smartphone per prenotazioni in giornata.",
                recommendation="Aggiungere il numero di telefono principale nella scheda Google Business Profile.",
            ))
        if gprof.get("reservable") is False:
            rev_score -= 5
            findings.append(Finding(
                category="reviews",
                severity="BASSO",
                title="Prenotazione diretta non attiva su Google Maps",
                description="La scheda Google non espone il pulsante di prenotazione diretta del tavolo.",
                business_impact="Riduce la conversione immediata degli utenti che vorrebbero riservare un tavolo direttamente da Google Maps.",
                recommendation="Collegare un link prenotazione nella scheda Google Business Profile.",
            ))

    rev_score = _clamp(rev_score)

    # Assicura che la tabella "Presenza sui Motori di Ricerca e Portali Web" elenchi sempre gli asset reali verificati
    # anche quando lo scraping motori e' disattivato/bloccato su cloud (es. Render).
    existing_rec_urls = {r.get("url", "").rstrip("/") for r in osint_data.get("search_records", [])}
    verified_assets = [
        (f"Sito Web Ufficiale — {audit_input.restaurant_name}", website_data.get("final_url") if website_data.get("reachable") else ""),
        (f"Menù Digitale — {audit_input.restaurant_name}", menu_data.get("url") if menu_data.get("found") else ""),
        (f"Google Maps — {audit_input.restaurant_name}", reviews_data.get("google_maps_url")),
        (f"TripAdvisor — {audit_input.restaurant_name}", reviews_data.get("tripadvisor_url")),
        (f"TheFork — {audit_input.restaurant_name}", reviews_data.get("thefork_url")),
        (f"Instagram — {audit_input.restaurant_name}", social_data["channels"]["instagram"].get("url")),
        (f"Facebook — {audit_input.restaurant_name}", social_data["channels"]["facebook"].get("url")),
        (f"TikTok — {audit_input.restaurant_name}", social_data["channels"]["tiktok"].get("url")),
    ]
    for label, asset_url in verified_assets:
        u = str(asset_url or "").strip()
        if u and u.rstrip("/") not in existing_rec_urls:
            existing_rec_urls.add(u.rstrip("/"))
            osint_data.setdefault("search_records", []).append({
                "title": label,
                "url": u,
                "engine": "verificato",
                "snippet": "",
            })

    # =========================================================================
    # 5. VISIBILITÀ WEB & LOCAL SEO (peso 15%)
    # =========================================================================
    osint_score = 100
    records_count = len(osint_data.get("search_records", []))
    username_found = sum(s.get("found_count", 0) for s in osint_data.get("username_scans", []))

    web_search_ran = bool(osint_data.get("queries_executed")) and not osint_data.get("errors")
    if audit_input.run_ominis_search and web_search_ran and records_count < 4:
        osint_score -= 30
        findings.append(Finding(
            category="osint",
            severity="ALTO",
            title=f"Scarsa presenza indicizzata sui motori di ricerca ({records_count} risultati rilevati)",
            description=f"Le verifiche online hanno restituito pochissime citazioni pubbliche per {audit_input.restaurant_name} a {audit_input.city}.",
            business_impact=(
                "Una bassa presenza su portali gastronomici, guide locali e directory riduce l'autorevolezza del ristorante agli occhi di Google, "
                "abbassando il posizionamento rispetto ai concorrenti della stessa zona."
            ),
            recommendation="Uniformare Nome, Indirizzo e Telefono (NAP) su Google, Apple Maps, TripAdvisor, PagineGialle e portali locali di " + audit_input.city + ".",
        ))
    elif records_count >= 8:
        strengths.append(Finding(
            category="osint",
            severity="OTTIMO",
            title=f"Buona visibilità sul web ({records_count} pagine e portali indicizzati)",
            description=f"Il ristorante è ben presente in {records_count} fonti pubbliche, directory e portali online legati a {audit_input.city}.",
            business_impact="Una solida impronta digitale rafforza l'autorevolezza del brand sui motori di ricerca e facilita la scoperta organica.",
            recommendation="Verificare periodicamente che telefono, orari e menù siano identici e aggiornati su tutti i portali emersi.",
        ))

    if audit_input.run_ominis_username and username_found == 0 and social_data["active_core_count"] < 2:
        osint_score -= 15
        findings.append(Finding(
            category="osint",
            severity="MEDIO",
            title="Nome profilo / brand non uniforme tra le piattaforme",
            description="Non è emerso un nome utente coerente e facilmente riconoscibile tra i diversi canali digitali.",
            business_impact="Quando i clienti faticano a trovare il profilo esatto per taggare il locale nelle storie, si perde passaparola gratuito.",
            recommendation="Scegliere un unico handle semplice e coerente (@nomeristorante) da utilizzare su tutti i canali.",
        ))

    if website_data["reachable"] and not website_data["has_sitemap_xml"]:
        osint_score -= 10

    osint_score = _clamp(osint_score)

    # =========================================================================
    # PUNTEGGIO GLOBALE & SINTESI ESECUTIVA
    # =========================================================================
    categories = [
        CategoryScore("website", "Sito Web & Esperienza Mobile", web_score, 0.25,
                      "Sicurezza HTTPS, velocità, mobile, prenotazione online e SEO tecnica."),
        CategoryScore("menu", "Menù Digitale & Aggiornamento", menu_score, 0.20,
                      "Accessibilità del menù, aggiornamento, leggibilità da smartphone, prezzi e allergeni."),
        CategoryScore("social", "Presenza Social & Attività", social_score, 0.20,
                      "Presidio Instagram/Facebook/TikTok, costanza editoriale e integrazione con il sito."),
        CategoryScore("reviews", "Recensioni & Reputazione", rev_score, 0.20,
                      "Media voto, gestione delle recensioni, completezza scheda Google e portali di settore."),
        CategoryScore("osint", "Visibilità Web & Local SEO", osint_score, 0.15,
                      "Impronta digitale su motori di ricerca, directory e coerenza del brand online."),
    ]

    overall_score = _clamp(sum(c.score * c.weight for c in categories))
    overall_grade = _grade_for_score(overall_score)

    sev_rank = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "BASSO": 3}
    findings.sort(key=lambda f: (sev_rank.get(f.severity, 9), f.category))

    crit_count = sum(1 for f in findings if f.severity == "CRITICO")
    high_count = sum(1 for f in findings if f.severity == "ALTO")

    executive_summary = (
        f"L'analisi della presenza digitale condotta su {audit_input.restaurant_name} ({audit_input.city}) ha registrato un indice di "
        f"salute digitale pari a {overall_score}/100 ({overall_grade}), con {len(strengths)} punti di forza già consolidati, "
        f"{crit_count} criticità prioritarie e {high_count} aree di miglioramento ad alto impatto. "
        f"Valorizzare ciò che già funziona e risolvere rapidamente le criticità emerse permette di recuperare prenotazioni perse ogni settimana, "
        f"migliorare la visibilità locale su Google e trasformare chi cerca il locale online in clienti seduti al tavolo."
    )

    quick_wins = [f.recommendation for f in findings if f.severity == "CRITICO"][:4]
    if not quick_wins:
        quick_wins = ["Verificare l'aggiornamento di orari, foto e link al menù su Google Business Profile."]

    medium_term = [f.recommendation for f in findings if f.severity == "ALTO"][:4]
    if not medium_term:
        medium_term = ["Avviare una routine settimanale di risposta a tutte le recensioni e pubblicazione social."]

    long_term = [f.recommendation for f in findings if f.severity in ("MEDIO", "BASSO")][:4]
    if not long_term:
        long_term = ["Monitorare trimestralmente la reputazione online e introdurre campagne locali stagionali."]

    # =========================================================================
    # ANALISI ISOCRONA & COMPETITOR (OpenRouteService + Google Places)
    # =========================================================================
    market_analysis: dict[str, Any] = {}
    lat = None
    lng = None
    if gp_data and gp_data.get("location"):
        lat = gp_data["location"].get("latitude")
        lng = gp_data["location"].get("longitude")

    if lat and lng:
        ors_info = fetch_ors_isochrone(
            lat=lat,
            lng=lng,
            api_key=audit_input.openrouteservice_api_key.strip(),
            minutes=10,
        )
        competitors = fetch_nearby_competitors(
            restaurant_name=audit_input.restaurant_name,
            city=audit_input.city,
            cuisine_type=audit_input.cuisine_type,
            lat=lat,
            lng=lng,
            google_places_api_key=audit_input.google_places_api_key.strip(),
            radius_meters=int(ors_info.get("reach_radius_km", 3.5) * 1000) if ors_info else 2000,
            max_competitors=5,
        )
        market_analysis = calculate_market_share(
            isochrone_info=ors_info,
            competitors=competitors,
            target_restaurant_name=audit_input.restaurant_name,
            target_reviews_count=reviews_data.get("google_reviews_count"),
            estimated_population=50000,
        )

    # Costruzione Analisi S.W.O.T. basata sui dati raccolti e il contesto competitivo
    swot_strengths = [s.title for s in strengths[:4]]
    if not swot_strengths:
        swot_strengths = ["Identità di cucina e offerta gastronomica radicata nel territorio."]

    swot_weaknesses = [f.title for f in findings if f.severity in ("CRITICO", "ALTO")][:4]
    if not swot_weaknesses:
        swot_weaknesses = ["Margine di miglioramento nella tempestività di risposta e copertura recensioni."]

    swot_opportunities = []
    if market_analysis.get("available_market_share_pct"):
        swot_opportunities.append(
            f"Quota di Mercato libera del {market_analysis['available_market_share_pct']}% nell'isocrona di 10 min: "
            "spazio per attrarre nuovi clienti con posizionamento locale mirato."
        )
    if any(f.category == "reviews" for f in findings):
        swot_opportunities.append("Intercettare la clientela delusa dai competitor rispondendo al 100% delle recensioni con cura ed empatia.")
    if any(f.category in ("website", "menu") for f in findings):
        swot_opportunities.append("Offrire un menù digitale rapido e prenotazioni dirette dal sito web per evitare commissioni e attrarre turisti.")
    if any(f.category == "social" for f in findings):
        swot_opportunities.append("Sfruttare la discontinuità social dei locali vicini con un piano editoriale costante su piatti signature e dietro le quinte.")
    if not swot_opportunities:
        swot_opportunities.append("Valorizzare i piatti signature sui canali digitali per differenziarsi dai competitor dell'area.")
    swot_opportunities = swot_opportunities[:4]

    swot_threats = []
    if market_analysis.get("competitors"):
        top_comp = market_analysis["competitors"][0]
        swot_threats.append(
            f"Forte presenza di competitor storici nell'isocrona (es. {top_comp['name']} con QM potenziale stimata al {top_comp['market_share_pct']}%)."
        )
    swot_threats.extend([
        "Competitor diretti di zona con strategie attive su Google Maps e posizionamento sponsorizzato.",
        "Aumento delle aspettative dei clienti su rapidità di prenotazione online e trasparenza dei prezzi prima di arrivare al locale.",
    ])
    swot_threats = swot_threats[:3]

    swot_analysis = {
        "Strengths (Punti di Forza)": swot_strengths,
        "Weaknesses (Debolezze)": swot_weaknesses,
        "Opportunities (Opportunità di Mercato)": swot_opportunities,
        "Threats (Minacce e Competitor)": swot_threats,
    }

    result = AuditResult(
        audit_id=audit_id,
        input_data=asdict(audit_input),
        overall_score=overall_score,
        overall_grade=overall_grade,
        executive_summary=executive_summary,
        category_scores=categories,
        strengths=strengths,
        findings=findings,
        website_data=website_data,
        menu_data=menu_data,
        social_data=social_data,
        reviews_data=reviews_data,
        osint_data=osint_data,
        action_plan={
            "Interventi Immediati (Entro 7 giorni)": quick_wins,
            "Ottimizzazione Operativa (Entro 30 giorni)": medium_term,
            "Crescita & Consolidamento (Entro 90 giorni)": long_term,
        },
        swot_analysis=swot_analysis,
        market_analysis=market_analysis,
        warnings=warnings,
    )
    result.email_draft = generate_outreach_email(result)
    return result
