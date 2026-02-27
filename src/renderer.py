"""
renderer.py - PDF-generatie module.

Combineert het voorblad, de inhoudsopgave en alle nieuwsbrieven tot één HTML-document
en rendert dit naar PDF met Playwright (Chromium).
"""

import locale
import logging
import os
import tempfile
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

# Nederlandse dag- en maandnamen (onafhankelijk van systeemlocale)
_NL_DAYS = {
    "Monday": "maandag", "Tuesday": "dinsdag", "Wednesday": "woensdag",
    "Thursday": "donderdag", "Friday": "vrijdag", "Saturday": "zaterdag",
    "Sunday": "zondag",
}
_NL_MONTHS = {
    "January": "januari", "February": "februari", "March": "maart",
    "April": "april", "May": "mei", "June": "juni",
    "July": "juli", "August": "augustus", "September": "september",
    "October": "oktober", "November": "november", "December": "december",
}


def _format_dutch_date(dt: datetime) -> str:
    """Formatteer een datetime naar Nederlandse datum: 'donderdag 05 februari 2026'."""
    en_date = dt.strftime("%A %d %B %Y")
    for en, nl in _NL_DAYS.items():
        en_date = en_date.replace(en, nl)
    for en, nl in _NL_MONTHS.items():
        en_date = en_date.replace(en, nl)
    return en_date

logger = logging.getLogger(__name__)

# Pad naar de templates-map
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")


def _get_edition_number() -> int:
    """
    Bereken het editienummer op basis van het aantal dagen sinds de start.
    Startdatum: 2025-01-01 (pas aan naar je eigen startdatum).
    """
    start_date = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - start_date).days + 1


def render_cover_page(newsletters: list[dict], toc_entries: list[dict]) -> str:
    """
    Render het voorblad + inhoudsopgave als HTML.

    Args:
        newsletters: Lijst van nieuwsbrief-dicts.
        toc_entries: Lijst van dicts met 'subject', 'sender', 'description'.

    Returns:
        HTML-string van het voorblad.
    """
    env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR))
    template = env.get_template("cover.html")

    now = datetime.now(timezone.utc)
    return template.render(
        date=_format_dutch_date(now),
        edition_number=_get_edition_number(),
        newsletter_count=len(newsletters),
        toc_entries=toc_entries,
    )


