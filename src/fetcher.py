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


def fetch_newsletters(
    gmail_user: str,
    gmail_password: str,
    label: str = "Nieuwsbrieven",
    hours_back: int = 24,
) -> list[dict]:
    """
    Haal nieuwsbrieven op uit Gmail via IMAP.

    Returns:
        Lijst van dicts met keys: subject, sender, date, html_content, plain_content
    """
    newsletters = []
    since_date = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    # IMAP SINCE gebruikt alleen een datum (geen tijd)
    since_str = since_date.strftime("%d-%b-%Y")

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

        for folder in folders_to_search:
            try:
                status, _ = mail.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    logger.warning(f"Kan folder '{folder}' niet openen, skip.")
                    continue

                # Zoek e-mails sinds de opgegeven datum
                status, message_ids = mail.search(None, f'(SINCE "{since_str}")')
                if status != "OK" or not message_ids[0]:
                    logger.debug(f"Geen e-mails in '{folder}' sinds {since_str}.")
                    continue

                ids = message_ids[0].split()
                logger.info(f"  {len(ids)} e-mail(s) in '{folder}' sinds {since_str}.")

                for msg_id in ids:
                    try:
                        status, msg_data = mail.fetch(msg_id, "(RFC822)")
                        if status != "OK":
                            continue

                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)

                        # Dedup op Message-ID over folders heen
                        message_id = msg.get("Message-ID", "")
                        if message_id and message_id in seen_message_ids:
                            continue
                        if message_id:
                            seen_message_ids.add(message_id)

                        # Parse datum en controleer of het binnen het tijdvenster valt
                        date_str = msg.get("Date", "")
                        parsed_date = email.utils.parsedate_to_datetime(date_str)
                        if parsed_date.tzinfo is None:
                            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                        if parsed_date < since_date:
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

                        newsletters.append({
                            "subject": subject,
                            "sender": sender,
                            "date": parsed_date.isoformat(),
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
