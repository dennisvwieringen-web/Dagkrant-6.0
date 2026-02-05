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
        # Gmail labels worden als IMAP-folders benaderd
        status, _ = mail.select(f'"{label}"', readonly=True)
        if status != "OK":
            logger.warning(f"Label '{label}' niet gevonden, probeer inbox...")
            mail.select("INBOX", readonly=True)

        # Zoek e-mails sinds de opgegeven datum
        status, message_ids = mail.search(None, f'(SINCE "{since_str}")')
        if status != "OK" or not message_ids[0]:
            logger.info("Geen nieuwe nieuwsbrieven gevonden.")
            return newsletters

        ids = message_ids[0].split()
        logger.info(f"{len(ids)} e-mail(s) gevonden sinds {since_str}.")

        for msg_id in ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Parse datum en controleer of het binnen het tijdvenster valt
                date_str = msg.get("Date", "")
                parsed_date = email.utils.parsedate_to_datetime(date_str)
                if parsed_date.tzinfo is None:
                    parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                if parsed_date < since_date:
                    continue

                subject = _decode_header_value(msg.get("Subject", "(Geen onderwerp)"))
                sender = _decode_header_value(msg.get("From", "(Onbekende afzender)"))
                html_content = _extract_html_body(msg)
                plain_content = _extract_plain_body(msg)

                if not html_content and not plain_content:
                    logger.warning(f"Geen content in e-mail: {subject}")
                    continue

                # Als er geen HTML is, wikkel plain text in basis-HTML
                if not html_content and plain_content:
                    html_content = f"<html><body><pre>{plain_content}</pre></body></html>"

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

    finally:
        mail.logout()

    logger.info(f"Totaal {len(newsletters)} nieuwsbrief(ven) opgehaald.")
    return newsletters
