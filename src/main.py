"""
main.py - Orchestratie van De Dagkrant.

Dit is het hoofdscript dat alle modules aanstuurt:
1. Nieuwsbrieven ophalen uit Gmail
2. Taal detecteren en Engels vertalen naar Nederlands
3. PDF genereren met voorblad en inhoudsopgave
4. PDF e-mailen naar het werkadres

Schema: ma/wo/do/vr om 16:00 CET.
Maandag  → 72 uur terug (vr 16:00 → ma 16:00, vangt za+zo+ma op)
Woensdag → 48 uur terug (ma 16:00 → wo 16:00, vangt di+wo op)
Donderdag→ 24 uur terug (wo 16:00 → do 16:00)
Vrijdag  → 24 uur terug (do 16:00 → vr 16:00)
"""

import logging
import os
import sys
import tempfile
from datetime import datetime, timezone

from dotenv import load_dotenv

from bs4 import BeautifulSoup
from fetcher import fetch_newsletters
from translator import detect_language, generate_toc_entry, translate_html
from cleaner import clean_html, deduplicate_title, is_website_template, truncate_html_content
from renderer import compose_full_html, render_cover_page, render_pdf, send_email_with_pdf

# Logging configuratie
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dagkrant")

# Printschema: hoeveel uur terugkijken per weekdag
# 0=ma, 1=di, 2=wo, 3=do, 4=vr, 5=za, 6=zo
_HOURS_BACK = {
    0: 72,   # Maandag:   vr 16:00 → ma 16:00 (za + zo + ma)
    2: 48,   # Woensdag:  ma 16:00 → wo 16:00 (di + wo)
    3: 24,   # Donderdag: wo 16:00 → do 16:00
    4: 24,   # Vrijdag:   do 16:00 → vr 16:00
}


def _calculate_hours_back() -> int:
    """
    Bereken hoeveel uur we moeten terugkijken op basis van de huidige weekdag.
    Fallback: 24 uur (voor handmatige runs op andere dagen).
    """
    weekday = datetime.now(timezone.utc).weekday()  # 0=ma ... 6=zo
    hours = _HOURS_BACK.get(weekday, 24)
    logger.info(f"Weekdag {weekday} → {hours} uur terugkijken")
    return hours


