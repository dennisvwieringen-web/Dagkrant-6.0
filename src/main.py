"""
main.py - Orchestratie van De Dagkrant.

Dit is het hoofdscript dat alle modules aanstuurt:
1. Nieuwsbrieven ophalen uit Gmail
2. Taal detecteren en Engels vertalen naar Nederlands
3. PDF genereren met voorblad en inhoudsopgave
4. PDF e-mailen naar het werkadres + Kindle

Schema: ma/wo/do/vr, richttijd krant klaar om 15:00 CEST (lokale Taakplanner
triggert de cloud-run om 14:30). Elke run → 72 uur terugkijken; de
verzonden-administratie (Actions-cache) voorkomt duplicaten tussen edities.
"""

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from bs4 import BeautifulSoup
from fetcher import fetch_newsletters, fetch_article_urls
from web_article import fetch_article
from translator import (
    OpenAIUnavailableError,
    detect_language,
    generate_toc_entry,
    translate_html,
)
from cleaner import clean_html, minimal_clean, deduplicate_title, is_website_template, strip_ai_artifacts
from renderer import compose_full_html, render_cover_page, render_pdf, send_email_with_pdf
from readwise import save_html_document

# Logging configuratie
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dagkrant")

# Regex voor het detecteren van display:none in inline styles
_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


def _get_truly_visible_text(html: str) -> str:
    """
    Meet de daadwerkelijk zichtbare tekst van HTML — zoals Chromium het rendert.

    Verwijdert vóór het tellen:
    - <style> en <link> tags (CSS telt niet als tekst)
    - Elementen met display:none (hidden preview text in email-templates)
    - Non-breaking spaces (&nbsp; / \\xa0) die als layout-spacers dienen

    Dit voorkomt dat emails met veel verborgen tekst of &nbsp;-padding
    de minimumdrempel passeren en als lege pagina's in de PDF verschijnen.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["style", "link"]):
        tag.decompose()
    for tag in soup.find_all(style=_DISPLAY_NONE_RE):
        tag.decompose()
    text = soup.get_text(strip=True)
    # Strip non-breaking spaces (veelgebruikt als layout-spacer in email-tabellen)
    text = text.replace("\xa0", "").strip()
    return text

# Terugkijkvenster: 72 uur (i.p.v. 24) zodat handmatig gelabelde mails die pas
# een dag of twee na ontvangst hun label krijgen alsnog worden meegenomen. De
# verzonden-administratie (zie _load_seen/_save_seen) voorkomt dat een mail
# die al in een eerdere editie zat opnieuw verstuurd wordt.
_HOURS_BACK = 72


def _calculate_hours_back() -> int:
    """Retourneer het aantal uur terugkijken (72; dedup via verzonden-administratie)."""
    logger.info(f"{_HOURS_BACK} uur terugkijken")
    return _HOURS_BACK


# ── Verzonden-administratie ────────────────────────────────────────────────
# JSON-bestand met twee onderdelen: "message_ids" ({message-id-of-url:
# iso-datum}) voor exacte dubbelingen, en "content" (lijst van
# inhoud-fingerprints) voor artikelen die WEL een nieuw bericht zijn maar
# grotendeels dezelfde tekst herhalen — bv. Readwise's "Sunday Favorites" die
# highlights van eerder die week hergroepeert, of een nieuwsbrief die zijn
# eigen stuk van een dag eerder samenvat. Message-ID-dedup ving dat niet:
# andere afzender-mail, ander onderwerp, dus "nieuw" volgens die check.
# In GitHub Actions leeft dit in de Actions-cache (zie dagkrant.yml, key
# dagkrant-seen-*); lokaal in logs/ (gitignored). Alleen de dagkrant gebruikt
# dit — een magazine bundelt bewust een vaste periode en raakt het niet aan.
_SEEN_RETENTION_DAYS = 8  # ruim boven het 72-uursvenster
_CONTENT_RETENTION_DAYS = 8  # zelfde horizon als de message-id-administratie

# Inhoud-fingerprint: hashes van opeenvolgende woordgroepjes ("shingles").
# k=8 is bewust specifiek — een toevallige overlap van 8 exact dezelfde
# woorden op rij is zeldzaam, dus weinig kans op een fout-positieve match.
_CONTENT_SHINGLE_K = 8
# Jaccard-overlap vanaf hier telt als inhoudelijk duplicaat. Niet empirisch
# gekalibreerd tegen echte recap-mails (die had ik niet voorhanden) — begin
# hiermee en draai bij als er ten onrechte iets wordt overgeslagen of juist
# een duidelijke herhaling doorglipt.
_CONTENT_DUP_THRESHOLD = 0.5
# Cap op de tekstlengte vóór het shingelen: houdt de fingerprint (en dus het
# bewaarde JSON-bestand) klein, ook bij een lang artikel als Lenny's Newsletter.
_CONTENT_FINGERPRINT_CHARS = 4000


def _seen_ids_path() -> str:
    """Pad naar de verzonden-administratie (env SEEN_IDS_FILE of logs/)."""
    return os.getenv("SEEN_IDS_FILE", os.path.join("..", "logs", "seen_ids.json"))


def _load_seen(path: str) -> dict | None:
    """Lees de administratie; None als het bestand (nog) niet bestaat."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"Verzonden-administratie onleesbaar ({e}) — start met lege lijst.")
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Migratie: oudere bestanden waren een platte {message-id: datum}-map
    # zonder "content"-fingerprints.
    if "message_ids" not in data:
        data = {"message_ids": data, "content": []}
    data.setdefault("message_ids", {})
    data.setdefault("content", [])
    return data


