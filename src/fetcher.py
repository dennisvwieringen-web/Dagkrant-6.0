"""
fetcher.py - Gmail IMAP module voor het ophalen van nieuwsbrieven.

Haalt e-mails op met het label 'Nieuwsbrieven' die in de laatste 24 uur
zijn ontvangen. Retourneert een lijst van dictionaries met metadata en HTML-content.
"""

import imaplib
import email
import email.message
import email.utils
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.header import decode_header
from typing import Optional

logger = logging.getLogger(__name__)


def _decode_header_value(value: str) -> str:
    """Decodeer een MIME-gecodeerde header naar leesbare tekst."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_html_body(msg: email.message.Message) -> Optional[str]:
    """Haal de HTML-body op uit een e-mailbericht (multipart of single)."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        if msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return None


def _extract_plain_body(msg: email.message.Message) -> Optional[str]:
    """Haal de plain-text body op als fallback."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return None


def extract_real_sender(plain_content: Optional[str], html_content: Optional[str], envelope_sender: str) -> str:
    """
    Probeer de echte afzender te achterhalen uit doorgestuurde e-mails.

    Zoekt naar forwarding-patronen in plain-text én HTML-content:
    - "Oorspronkelijk van: Naam <email>"
    - "Van: Naam <email>" / "From: Naam <email>"
    - "---------- Forwarded message ---------"
    - "Begin forwarded message:"

    Returns:
        De geëxtraheerde echte afzendernaam, of de envelope_sender als fallback.
    """
    # Patronen om de echte afzender te vinden in forwarding-headers
    _REAL_SENDER_PATTERNS = [
        # Nederlands: "Oorspronkelijk van: Naam <email>" of "Oorspronkelijk van: Naam"
        re.compile(r"oorspronkelijk\s+van\s*:\s*([^\n<\r]+?)(?:\s*<[^>]+>)?\s*[\r\n]", re.IGNORECASE),
        # Engels: "From: Naam <email>" na een forwarding-marker
        re.compile(r"from\s*:\s*([^\n<\r]+?)(?:\s*<[^>]+>)?\s*[\r\n]", re.IGNORECASE),
        # Nederlands: "Van: Naam <email>"
        re.compile(r"^van\s*:\s*([^\n<\r]+?)(?:\s*<[^>]+>)?\s*[\r\n]", re.IGNORECASE | re.MULTILINE),
    ]

    # Geef de voorkeur aan de plain-text body voor sender-extractie
    search_texts = []
    if plain_content:
        search_texts.append(plain_content)
    if html_content:
        # Strip HTML-tags voor plain-text zoeken
        from bs4 import BeautifulSoup as _BS
        try:
            search_texts.append(_BS(html_content, "html.parser").get_text())
        except Exception:
            pass

    for text in search_texts:
        # Controleer of dit een doorgestuurde e-mail is
        is_forwarded = bool(re.search(
            r"(-{3,}\s*(forwarded|doorgestuurd|begin forwarded)\s*(message|bericht)?\s*-{3,}|"
            r"oorspronkelijk\s+(van|bericht)\s*:|"
            r"begin\s+forwarded\s+message)",
            text, re.IGNORECASE
        ))

        if not is_forwarded:
            continue

        # Zoek de forwarding-sectie en scan daarna naar de afzender
        # Splits op de forwarding-marker en kijk in het volgende blok
        forward_marker = re.search(
            r"-{3,}\s*(forwarded|doorgestuurd|begin forwarded).*?-{3,}|"
            r"oorspronkelijk\s+(van|bericht)\s*:|"
            r"begin\s+forwarded\s+message",
            text, re.IGNORECASE
        )

        if forward_marker:
            # Zoek in het stuk DIRECT NA de forwarding-marker
            after_marker = text[forward_marker.start():]
            for pattern in _REAL_SENDER_PATTERNS:
                m = pattern.search(after_marker[:500])  # Beperk zoekruimte
                if m:
                    extracted = m.group(1).strip()
                    # Sanity check: niet leeg, niet te lang, geen e-mailadres zelf
                    if extracted and len(extracted) < 100 and "@" not in extracted:
                        logger.info(f"  Echte afzender geëxtraheerd: '{extracted}' (was: '{envelope_sender}')")
                        return extracted

    return envelope_sender


def _imap_utf7_decode(name: str) -> str:
    """
    Decodeer een IMAP modified-UTF-7 foldernaam (RFC 3501) naar gewone tekst.

    Gmail codeert niet-ASCII-tekens in labelnamen zo: "Alexander Kl&APY-pping"
    → "Alexander Klöpping". "&-" is een letterlijke "&". Nodig om labelnamen
    leesbaar te maken voor weergave en voor het magazine-filter.
    """
    import base64

    result = []
    i = 0
    while i < len(name):
        if name[i] == "&":
            end = name.find("-", i)
            if end == -1:  # geen afsluiter — behandel als letterlijke tekst
                result.append(name[i:])
                break
            b64 = name[i + 1:end]
            if not b64:
                result.append("&")
            else:
                b64 = b64.replace(",", "/")
                b64 += "=" * ((4 - len(b64) % 4) % 4)
                try:
                    result.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    result.append(name[i:end + 1])
            i = end + 1
        else:
            result.append(name[i])
            i += 1
    return "".join(result)


def fetch_newsletters(
    gmail_user: str,
    gmail_password: str,
    label: str = "Nieuwsbrieven",
    hours_back: int = 24,
    since_date: Optional[datetime] = None,
    until_date: Optional[datetime] = None,
    sender_filter: Optional[str] = None,
) -> list[dict]:
    """
    Haal nieuwsbrieven op uit Gmail via IMAP.

    Standaard wordt `hours_back` gebruikt om het tijdvenster te bepalen (dagelijkse
    editie). Geef `since_date`/`until_date` expliciet mee om een vast datumbereik
    op te vragen (bv. voor een magazine over een hele maand) — dat overschrijft
    `hours_back`. `sender_filter` beperkt het resultaat tot e-mails waarvan de
    afzender, het onderwerp óf de Gmail-labelnaam de tekst bevat
    (case-insensitive substring). Meerdere nieuwsbrieven tegelijk: scheid
    termen met "|" (of komma's als er geen "|" in zit — "|" is nodig omdat
    labelnamen zelf komma's kunnen bevatten, zoals "X, Y of Einstein");
    een e-mail hoeft maar aan één term te voldoen (OR).

    Returns:
        Lijst van dicts met keys: subject, sender, date, label,
        html_content, plain_content
    """
    newsletters = []
    if since_date is None:
        since_date = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    # IMAP SINCE gebruikt alleen een datum (geen tijd)
    since_str = since_date.strftime("%d-%b-%Y")
    search_criteria = f'(SINCE "{since_str}")'
    if until_date is not None:
        before_str = until_date.strftime("%d-%b-%Y")
        search_criteria = f'(SINCE "{since_str}" BEFORE "{before_str}")'

    logger.info(f"Verbinden met Gmail IMAP als {gmail_user}...")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_user, gmail_password)
    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP login mislukt: {e}")
        raise

    try:
        # Zoek alle IMAP-folders die beginnen met het label (inclusief sublabels)
        # Gmail slaat sublabels op als "Nieuwsbrieven/AI Report", etc.
        status, folder_list = mail.list('""', f'"{label}*"')
        folders_to_search = []

        if status == "OK" and folder_list:
            for folder_info in folder_list:
                if folder_info is None:
                    continue
                # Parse folder name uit IMAP LIST response
                # Formaat: b'(\\HasChildren) "/" "Nieuwsbrieven"'
                decoded = folder_info.decode("utf-8", errors="replace")
                # Haal de folder-naam op (laatste quoted string)
                parts = decoded.rsplit('" "', 1)
                if len(parts) == 2:
                    folder_name = parts[1].rstrip('"')
                else:
                    # Alternatief: neem alles na de laatste slash-spatie
                    parts = decoded.rsplit('"', 2)
                    if len(parts) >= 2:
                        folder_name = parts[-2]
                    else:
                        continue
                folders_to_search.append(folder_name)

        if not folders_to_search:
            # Fallback: probeer alleen het opgegeven label
            folders_to_search = [label]

        logger.info(
            f"Doorzoek {len(folders_to_search)} folder(s): "
            f"{', '.join(folders_to_search)}"
        )

        seen_message_ids = set()  # Voorkom duplicaten over folders heen

        # Filtertermen: "|" is het scheidingsteken (labelnamen kunnen komma's
        # bevatten); alleen als er geen "|" staat, splitsen we op komma's.
        filter_terms = []
        if sender_filter:
            splitter = "|" if "|" in sender_filter else ","
            filter_terms = [t.strip().lower() for t in sender_filter.split(splitter) if t.strip()]

        for folder in folders_to_search:
            # Leesbare labelnaam: UTF-7 gedecodeerd, zonder "Nieuwsbrieven/"-prefix
            folder_label = _imap_utf7_decode(folder)
            if folder_label.lower().startswith(f"{label.lower()}/"):
                folder_label = folder_label[len(label) + 1:]
            try:
                status, _ = mail.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    logger.warning(f"Kan folder '{folder}' niet openen, skip.")
                    continue

                # Zoek e-mails binnen het opgegeven tijdvenster
                status, message_ids = mail.search(None, search_criteria)
                if status != "OK" or not message_ids[0]:
                    logger.debug(f"Geen e-mails in '{folder}' voor {search_criteria}.")
                    continue

                ids = message_ids[0].split()
                logger.info(f"  {len(ids)} e-mail(s) in '{folder}' voor {search_criteria}.")

                for msg_id in ids:
                    try:
                        status, msg_data = mail.fetch(msg_id, "(RFC822)")
                        if status != "OK":
                            continue

                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)

                        # Dedup op Message-ID over folders heen. Pas op: registreren
                        # gebeurt pas bij ACCEPTATIE (na het filter, zie onder) —
                        # dezelfde mail hangt vaak onder meerdere labels (hoofdfolder
                        # "Nieuwsbrieven" + sublabel), en het label-filter kan 'm in
                        # de ene folder afwijzen maar in de andere moeten accepteren.
                        message_id = msg.get("Message-ID", "")
                        if message_id and message_id in seen_message_ids:
                            continue

                        # Parse datum en controleer of het binnen het tijdvenster valt
                        date_str = msg.get("Date", "")
                        parsed_date = email.utils.parsedate_to_datetime(date_str)
                        if parsed_date.tzinfo is None:
                            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                        if parsed_date < since_date:
                            continue
                        if until_date is not None and parsed_date >= until_date:
                            continue

                        subject = _decode_header_value(msg.get("Subject", "(Geen onderwerp)"))
                        envelope_sender = _decode_header_value(msg.get("From", "(Onbekende afzender)"))
                        html_content = _extract_html_body(msg)
                        plain_content = _extract_plain_body(msg)

                        if not html_content and not plain_content:
                            logger.warning(f"Geen content in e-mail: {subject}")
                            continue

                        # Als er geen HTML is, wikkel plain text in basis-HTML
                        if not html_content and plain_content:
                            html_content = f"<html><body><pre>{plain_content}</pre></body></html>"

                        # Extraheer de echte afzender bij doorgestuurde e-mails
                        sender = extract_real_sender(plain_content, html_content, envelope_sender)

                        # Magazine-modus: filter op afzender, onderwerp óf labelnaam
                        # (case-insensitive substring). Een e-mail hoeft maar aan één
                        # term te voldoen (OR), zodat één magazine meerdere
                        # nieuwsbrieven kan bundelen. Labelnaam meenemen is essentieel:
                        # labels als "Oliver Burkeman" of "Lenny" wijken af van de
                        # afzendernaam in de mail zelf.
                        if filter_terms and not any(
                            t in sender.lower()
                            or t in subject.lower()
                            or t in folder_label.lower()
                            for t in filter_terms
                        ):
                            continue

                        # Geaccepteerd — nu pas markeren als gezien (zie dedup-opmerking)
                        if message_id:
                            seen_message_ids.add(message_id)

                        newsletters.append({
                            "subject": subject,
                            "sender": sender,
                            "date": parsed_date.isoformat(),
                            "label": folder_label,
                            "message_id": message_id,
                            "html_content": html_content,
                            "plain_content": plain_content,
                        })
                        logger.info(f"  Opgehaald: {subject}")

                    except Exception as e:
                        logger.error(f"Fout bij verwerken e-mail {msg_id}: {e}")
                        continue

            except Exception as e:
                logger.error(f"Fout bij doorzoeken folder '{folder}': {e}")
                continue

    finally:
        mail.logout()

    logger.info(f"Totaal {len(newsletters)} nieuwsbrief(ven) opgehaald (vóór dedup).")

    # Deduplicatie: verwijder e-mails met zeer vergelijkbaar subject (>90% match)
    newsletters = _deduplicate_newsletters(newsletters)
    logger.info(f"Na deduplicatie: {len(newsletters)} nieuwsbrief(ven).")

    return newsletters


def fetch_article_urls(
    gmail_user: str,
    gmail_password: str,
    label: str = "Nieuwsbrieven/Extra artikelen",
    hours_back: int = 24,
) -> list[str]:
    """
    Haal artikel-URLs op uit emails met het label 'Dagkrant/Lezen'.

    De gebruiker stuurt zichzelf een e-mail met een URL (in het onderwerp
    of de body) en labelt die met 'Dagkrant/Lezen'. Deze functie extraheert
    alle gevonden URLs uit zulke emails van de afgelopen 24 uur.

    Returns:
        Lijst van unieke URLs.
    """
    _URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")

    urls: list[str] = []
    since_date = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    since_str = since_date.strftime("%d-%b-%Y")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_user, gmail_password)
    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP login mislukt voor artikel-URLs: {e}")
        return []

    try:
        status, _ = mail.select(f'"{label}"', readonly=True)
        if status != "OK":
            logger.info(f"Label '{label}' niet gevonden — geen handmatige artikelen.")
            return []

        status, message_ids = mail.search(None, f'(SINCE "{since_str}")')
        if status != "OK" or not message_ids[0]:
            logger.info(f"Geen emails in '{label}' sinds {since_str}.")
            return []

        for msg_id in message_ids[0].split():
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                # Zoek URLs in onderwerp én body
                search_text = msg.get("Subject", "") + "\n"
                plain = _extract_plain_body(msg)
                if plain:
                    search_text += plain
                else:
                    html = _extract_html_body(msg)
                    if html:
                        from bs4 import BeautifulSoup as _BS
                        search_text += _BS(html, "html.parser").get_text()

                for url in _URL_RE.findall(search_text):
                    if url not in urls:
                        logger.info(f"  Artikel-URL gevonden: {url}")
                        urls.append(url)

            except Exception as e:
                logger.error(f"Fout bij verwerken artikel-email {msg_id}: {e}")
                continue

    finally:
        mail.logout()

    return urls


def _deduplicate_newsletters(newsletters: list[dict]) -> list[dict]:
    """
    Verwijder duplicaten op basis van subject-overeenkomst (>90%).
    Bij duplicaten: bewaar degene met de meest recente datum.
    """
    if not newsletters:
        return newsletters

    deduplicated = []

    for nl in newsletters:
        subject = nl.get("subject", "").lower().strip()
        nl_date = nl.get("date", "")
        is_duplicate = False

        for i, existing in enumerate(deduplicated):
            existing_subject = existing.get("subject", "").lower().strip()
            similarity = SequenceMatcher(None, subject, existing_subject).ratio()

            if similarity > 0.90:
                # Bewaar de meest recente versie
                if nl_date > existing.get("date", ""):
                    logger.info(
                        f"  Dedup: '{nl['subject'][:60]}' vervangt oudere versie "
                        f"({similarity:.0%} match)"
                    )
                    deduplicated[i] = nl
                else:
                    logger.info(
                        f"  Dedup: '{nl['subject'][:60]}' overgeslagen — "
                        f"duplicaat ({similarity:.0%} match)"
                    )
                is_duplicate = True
                break

        if not is_duplicate:
            deduplicated.append(nl)

    return deduplicated