def main():
    """Hoofdfunctie: de dirigent die alles aanstuurt."""
    load_dotenv()

    # Configuratie laden
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    target_email = os.getenv("TARGET_EMAIL")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    # Validatie
    missing = []
    if not gmail_user:
        missing.append("GMAIL_USER")
    if not gmail_password:
        missing.append("GMAIL_APP_PASSWORD")
    if not openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not target_email:
        missing.append("TARGET_EMAIL")

    if missing:
        logger.error(f"Ontbrekende environment variables: {', '.join(missing)}")
        logger.error("Maak een .env bestand aan op basis van .env.example")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("DE DAGKRANT - Dagelijkse nieuwsbundel")
    logger.info(f"Datum: {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M UTC')}")
    logger.info("=" * 60)

    # --- Stap 1: Nieuwsbrieven ophalen ---
    hours_back = _calculate_hours_back()
    logger.info(f"\n📬 Stap 1: Nieuwsbrieven ophalen uit Gmail (laatste {hours_back} uur)...")
    newsletters = fetch_newsletters(gmail_user, gmail_password, hours_back=hours_back)

    if not newsletters:
        logger.info(f"Geen nieuwsbrieven gevonden in de laatste {hours_back} uur. Klaar!")
        return

    logger.info(f"{len(newsletters)} nieuwsbrief(ven) gevonden.")

    # Sorteer op datum (nieuwste eerst)
    newsletters.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Dedupliceer per afzender: maximaal 2 artikelen per afzender
    MAX_PER_SENDER = 2
    sender_counts: dict[str, int] = {}
    filtered_by_sender = []
    for nl in newsletters:
        sender = nl.get("sender", "").strip()
        count = sender_counts.get(sender, 0)
        if count < MAX_PER_SENDER:
            filtered_by_sender.append(nl)
            sender_counts[sender] = count + 1
        else:
            logger.info(f"  ⏭ Overgeslagen (max {MAX_PER_SENDER}/afzender): '{nl['subject'][:60]}'")
    newsletters = filtered_by_sender

    # Limiet: maximaal 15 artikelen per PDF om overflow te voorkomen
    MAX_ARTICLES = 15
    if len(newsletters) > MAX_ARTICLES:
        logger.warning(
            f"⚠️ {len(newsletters)} artikelen gevonden, limiet is {MAX_ARTICLES}. "
            f"Oudste {len(newsletters) - MAX_ARTICLES} worden overgeslagen."
        )
        newsletters = newsletters[:MAX_ARTICLES]

    # --- Stap 2-3: Verwerk elke nieuwsbrief individueel ---
    # Elke nieuwsbrief wordt apart verwerkt. Als er iets misgaat,
    # wordt die ene nieuwsbrief overgeslagen en gaat de rest door.
    logger.info("\n🧹 Stap 2-3: Opschonen, dedupliceren, detecteren en vertalen...")
    # Maximum zichtbare woorden per artikel (≈ 3 A4-pagina's)
    MAX_ARTICLE_WORDS = 700

    processed = []
    for i, nl in enumerate(newsletters):
        subject = nl.get("subject", "(Onbekend)")
        try:
            # Stap 2a: Controleer op generieke website-template (vóór cleaning)
            if is_website_template(nl["html_content"]):
                logger.warning(f"  ⚠️ '{subject}' lijkt een website-template — overgeslagen.")
                continue

            # Stap 2b: HTML opschonen
            original_len = len(nl["html_content"])
            nl["html_content"] = clean_html(nl["html_content"])
            cleaned_len = len(nl["html_content"])
            reduction = ((original_len - cleaned_len) / original_len * 100) if original_len > 0 else 0
            logger.info(f"  [{i+1}/{len(newsletters)}] '{subject}' - {reduction:.0f}% rommel verwijderd")

            # Stap 2c: Validatie zichtbare tekst (minimaal 300 tekens)
            # Let op: verwijder <style>-tags vóór get_text() — anders telt CSS-code
            # mee als "zichtbare tekst" en passeren lege newsletters de drempel.
            _check_soup = BeautifulSoup(nl["html_content"], "html.parser")
            for _style_tag in _check_soup.find_all(["style", "link"]):
                _style_tag.decompose()
            visible_text = _check_soup.get_text(strip=True)
            if len(visible_text) < 300:
                logger.warning(
                    f"  ⚠️ '{subject}' heeft te weinig zichtbare tekst "
                    f"({len(visible_text)} tekens) — overgeslagen."
                )
                continue

            # Stap 2c(2): Structurele inhoudscheck — voorkomt dat alleen link-lijsten
            # of minimale fragmenten de PDF halen als visueel lege pagina's.
            # Eis: minstens 1 blok-element met > 60 chars ÓÓGT minstens 2 blokken met > 20 chars.
            _blocks_60 = [
                el for el in _check_soup.find_all(["p", "li", "h1", "h2", "h3", "blockquote", "td"])
                if len(el.get_text(strip=True)) > 60
            ]
            _blocks_20 = [
                el for el in _check_soup.find_all(["p", "li", "h1", "h2", "h3", "blockquote", "td"])
                if len(el.get_text(strip=True)) > 20
            ]
            if not _blocks_60 and len(_blocks_20) < 2:
                logger.warning(
                    f"  ⚠️ '{subject}' mist substantiële tekstblokken "
                    f"(blokken>60: {len(_blocks_60)}, blokken>20: {len(_blocks_20)}) — overgeslagen."
                )
                continue

            # Stap 2d: Dubbele titels verwijderen
            nl["html_content"] = deduplicate_title(nl["html_content"], subject)

            # Stap 3: Taaldetectie + vertaling
            lang = detect_language(nl["html_content"])
            nl["was_translated"] = (lang == "en")
            logger.info(f"    Taal: {lang.upper()}")

            if lang == "en":
                logger.info(f"    Vertalen naar Nederlands...")
                nl["html_content"] = translate_html(nl["html_content"], openai_api_key)
                logger.info(f"    Vertaling voltooid.")

                # Re-valideer na vertaling: AI kan content soms inkorten of weggooien
                _post_soup = BeautifulSoup(nl["html_content"], "html.parser")
                for _s in _post_soup.find_all(["style", "link"]):
                    _s.decompose()
                post_text = _post_soup.get_text(strip=True)
                if len(post_text) < 200:
                    logger.warning(
                        f"  ⚠️ '{subject}' te weinig content na vertaling "
                        f"({len(post_text)} tekens) — overgeslagen."
                    )
                    continue

            # Stap 3b: Trunceer te lange artikelen
            visible_words = len(
                BeautifulSoup(nl["html_content"], "html.parser")
                .get_text(separator=" ", strip=True)
                .split()
            )
            if visible_words > MAX_ARTICLE_WORDS:
                logger.info(
                    f"    Artikel ingekort: {visible_words} woorden → max {MAX_ARTICLE_WORDS}"
                )
                nl["html_content"] = truncate_html_content(nl["html_content"], MAX_ARTICLE_WORDS)

            processed.append(nl)

        except Exception as e:
            logger.error(f"  ❌ FOUT bij verwerken '{subject}': {e}")
            logger.error(f"     Deze nieuwsbrief wordt OVERGESLAGEN, de rest gaat door.")
            continue

    # Vervang de originele lijst door alleen de succesvol verwerkte items
    newsletters = processed
    logger.info(f"\n  {len(newsletters)} van {len(processed) + (len(newsletters) - len(newsletters))} nieuwsbrieven succesvol verwerkt.")

    if not newsletters:
        logger.error("Geen enkele nieuwsbrief kon worden verwerkt. Gestopt.")
        return

    # --- Stap 4: Inhoudsopgave genereren ---
    logger.info("\n📋 Stap 4: Inhoudsopgave genereren...")
    toc_entries = []
    for nl in newsletters:
        try:
            # Geef een content-snippet mee voor betere beschrijvingen
            snippet = ""
            try:
                snippet = (
                    BeautifulSoup(nl["html_content"], "html.parser")
                    .get_text(separator=" ", strip=True)[:500]
                )
            except Exception:
                pass

            toc_data = generate_toc_entry(
                nl["subject"], nl["sender"], openai_api_key,
                content_snippet=snippet,
            )
            toc_entries.append({
                "subject": nl["subject"],
                "sender": nl["sender"],
                "short_title": toc_data["short_title"],
                "description": toc_data["description"],
                "was_translated": nl.get("was_translated", False),
            })
            logger.info(f"  TOC: '{toc_data['short_title']}'")
        except Exception as e:
            logger.error(f"  Fout bij TOC entry voor '{nl['subject']}': {e}")
            toc_entries.append({
                "subject": nl["subject"],
                "sender": nl["sender"],
                "short_title": nl["subject"][:50],
                "description": "",
                "was_translated": nl.get("was_translated", False),
            })

    # --- Stap 5: PDF samenstellen ---
    logger.info("\n📄 Stap 5: PDF genereren...")
    cover_html = render_cover_page(newsletters, toc_entries)
    full_html = compose_full_html(cover_html, newsletters)

    # PDF opslaan in een tijdelijk bestand
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = f.name

    try:
        render_pdf(full_html, pdf_path)
        file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        logger.info(f"PDF grootte: {file_size_mb:.1f} MB")

        # Valideer pagina-telling: verwacht 1 voorblad + 1 pagina per artikel (minimaal).
        # Als de PDF significant korter is, waarschuw dan over mogelijk afgebroken rendering.
        try:
            from pypdf import PdfReader as _PdfReader
            _actual = len(_PdfReader(pdf_path).pages)
            _expected_min = len(newsletters) + 1
            if _actual < _expected_min:
                logger.warning(
                    f"⚠️ PDF heeft {_actual} pagina's, maar er zijn {len(newsletters)} artikelen "
                    f"(verwacht minimaal {_expected_min}). Mogelijk afgebroken rendering! "
                    f"Controleer of Playwright voldoende geheugen/tijd had."
                )
        except Exception:
            pass

        # --- Stap 6: E-mail verzenden ---
        logger.info("\n📧 Stap 6: E-mail verzenden...")
        send_email_with_pdf(
            pdf_path=pdf_path,
            sender_email=gmail_user,
            sender_password=gmail_password,
            recipient_email=target_email,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
        )

        logger.info("\n" + "=" * 60)
        logger.info("DE DAGKRANT IS KLAAR!")
        logger.info(f"Verzonden naar: {target_email}")
        logger.info("=" * 60)

    finally:
        # Tijdelijk PDF bestand opruimen
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)


if __name__ == "__main__":
    main()
