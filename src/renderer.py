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


def render_cover_page(
    newsletters: list[dict],
    toc_entries: list[dict],
    masthead_title: str | None = None,
    masthead_subtitle: str | None = None,
    edition_label: str | None = None,
) -> str:
    """
    Render het voorblad + inhoudsopgave als HTML.

    Args:
        newsletters: Lijst van nieuwsbrief-dicts.
        toc_entries: Lijst van dicts met 'subject', 'sender', 'description'.
        masthead_title: Optionele titel i.p.v. "De Dagkrant" (bv. voor een magazine).
        masthead_subtitle: Optionele ondertitel i.p.v. de standaardtekst.
        edition_label: Optioneel label i.p.v. "Editie #N" (bv. een datumbereik).

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
        masthead_title=masthead_title,
        masthead_subtitle=masthead_subtitle,
        edition_label=edition_label,
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
                <span class="article-number">Nr. {i + 1}</span>
                <h2 class="newsletter-title">{nl.get('display_subject', nl['subject'])}</h2>
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

        /* Forceer een witte achtergrond op het HELE document. Sommige
           nieuwsbrieven bevatten een <style>-blok met een globale
           `body {{ background: ... }}`-regel die anders over alle pagina's
           lekt — inclusief de voorpagina (geobserveerd: roze cover). */
        html, body {{
            background: #ffffff !important;
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

        /* Links: donkere kleur voor print, maar wel zichtbaar als link */
        a {{
            color: #1a365d !important;
            text-decoration: underline;
        }}

        .newsletter-header {{
            padding: 10px 12px 10px 14px;
            margin-bottom: 20px;
            page-break-after: avoid;
            border-left: 4px solid #1a365d;
            background: #f8f7f5;
        }}

        .article-number {{
            display: block;
            font-size: 10px;
            font-weight: bold;
            color: #1a365d;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            margin-bottom: 4px;
            opacity: 0.75;
        }}

        .newsletter-title {{
            margin: 0 0 4px 0;
            font-size: 20px;
            font-weight: bold;
            color: #1a365d;
            line-height: 1.3;
        }}

        .newsletter-sender {{
            margin: 0;
            font-size: 10px;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 0.5px;
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
            max-height: 30vh !important;
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


def _embed_cover_thumbnail(pdf_path: str, screenshot_png: bytes) -> None:
    """
    Embed een miniatuurafbeelding van het voorblad als /Thumb in de eerste PDF-pagina.
    Kindle gebruikt dit als bibliotheekafbeelding.
    """
    try:
        import io
        from PIL import Image
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

        # Schaal naar Kindle-thumbnailformaat
        img = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
        img.thumbnail((256, 362), Image.LANCZOS)
        w, h = img.size

        # Raw RGB-pixeldata; pypdf schrijft dit als ongecomprimeerde stream
        raw_bytes = img.tobytes()

        reader = PdfReader(pdf_path)
        writer = PdfWriter()
        writer.append(reader)
        writer.add_metadata({"/Title": "De Dagkrant"})

        # Image XObject voor de thumbnail
        thumb = DecodedStreamObject()
        thumb.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Width"): NumberObject(w),
            NameObject("/Height"): NumberObject(h),
        })
        thumb.set_data(raw_bytes)

        writer.pages[0][NameObject("/Thumb")] = writer._add_object(thumb)

        with open(pdf_path, "wb") as f:
            writer.write(f)

        logger.info(f"Kindle-thumbnail ingebed ({w}×{h}px)")
    except Exception as e:
        logger.warning(f"Kindle-thumbnail niet ingebed: {e}")


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

            # Timeout op 60s: grote edities met veel externe afbeeldingen
            # kunnen langer duren dan de Playwright-default van 30s.
            page.goto(
                f"file:///{temp_html_path}",
                wait_until="networkidle",
                timeout=60000,
            )
            # Wacht even zodat alle afbeeldingen geladen kunnen worden
            page.wait_for_timeout(2000)

            # Screenshot van voorblad voor Kindle-bibliotheekthumbnail (A4-breedte viewport)
            cover_screenshot = page.screenshot(clip={"x": 0, "y": 0, "width": 794, "height": 1123})

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

        _embed_cover_thumbnail(output_path, cover_screenshot)

        # Valideer het gegenereerde PDF: log een waarschuwing als het
        # aantal pagina's veel lager is dan verwacht (rendering-crash detectie).
        try:
            from pypdf import PdfReader as _PdfReader
            _reader = _PdfReader(output_path)
            actual_pages = len(_reader.pages)
            file_kb = os.path.getsize(output_path) / 1024
            logger.info(f"PDF gegenereerd: {actual_pages} pagina's, {file_kb:.0f} KB → {output_path}")
        except Exception:
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
    subject: str | None = None,
    body: str | None = None,
    filename: str | None = None,
) -> None:
    """Verstuur de PDF als e-mailbijlage.

    `subject`, `body` en `filename` zijn optioneel en overschrijven de standaard
    "Dagkrant Editie #N"-teksten (gebruikt door bv. een magazine-run).
    """
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    now = datetime.now(timezone.utc)
    edition = _get_edition_number()
    subject = subject or f"De Dagkrant - Editie #{edition} - {_format_dutch_date(now)}"

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = subject

    body = body or (
        f"Goedemiddag!\n\n"
        f"Hierbij de Dagkrant van vandaag (Editie #{edition}).\n"
        f"Veel leesplezier!\n\n"
        f"Met vriendelijke groet,\n"
        f"De Dagkrant"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    filename = filename or f"Dagkrant_Editie_{edition}_{now.strftime('%Y%m%d')}.pdf"
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