def compose_full_html(cover_html: str, newsletters: list[dict]) -> str:
    """
    Combineer voorblad en nieuwsbrieven tot één lang HTML-document met page breaks.

    Args:
        cover_html: HTML van het voorblad.
        newsletters: Lijst van nieuwsbrief-dicts met 'html_content' en 'subject'.

    Returns:
        Volledig gecombineerd HTML-document.
    """
    sections = [cover_html]

    for i, nl in enumerate(newsletters):
        anchor_id = f"newsletter-{i + 1}"
        section = f"""
        <div class="newsletter-section" id="{anchor_id}" style="page-break-before: always;">
            <div class="newsletter-header">
                <h2 class="newsletter-title">{nl['subject']}</h2>
                <p class="newsletter-sender">{nl['sender']}</p>
            </div>
            <div class="newsletter-content">
                {nl['html_content']}
            </div>
        </div>
        """
        sections.append(section)

    combined_body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        @page {{
            size: A4;
            margin: 15mm;
        }}

        body {{
            font-family: Georgia, 'Times New Roman', serif;
            line-height: 1.6;
            color: #1a1a1a;
            max-width: 100%;
            margin: 0;
            padding: 0;
        }}

        .newsletter-section {{
            page-break-before: always;
        }}

        .newsletter-header {{
            color: #222222;
            padding: 0;
            margin-bottom: 20px;
            page-break-after: avoid;
        }}

        .newsletter-title {{
            margin: 0 0 5px 0;
            font-size: 20px;
            font-weight: bold;
            color: #222222;
            border-bottom: 1px solid #cccccc;
            padding-bottom: 8px;
            margin-bottom: 15px;
        }}

        .newsletter-sender {{
            margin: 0;
            font-size: 12px;
            color: #666666;
        }}

        .newsletter-content {{
            padding: 10px 0;
        }}

        /* --- Fix 1: Anti-gatenkaas CSS --- */
        /* Afbeeldingen: responsief, nooit te breed, gecentreerd */
        img {{
            max-width: 100% !important;
            height: auto !important;
            display: block;
            margin: 0 auto;
        }}

        .newsletter-content img {{
            max-width: 100% !important;
            max-height: 45vh !important;
            height: auto !important;
            width: auto !important;
            object-fit: contain;
            display: block;
            margin: 0 auto;
            page-break-inside: auto;
        }}

        .newsletter-content table {{
            max-width: 100% !important;
            page-break-inside: auto !important;
        }}

        .newsletter-content tr {{
            page-break-inside: auto !important;
        }}

        .newsletter-content td {{
            page-break-inside: auto !important;
        }}

        /* Laat tekst DOORLOPEN - alleen headers beschermen */
        h1, h2, h3, h4, h5, h6 {{
            page-break-after: avoid;
            page-break-inside: avoid;
        }}

        /* Drop-cap fix: forceer inline weergave van NRC-specifieke spans */
        .newsletter-content span[style*="font-size"] {{
            display: inline !important;
            float: none !important;
            line-height: inherit !important;
        }}

        /* Paragrafen en lijstitems mogen WEL gesplitst worden */
        .newsletter-content p,
        .newsletter-content li,
        .newsletter-content div {{
            page-break-inside: auto !important;
            orphans: 3;
            widows: 3;
        }}

        /* Alleen figure en blockquote beschermen */
        figure, blockquote {{
            page-break-inside: avoid;
        }}

        /* Verberg overflow van te brede nieuwsbrief-elementen */
        .newsletter-content > * {{
            max-width: 100% !important;
            overflow: hidden;
        }}

        /* Forceer dat nested tables niet de layout breken */
        .newsletter-content table table {{
            width: 100% !important;
        }}

        /* Voorkom lege pagina na het laatste artikel */
        .newsletter-section:last-child {{
            page-break-after: avoid;
        }}

        /* Afkap-noot stijl */
        .truncation-note {{
            color: #888;
            font-style: italic;
            margin-top: 20px;
            border-top: 1px solid #ddd;
            padding-top: 8px;
            font-size: 11px;
        }}
    </style>
</head>
<body>
{combined_body}
</body>
</html>"""


def render_pdf(html_content: str, output_path: str) -> str:
    """
    Render HTML naar PDF met Playwright (Chromium).

    Args:
        html_content: Het volledige HTML-document.
        output_path: Pad waar de PDF wordt opgeslagen.

    Returns:
        Pad naar het gegenereerde PDF-bestand.
    """
    logger.info("PDF renderen met Playwright...")

    # Schrijf HTML naar een tijdelijk bestand voor Playwright
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as f:
        f.write(html_content)
        temp_html_path = f.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"file:///{temp_html_path}", wait_until="networkidle")
            # Wacht even zodat alle afbeeldingen geladen kunnen worden
            page.wait_for_timeout(2000)

            page.pdf(
                path=output_path,
                format="A4",
                margin={
                    "top": "15mm",
                    "right": "15mm",
                    "bottom": "15mm",
                    "left": "15mm",
                },
                print_background=True,
            )
            browser.close()

        logger.info(f"PDF gegenereerd: {output_path}")
        return output_path

    finally:
        os.unlink(temp_html_path)


def send_email_with_pdf(
    pdf_path: str,
    sender_email: str,
    sender_password: str,
    recipient_email: str,
    smtp_server: str = "smtp.gmail.com",
    smtp_port: int = 587,
) -> None:
    """Verstuur de PDF als e-mailbijlage."""
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    now = datetime.now(timezone.utc)
    edition = _get_edition_number()
    subject = f"De Dagkrant - Editie #{edition} - {_format_dutch_date(now)}"

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = subject

    body = (
        f"Goedemiddag!\n\n"
        f"Hierbij de Dagkrant van vandaag (Editie #{edition}).\n"
        f"Veel leesplezier!\n\n"
        f"Met vriendelijke groet,\n"
        f"De Dagkrant"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    filename = f"Dagkrant_Editie_{edition}_{now.strftime('%Y%m%d')}.pdf"
    with open(pdf_path, "rb") as f:
        pdf_attachment = MIMEApplication(f.read(), _subtype="pdf")
        pdf_attachment.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(pdf_attachment)

    logger.info(f"E-mail verzenden naar {recipient_email}...")

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)

    logger.info("E-mail succesvol verzonden!")