def _save_seen(path: str, seen: dict) -> None:
    """Schrijf de administratie; snoei items ouder dan hun bewaartermijn."""
    now = datetime.now(timezone.utc)
    id_cutoff = now - timedelta(days=_SEEN_RETENTION_DAYS)
    content_cutoff = now - timedelta(days=_CONTENT_RETENTION_DAYS)

    pruned_ids = {}
    for mid, stamp in seen.get("message_ids", {}).items():
        try:
            if datetime.fromisoformat(stamp) >= id_cutoff:
                pruned_ids[mid] = stamp
        except (ValueError, TypeError):
            continue

    pruned_content = []
    for entry in seen.get("content", []):
        try:
            if datetime.fromisoformat(entry.get("date", "")) >= content_cutoff:
                pruned_content.append(entry)
        except (ValueError, TypeError):
            continue

    pruned = {"message_ids": pruned_ids, "content": pruned_content}
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=1)
    logger.info(
        f"Verzonden-administratie bijgewerkt: {len(pruned_ids)} id's, "
        f"{len(pruned_content)} inhoud-fingerprint(s) ({path})."
    )


def _shingle_fingerprint(text: str, k: int = _CONTENT_SHINGLE_K) -> list[int]:
    """
    Maak een compacte, deterministische vingerafdruk van doorlopende tekst:
    hashes van opeenvolgende woordgroepjes van k woorden ("shingles").

    Deterministisch via hashlib (niet Python's ingebouwde hash()): elke run
    is een nieuw proces met een gerandomiseerde hash-seed, dus dezelfde tekst
    zou in twee runs andere hash()-waarden geven en vergelijken zou nooit iets
    vinden.
    """
    words = re.findall(r"\w+", text.lower()[:_CONTENT_FINGERPRINT_CHARS])
    if len(words) < k:
        return []
    shingles = {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}
    return [
        int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")
        for s in shingles
    ]


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_content_duplicate(fingerprint: list[int], recent: list[dict]) -> bool:
    """True als deze fingerprint sterk overlapt met een eerder afgeleverd artikel."""
    fp_set = set(fingerprint)
    if not fp_set:
        return False
    return any(
        _jaccard(fp_set, set(entry.get("shingles", []))) >= _CONTENT_DUP_THRESHOLD
        for entry in recent
    )


_NL_MONTHS_SHORT = {
    1: "januari", 2: "februari", 3: "maart", 4: "april", 5: "mei", 6: "juni",
    7: "juli", 8: "augustus", 9: "september", 10: "oktober", 11: "november", 12: "december",
}


