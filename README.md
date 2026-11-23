# RistoAudit — Audit OSINT & Presenza Digitale per Ristoranti

**RistoAudit** è un'applicazione locale Python + HTML che integra i moduli OSINT di [AnonCatalyst/Ominis-OSINT](https://github.com/AnonCatalyst/Ominis-OSINT) (`vendor/Ominis-OSINT`) con un motore di audit tecnico dedicato alla ristorazione per generare un **report PDF esecutivo in italiano** pronto da consegnare al titolare del ristorante.

## Cosa analizza
1. **Sito Web & Esperienza Mobile**: HTTPS/SSL e scadenza certificato, tempi di risposta, mobile viewport, SEO (Title, Meta Description, H1, OpenGraph, Schema.org `Restaurant`), numero di telefono cliccabile, mappa, CTA di prenotazione online, anno copyright, privacy/P.IVA e rilevamento link rotti.
2. **Menù Digitale**: auto-discovery del menù (HTML o PDF), analisi metadati PDF (`CreationDate`, `ModDate`) e header `Last-Modified` per rilevare **menù troppo vecchi**, verifica PDF solo-immagine, presenza prezzi e allergeni.
3. **Presenza Social & Scansione Username (Ominis-OSINT)**: verifica canali Instagram, Facebook, TikTok, YouTube, WhatsApp tramite link del sito + `ominis_src.username` + monitoraggio inattività social.
4. **Recensioni & Reputazione Online**: rilevamento portali (Google Maps, TripAdvisor, TheFork), analisi recensioni senza risposta (totali e negative), valutazione media, supporto opzionale Google Places API.
5. **Ricognizione Web OSINT (Ominis-OSINT)**: query multi-motore (DuckDuckGo + Bing via `ominis_src.search`) per mappare impronta digitale, menzioni e vecchi menù indicizzati.

## Avvio rapido
```bash
cd /Users/lucaperini/Desktop/RistoAudit
python3 app.py --open
```
Poi apri il browser su **http://127.0.0.1:8765**, compila i moduli richiesti e scarica il **Report PDF completo** (salvato anche nella cartella `reports/`).