def _parse_local_date(value: str) -> datetime:
    """Parse 'YYYY-MM-DD' als start-van-de-dag in Europe/Amsterdam, terug in UTC."""
    local = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    return local.astimezone(timezone.utc)


def _format_dutch_date_only(value: str) -> str:
    """Formatteer 'YYYY-MM-DD' naar '1 juni 2026'."""
    d = datetime.strptime(value, "%Y-%m-%d")
    return f"{d.day} {_NL_MONTHS_SHORT[d.month]} {d.year}"


def _slugify(value: str) -> str:
    """Maak een bestandsnaam-veilige slug van een titel."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return slug or "Magazine"


def main():
    """Hoofdfunctie: de dirigent die alles aanstuurt."""
    load_dotenv()

    # Configuratie laden
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    target_email = os.getenv("TARGET_EMAIL")
    # Hervat op verzoek van Dennis (3 sept 2026) — was gepauzeerd sinds 20 juli 2026.
    target_email_enabled = True
    kindle_email = os.getenv("KINDLE_EMAIL")  # optioneel
    # Tijdelijk gepauzeerd op verzoek van Dennis (29 juli 2026): tot nader order
    # alleen Readwise, geen Kindle-verzending. Zet terug op True om te hervatten.
    kindle_enabled = False
    # Reader API: één highlightbaar HTML-document per editie. De oude feedmail
    # met PDF-bijlage is bewust vervallen, omdat Readwise daar twee items van maakte.
    readwise_token = os.getenv("READWISE_TOKEN", "").strip()
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    # Magazine-modus: bundel álle edities van ÉÉN nieuwsbrief over een vast
    # datumbereik (bv. een hele maand), i.p.v. de dagelijkse 24-uurs editie.
    # MAGAZINE_SENDER is de oude naam van dezelfde input; nog gelezen als
    # fallback zodat een oude workflow-aanroep niet stilvalt.
    is_magazine = os.getenv("MODE", "dagkrant").strip().lower() == "magazine"
    magazine_newsletter = (
        os.getenv("MAGAZINE_NEWSLETTER", "") or os.getenv("MAGAZINE_SENDER", "")
    ).strip()
    magazine_from = os.getenv("MAGAZINE_FROM", "").strip()
    magazine_to = os.getenv("MAGAZINE_TO", "").strip()
    magazine_title = os.getenv("MAGAZINE_TITLE", "").strip()

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

    if is_magazine and (not magazine_from or not magazine_to):
        logger.error("Magazine-modus vereist MAGAZINE_FROM en MAGAZINE_TO (YYYY-MM-DD).")
        sys.exit(1)

    # Een magazine is per definitie een bundel van één nieuwsbrief. Zonder keuze
    # zou de run álle nieuwsbrieven van de periode bundelen — dat is een andere
    # publicatie en nooit wat er bedoeld werd.
    if is_magazine and not magazine_newsletter:
        logger.error("Magazine-modus vereist MAGAZINE_NEWSLETTER: de naam van precies één nieuwsbrief.")
        sys.exit(1)

    logger.info("=" * 60)
    if is_magazine:
        logger.info("DE DAGKRANT - Magazine")
    else:
        logger.info("DE DAGKRANT - Dagelijkse nieuwsbundel")
    logger.info(f"Datum: {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M UTC')}")
    logger.info("=" * 60)

    # --- Stap 1: Nieuwsbrieven ophalen ---
    if is_magazine:
        since_dt = _parse_local_date(magazine_from)
        until_dt = _parse_local_date(magazine_to) + timedelta(days=1)
        logger.info(
            f"\n📬 Stap 1: Nieuwsbrieven ophalen uit Gmail "
            f"(magazine '{magazine_newsletter}': {magazine_from} t/m {magazine_to})..."
        )
        newsletters = fetch_newsletters(
            gmail_user, gmail_password,
            since_date=since_dt, until_date=until_dt,
            newsletter_label=magazine_newsletter,
        )
    else:
        hours_back = _calculate_hours_back()
        logger.info(f"\n📬 Stap 1: Nieuwsbrieven ophalen uit Gmail (laatste {hours_back} uur)...")
        newsletters = fetch_newsletters(gmail_user, gmail_password, hours_back=hours_back)

    logger.info(f"{len(newsletters)} nieuwsbrief(ven) gevonden.")

    # --- Verzonden-administratie: filter wat al in een eerdere editie zat ---
    # Het 72-uursvenster geeft laat-gelabelde mails alsnog een kans; deze
    # administratie voorkomt dat de rest drie dagen achter elkaar meekomt.
    seen: dict | None = None
    seen_path = _seen_ids_path()
    first_seen_run = False
    if not is_magazine:
        seen = _load_seen(seen_path)
        first_seen_run = seen is None
        if first_seen_run:
            # Overgang van 24u → 72u: mails ouder dan 24 uur zaten (mits op tijd
            # gelabeld) al in eerdere edities. Markeer ze als gedekt zonder te
            # versturen, anders krijgt de eerste run een golf aan duplicaten.
            logger.info("Geen verzonden-administratie gevonden — eerste run met 72-uursvenster; "
                        "mails ouder dan 24 uur worden als al-gedekt gemarkeerd.")
            seen = {"message_ids": {}, "content": []}
        now_utc = datetime.now(timezone.utc)
        recent_cutoff = now_utc - timedelta(hours=24)
        kept = []
        for nl in newsletters:
            mid = nl.get("message_id", "")
            if mid and mid in seen["message_ids"]:
                logger.info(f"  ⏭ Al in eerdere editie: '{nl['subject'][:60]}'")
                continue
            if first_seen_run:
                try:
                    nl_date = datetime.fromisoformat(nl["date"])
                except (KeyError, ValueError):
                    nl_date = now_utc
                if nl_date < recent_cutoff:
                    if mid:
                        seen["message_ids"][mid] = now_utc.isoformat()
                    continue
            kept.append(nl)
        if len(kept) != len(newsletters):
            logger.info(f"{len(newsletters) - len(kept)} nieuwsbrief(ven) overgeslagen "
                        f"(al gedekt door een eerdere editie).")
        newsletters = kept

    # Handmatig toegevoegde webartikelen (label: Dagkrant/Lezen) — niet relevant
    # voor een themamagazine over een vast datumbereik.
    if not is_magazine:
        logger.info(f"\n🔗 Stap 1b: Handmatige artikelen ophalen (label: Dagkrant/Lezen)...")
        # Eerste run zonder administratie: 24u (oud gedrag), anders zouden al
        # eerder geplaatste webartikelen van de afgelopen 3 dagen dubbel komen.
        article_urls = fetch_article_urls(
            gmail_user, gmail_password,
            hours_back=24 if first_seen_run else hours_back,
        )
        for url in article_urls:
            if seen is not None and url in seen["message_ids"]:
                logger.info(f"  ⏭ Al in eerdere editie: {url}")
                continue
            article = fetch_article(url)
            if article:
                article["message_id"] = url  # URL als sleutel in de administratie
                newsletters.append(article)
                logger.info(f"  Toegevoegd: '{article['subject'][:60]}'")

    if not newsletters:
        if is_magazine:
            logger.info("Geen nieuwsbrieven gevonden voor dit magazine-filter. Klaar!")
        else:
            logger.info(f"Geen nieuwsbrieven of artikelen gevonden in de laatste {hours_back} uur. Klaar!")
            if seen is not None:
                _save_seen(seen_path, seen)  # bewaar evt. eerste-run-markeringen
        return

    # Sorteer op datum (nieuwste eerst)
    newsletters.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Alles wat deze editie op tafel had — óók wat straks door de afzenderlimiet
    # of contentchecks afvalt — telt als gedekt: elke mail krijgt precies één kans.
    edition_ids = [nl["message_id"] for nl in newsletters if nl.get("message_id")] if not is_magazine else []

    # Dedupliceer per afzender: maximaal 2 artikelen per afzender.
    # In magazine-modus is juist de bedoeling om alles van de gekozen periode/
    # afzender te bundelen, dus geen limiet.
    if is_magazine:
        filtered_by_sender = newsletters
    else:
        MAX_PER_SENDER = 3
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

    # --- Stap 2-3: Verwerk elke nieuwsbrief individueel ---
    # Elke nieuwsbrief wordt apart verwerkt. Als er iets misgaat,
    # wordt die ene nieuwsbrief overgeslagen en gaat de rest door.
    logger.info("\n🧹 Stap 2-3: Opschonen, dedupliceren, detecteren en vertalen...")

    processed = []
    # Zodra OpenAI permanent uitvalt (krediet op / key ongeldig) heeft verder
    # proberen geen zin: elke volgende aanroep faalt identiek. We onthouden dat,
    # laten de Engelse artikelen ongewijzigd staan (niet droppen) en slaan één
    # luide waarschuwing op voor aan het eind. Bewuste keuze: liever een (deels)
    # Engelse krant dan helemaal geen krant.
    openai_down = False
    for i, nl in enumerate(newsletters):
        subject = nl.get("subject", "(Onbekend)")
        try:
            # Stap 2a: Controleer op generieke website-template (vóór cleaning)
            if is_website_template(nl["html_content"]):
                logger.warning(f"  ⚠️ '{subject}' lijkt een website-template — overgeslagen.")
                continue

            # Stap 2b: HTML opschonen
            raw_html = nl["html_content"]
            original_len = len(raw_html)
            nl["html_content"] = clean_html(raw_html)
            cleaned_len = len(nl["html_content"])
            reduction = ((original_len - cleaned_len) / original_len * 100) if original_len > 0 else 0
            logger.info(f"  [{i+1}/{len(newsletters)}] '{subject}' - {reduction:.0f}% rommel verwijderd")

            # Stap 2c: Validatie zichtbare tekst (minimaal 300 tekens)
            # Gebruikt _get_truly_visible_text() die ook display:none en &nbsp;
            # weefiltert — dit vangt Substack-stijl emails met hidden preview text.
            visible_text = _get_truly_visible_text(nl["html_content"])

            # VANGNET A: als clean_html() de tekst onder de drempel bracht maar het
            # origineel wél genoeg inhoud had, dan heeft de agressieve opschoning
            # het artikel weggevaagd. Val terug op een lichte opschoning i.p.v. het
            # artikel te droppen (geobserveerd bij o.a. Wilfred Rubens-nieuwsbrieven).
            if len(visible_text) < 300:
                original_visible = _get_truly_visible_text(raw_html)
                if len(original_visible) >= 300:
                    logger.warning(
                        f"  ↩ '{subject}': clean_html() liet maar {len(visible_text)} tekens over "
                        f"(origineel: {len(original_visible)}). Val terug op minimal_clean()."
                    )
                    nl["html_content"] = minimal_clean(raw_html)
                    visible_text = _get_truly_visible_text(nl["html_content"])

            if len(visible_text) < 300:
                logger.warning(
                    f"  ⚠️ '{subject}' heeft te weinig zichtbare tekst "
                    f"({len(visible_text)} tekens) — overgeslagen."
                )
                continue

            # Stap 2d: Dubbele titels verwijderen
            nl["html_content"] = deduplicate_title(nl["html_content"], subject)

            # Stap 3: Vertaling. translate_html() beslist zelf per HTML-blok of
            # het Engels is (zie translator.py) — dit vangt gemengde nieuwsbrieven
            # zoals Readwise's dagelijkse digest, die Engelse en Nederlandse
            # highlights in één mail bundelt. Een taalbeslissing over het HELE
            # document (het oude gedrag) liet dan de Engelse stukken onvertaald
            # zodra Nederlandse tekst in het geheel overheerste.
            # `lang` hieronder is alleen voor logging/openai_down-boodschap —
            # gate NIET langer of translate_html() wordt aangeroepen.
            lang = detect_language(nl["html_content"])
            nl["was_translated"] = False
            logger.info(f"    Taal (document): {lang.upper()}")

            if openai_down:
                # OpenAI is deze run al uitgevallen — niet opnieuw proberen, de
                # nieuwsbrief blijft ongewijzigd staan.
                if lang == "en":
                    logger.warning(
                        f"    ⚠️ OpenAI onbereikbaar — '{subject}' blijft Engels."
                    )
            else:
                pre_translation_html = nl["html_content"]
                try:
                    translated, was_translated = translate_html(nl["html_content"], openai_api_key)
                except OpenAIUnavailableError as e:
                    # Krediet op / key ongeldig: dit raakt élk artikel, niet alleen
                    # dit ene. Behoud het origineel (niet droppen) en onthoud dat
                    # OpenAI weg is, zodat de rest niet nutteloos opnieuw probeert.
                    openai_down = True
                    logger.warning(
                        f"    ⚠️ OpenAI onbereikbaar ({e}) — '{subject}' blijft "
                        f"onvertaald. Resterende artikelen worden ook niet vertaald."
                    )
                else:
                    if not was_translated:
                        logger.info(f"    Geen Engelse blokken gevonden — niets te vertalen.")
                    else:
                        # Vertaler kan code-fence artefacten toevoegen (```html ... ```)
                        translated = strip_ai_artifacts(translated)

                        # VANGNET B: als de vertaling (bijna) leeg terugkomt terwijl het
                        # Engelse origineel wél inhoud had, behoud dan het origineel i.p.v.
                        # het artikel te droppen. Engels lezen is beter dan een leeg artikel
                        # (geobserveerd bij o.a. The New Yorker).
                        if len(_get_truly_visible_text(translated)) >= 100:
                            nl["html_content"] = translated
                            nl["was_translated"] = True
                            logger.info(f"    Vertaling voltooid.")
                        else:
                            nl["html_content"] = pre_translation_html
                            logger.warning(
                                f"    ↩ '{subject}': vertaling leverde lege inhoud — "
                                f"behoud het origineel."
                            )

            # FINALE VEILIGHEIDSCHECK: meet de echt zichtbare tekst na ALLE
            # verwerking (cleaning + deduplicate_title + vertaling).
            # Dit is het definitieve vangnet — als hier < 100 chars overblijft,
            # verschijnt de newsletter als lege pagina in de PDF.
            final_visible = _get_truly_visible_text(nl["html_content"])
            if len(final_visible) < 100:
                logger.warning(
                    f"  ⚠️ '{subject}' heeft te weinig zichtbare content na alle "
                    f"verwerking ({len(final_visible)} tekens) — overgeslagen."
                )
                continue

            # Stap 3b: Inhoudelijke dedup — vangt een NIEUW bericht (ander
            # message-ID, ander onderwerp) dat grotendeels dezelfde tekst
            # herhaalt als iets dat de afgelopen dagen al is afgeleverd. Bv.
            # Readwise's "Sunday Favorites" (hergroepeert highlights van
            # eerder die week) of een wekelijkse "lectuur op zaterdag"-rubriek
            # die eerdere artikelen samenvat. Message-ID-dedup ziet dit niet:
            # voor die check is het gewoon een nieuwe, unieke mail.
            content_fp = _shingle_fingerprint(final_visible)
            if seen is not None and _is_content_duplicate(content_fp, seen["content"]):
                logger.info(
                    f"  ⏭ '{subject}' lijkt inhoudelijk sterk op iets uit een "
                    f"eerdere editie — overgeslagen."
                )
                continue

            nl["_content_fingerprint"] = content_fp
            processed.append(nl)

        except OpenAIUnavailableError as e:
            # Vangnet: de vertaling vangt dit normaal al inline af (Engels behouden).
            # Mocht een andere OpenAI-aanroep binnen de loop 'm toch opgooien, dan
            # breken we de editie NIET af — we onthouden de uitval en gaan door.
            openai_down = True
            nl["was_translated"] = False
            logger.warning(
                f"  ⚠️ OpenAI onbereikbaar ({e}) — '{subject}' blijft Engels."
            )
            processed.append(nl)
            continue

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

            if openai_down:
                # OpenAI is deze run al uitgevallen — geen AI-titel/beschrijving
                # meer proberen, val terug op het onderwerp.
                toc_data = {"short_title": nl["subject"][:50], "description": ""}
            else:
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
            # Gebruik de Nederlandse TOC-titel ook als artikelkop in de PDF —
            # zo verschijnt er nooit een Engelse kop boven een vertaald artikel.
            nl["display_subject"] = toc_data["short_title"]
            logger.info(f"  TOC: '{toc_data['short_title']}'")
        except OpenAIUnavailableError as e:
            # Krediet raakte tijdens de TOC-stap op (geen enkel artikel was Engels,
            # dus niet eerder gedetecteerd). Onthoud het en val terug op het onderwerp.
            openai_down = True
            logger.warning(
                f"  ⚠️ OpenAI onbereikbaar ({e}) — TOC voor '{nl['subject']}' "
                f"zonder AI-titel."
            )
            toc_entries.append({
                "subject": nl["subject"],
                "sender": nl["sender"],
                "short_title": nl["subject"][:50],
                "description": "",
                "was_translated": nl.get("was_translated", False),
            })
            nl["display_subject"] = nl["subject"]
        except Exception as e:
            logger.error(f"  Fout bij TOC entry voor '{nl['subject']}': {e}")
            toc_entries.append({
                "subject": nl["subject"],
                "sender": nl["sender"],
                "short_title": nl["subject"][:50],
                "description": "",
                "was_translated": nl.get("was_translated", False),
            })
            nl["display_subject"] = nl["subject"]

    # Eén gebundelde, luide waarschuwing als OpenAI is uitgevallen. De editie gaat
    # bewust wél de deur uit (deels Engels), maar dit mag niet ongemerkt blijven —
    # zowel in de log (voor de cloud-run) als zichtbaar op de voorpagina (voor de
    # geprinte krant, waar de log niet te zien is).
    cover_warning = None
    if openai_down:
        untranslated = sum(1 for nl in newsletters if not nl.get("was_translated", False)
                           and detect_language(nl["html_content"]) == "en")
        logger.warning("=" * 60)
        logger.warning("⚠️ LET OP: OpenAI viel uit tijdens deze run.")
        logger.warning("   De krant is verstuurd, maar Engelse nieuwsbrieven zijn")
        logger.warning("   NIET vertaald (en TOC-titels vielen terug op het onderwerp).")
        logger.warning(f"   Vermoedelijk onvertaald gebleven: ~{untranslated} artikel(en).")
        logger.warning("   → Vul krediet aan op "
                       "https://platform.openai.com/settings/organization/billing")
        logger.warning("=" * 60)
        cover_warning = (
            f"Deze editie is deels onvertaald: de automatische vertaling was tijdens "
            f"het samenstellen niet beschikbaar, waardoor ~{untranslated} Engelse "
            f"nieuwsbrief(ven) in het Engels zijn gebleven."
        )

    # --- Stap 5: PDF samenstellen ---
    logger.info("\n📄 Stap 5: PDF genereren...")
    if is_magazine:
        # Een magazine bundelt precies één nieuwsbrief, dus de labelnaam ís de titel.
        names_label = magazine_newsletter
        # Titel: "Magazine — <nieuwsbrief>", tenzij een eigen covertitel is opgegeven
        display_title = magazine_title or f"Magazine — {names_label}"
        masthead_title = magazine_title or "Magazine"
        masthead_subtitle = names_label
        period_label = f"{_format_dutch_date_only(magazine_from)} – {_format_dutch_date_only(magazine_to)}"
        cover_html = render_cover_page(
            newsletters, toc_entries,
            masthead_title=masthead_title, masthead_subtitle=masthead_subtitle,
            edition_label=period_label,
            translation_warning=cover_warning,
        )
    else:
        cover_html = render_cover_page(
            newsletters, toc_entries, translation_warning=cover_warning,
        )
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
        mail_kwargs = {}
        if is_magazine:
            mail_kwargs["subject"] = f"{display_title} — {period_label}"
            mail_kwargs["body"] = (
                f"Goedemiddag!\n\n"
                f"Hierbij je magazine '{display_title}' ({period_label})"
                + f". {len(newsletters)} nieuwsbrie{'f' if len(newsletters) == 1 else 'ven'} gebundeld.\n\n"
                f"Veel leesplezier!\n\n"
                f"Met vriendelijke groet,\n"
                f"De Dagkrant"
            )
            mail_kwargs["filename"] = (
                f"Magazine_{_slugify(magazine_title or names_label)}"
                f"_{magazine_from}_tot_{magazine_to}.pdf"
            )

        if target_email_enabled:
            send_email_with_pdf(
                pdf_path=pdf_path,
                sender_email=gmail_user,
                sender_password=gmail_password,
                recipient_email=target_email,
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                **mail_kwargs,
            )
        else:
            logger.info(f"⏸️  TARGET_EMAIL-verzending is gepauzeerd — niet verstuurd naar {target_email}.")

        # Editie is verstuurd — registreer alle meegewogen mails én de
        # inhoud-fingerprints van wat daadwerkelijk verstuurd is, zodat het
        # 72-uursvenster geen exacte of inhoudelijke duplicaten meer geeft.
        if seen is not None:
            sent_stamp = datetime.now(timezone.utc).isoformat()
            for mid in edition_ids:
                seen["message_ids"][mid] = sent_stamp
            for nl in newsletters:
                fp = nl.get("_content_fingerprint")
                if fp:
                    seen["content"].append({"date": sent_stamp, "shingles": fp})
            _save_seen(seen_path, seen)

        recipients = [target_email] if target_email_enabled else []

        # Kindle: stuur dezelfde PDF ook naar de Kindle-e-reader
        if kindle_email and kindle_enabled:
            logger.info(f"📚 Kindle: PDF verzenden naar {kindle_email}...")
            send_email_with_pdf(
                pdf_path=pdf_path,
                sender_email=gmail_user,
                sender_password=gmail_password,
                recipient_email=kindle_email,
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                **mail_kwargs,
            )
            recipients.append(kindle_email)
        elif kindle_email:
            logger.info(f"⏸️  Kindle-verzending is gepauzeerd — niet verstuurd naar {kindle_email}.")

        # Readwise Reader: sla de HTML rechtstreeks op. Daardoor ontstaat één
        # leesbaar document met selecteerbare tekst en native highlights.
        if readwise_token:
            local_now = datetime.now(ZoneInfo("Europe/Amsterdam"))
            if is_magazine:
                readwise_title = f"{display_title} — {period_label}"
                readwise_key = (
                    f"magazine-{magazine_from}-{magazine_to}-"
                    f"{_slugify(magazine_title or names_label).lower()}"
                )
            else:
                dutch_date = (
                    f"{local_now.day} {_NL_MONTHS_SHORT[local_now.month]} {local_now.year}"
                )
                readwise_title = f"De Dagkrant — {dutch_date}"
                readwise_key = local_now.date().isoformat()
            logger.info(
                f"📖 Readwise: highlightbaar HTML-document opslaan als '{readwise_title}'..."
            )
            save_html_document(
                html_content=full_html,
                access_token=readwise_token,
                title=readwise_title,
                source_key=readwise_key,
                published_date=local_now.isoformat(),
            )
            recipients.append("Readwise Reader")
        else:
            logger.warning("⚠️ READWISE_TOKEN ontbreekt — geen levering aan Readwise Reader.")

        logger.info("\n" + "=" * 60)
        logger.info("DE DAGKRANT IS KLAAR!")
        logger.info(f"Verzonden naar: {', '.join(recipients)}")
        logger.info("=" * 60)

    finally:
        # Tijdelijk PDF bestand opruimen
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)


if __name__ == "__main__":
    # OpenAIUnavailableError wordt bewust NIET hier afgevangen om de run te laten
    # falen: bij credit-op verschijnt de krant (deels Engels) mét een luide
    # waarschuwing in de log — liever een imperfecte krant dan geen krant. De
    # uitval wordt inline afgehandeld in main() (zie `openai_down`).
    main()
