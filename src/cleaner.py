"""
cleaner.py - HTML-opschoonmodule voor nieuwsbrieven.

Verwijdert technische rommel (MSO-conditionals, scripts, tracking pixels),
footer-vervuiling (unsubscribe-secties, adresblokken), navigatie-rommel,
placeholder-tekst, en normaliseert de HTML voor schone PDF-rendering.
"""

import logging
import re
from difflib import SequenceMatcher

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger(__name__)

# --- KILL-LISTS ---

# Patronen voor footer-detectie (case-insensitive)
_FOOTER_MARKERS = [
    r"uitschrijven",
    r"afmelden",
    r"unsubscribe",
    r"opt[\s-]?out",
    r"manage\s+(your\s+)?preferences",
    r"email\s+preferences",
    r"e-?mailvoorkeuren",
    r"wijzig\s+je\s+",
    r"update\s+(your\s+)?preferences",
    r"sent\s+(by|to)\s+",
    r"verzonden\s+(door|naar)",
    r"you\s+received\s+this",
    r"je\s+ontvangt\s+dit",
    r"mailing\s+address",
    r"no\s+longer\s+wish\s+to\s+receive",
    r"this\s+email\s+was\s+sent\s+to",
    r"add\s+us\s+to\s+your\s+address\s+book",
    r"forward\s+this\s+email",
    r"powered\s+by\s+(mailchimp|substack|convertkit|beehiiv|revue)",
    r"©\s*\d{4}",
    r"all\s+rights\s+reserved",
    r"alle\s+rechten\s+voorbehouden",
    r"privacy\s+policy",
    r"privacybeleid",
    r"terms\s+of\s+(service|use)",
]

_FOOTER_PATTERN = re.compile("|".join(_FOOTER_MARKERS), re.IGNORECASE)

# Keyword kill-list: verwijder elementen die deze tekst bevatten
_KILLLIST_EXACT = [
    # Browser-view / app prompts
    r"bekijk\s+(in|deze\s+e-?mail\s+in)\s+(je\s+|uw\s+)?browser",
    r"view\s+(in|this\s+email\s+in)\s+(your\s+)?browser",
    r"lees\s+in\s+de\s+app",
    r"read\s+in\s+(the\s+)?app",
    r"open\s+in\s+(the\s+)?app",
    r"bekijk\s+de\s+webversie",
    r"view\s+online",
    # Social / share prompts
    r"^share$",
    r"^restack$",
    r"^liken$",
    r"^like$",
    # Subscription / action buttons
    r"^start\s+writing$",
    r"^subscribe$",
    r"^abonneren$",
    r"^download\s+de\s+app$",
    r"^download\s+the\s+app$",
    r"^get\s+the\s+app$",
    # Readwise / app-specific
    r"klik\s+hier\s+om\s+over\s+te\s+schakelen",
    r"switch\s+to\s+the\s+.*?web\s*app",
    r"update\(?s?\)?\s+in\s+deze\s+e-?mail",
    # Placeholder / spam content
    r"welkom\s+bij\s+onze\s+website",
    r"onze\s+diensten:\s+webontwikkeling",
    r"webontwikkeling",
    r"web\s*design",
    r"zoekmachine\s*optimalisatie",
    r"seo\s+diensten",
    # Extra banned phrases
    r"bekijk\s+deze\s+e-?mail\s+in\s+uw\s+browser",
    r"bekijk\s+deze\s+email\s+in\s+uw\s+browser",
    r"can.?t\s+see\s+this\s+email",
    r"trouble\s+viewing",
    r"email\s+not\s+displaying",
    r"view\s+this\s+email",
    r"images\s+not\s+showing",
    r"afbeeldingen\s+worden\s+niet\s+getoond",
    r"click\s+here\s+to\s+view",
    r"klik\s+hier\s+om\s+te\s+bekijken",
]

_KILLLIST_PATTERN = re.compile("|".join(_KILLLIST_EXACT), re.IGNORECASE)

# Button-teksten die verwijderd moeten worden
_BUTTON_KILL_TEXTS = {
    "download", "subscribe", "abonneren", "start writing",
    "get the app", "download the app", "download de app",
    "lees in de app", "read in the app", "open in app",
    "share", "restack", "like", "liken",
    "google play", "app store",
}


def clean_html(html_content: str) -> str:
    """
    Schoon HTML op voor PDF-rendering.

    Verwijdert:
    - MSO/Outlook conditionals
    - HTML-comments
    - <script> en <noscript> tags
    - Tracking pixels (1x1 afbeeldingen)
    - Kill-list elementen (browser-view, social, buttons, placeholders)
    - Footer/unsubscribe secties
    - Lege containers

    Args:
        html_content: Ruwe HTML van een nieuwsbrief.

    Returns:
        Opgeschoonde HTML.
    """
    if not html_content:
        return html_content

    # Stap 1: Verwijder MSO conditionals vóór BeautifulSoup parsing
    html_content = _remove_mso_conditionals(html_content)

    soup = BeautifulSoup(html_content, "html.parser")

    # Stap 2: Verwijder HTML-comments
    _remove_comments(soup)

    # Stap 3: Verwijder script tags
    _remove_tags(soup, ["script", "noscript"])

    # Stap 4: Verwijder tracking pixels
    _remove_tracking_pixels(soup)

    # Stap 5: Verwijder kill-list elementen (header-rommel, placeholders, buttons)
    _remove_killlisted_elements(soup)

    # Stap 6: Verwijder footer-secties
    _remove_footers(soup)

    # Stap 7: Verwijder lege containers
    _remove_empty_containers(soup)

    return str(soup)


def deduplicate_title(html_content: str, subject: str) -> str:
    """
    Verwijder dubbele titels: als het subject van de e-mail sterk overeenkomt
    met de eerste h1/h2 in de body, verberg dan die h1/h2.

    Args:
        html_content: De opgeschoonde HTML-content.
        subject: Het e-mail onderwerp (dat wij als header boven het artikel tonen).

    Returns:
        HTML met de dubbele titel verborgen.
    """
    if not html_content or not subject:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")

    # Zoek de eerste h1 of h2 in de content
    for tag_name in ["h1", "h2"]:
        heading = soup.find(tag_name)
        if heading and heading.parent is not None:
            heading_text = heading.get_text(strip=True)
            if not heading_text:
                continue

            # Bereken overeenkomst
            similarity = SequenceMatcher(
                None,
                subject.lower().strip(),
                heading_text.lower().strip(),
            ).ratio()

            if similarity > 0.6:
                # Verberg de heading (display:none) i.p.v. verwijderen
                # zodat de structuur intact blijft
                heading["style"] = heading.get("style", "") + "; display: none !important;"
                logger.info(f"    Dubbele titel verborgen: '{heading_text[:50]}' ({similarity:.0%} match)")
                return str(soup)

    return html_content


# --- PRIVATE FUNCTIES ---

def _remove_mso_conditionals(html: str) -> str:
    """
    Verwijder Microsoft Office/Outlook conditionals.
    Behoudt content in <!--[if !mso]><!--> blokken (niet-Outlook).
    Verwijdert <!--[if gte mso X]> blokken volledig.
    """
    # Stap 1: Bewaar niet-mso content, strip alleen de wrapper-comments
    html = re.sub(r"<!--\[if\s+!mso\]><!-->", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<!--<!\[endif\]-->", "", html, flags=re.IGNORECASE)

    # Stap 2: Verwijder MSO-only blokken volledig
    html = re.sub(
        r"<!--\[if[^\]!]*\]>.*?<!\[endif\]-->",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Stap 3: Ruim loshangende resten op
    html = re.sub(r"<!--\[if[^\]]*\]>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<!\[endif\]-->", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<!\[endif\]>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"\[if\s+[!\w\s]+\]>", "", html, flags=re.IGNORECASE)

    return html


def _remove_comments(soup: BeautifulSoup) -> None:
    """Verwijder alle HTML-comments."""
    for comment in list(soup.find_all(string=lambda text: isinstance(text, Comment))):
        comment.extract()


def _remove_tags(soup: BeautifulSoup, tag_names: list[str]) -> None:
    """Verwijder specifieke HTML-tags inclusief hun content."""
    for tag_name in tag_names:
        for tag in list(soup.find_all(tag_name)):
            if tag.parent is not None:
                tag.decompose()


def _remove_tracking_pixels(soup: BeautifulSoup) -> None:
    """Verwijder tracking pixels (1x1 of zeer kleine onzichtbare afbeeldingen)."""
    for img in list(soup.find_all("img")):
        if img.parent is None:
            continue
        try:
            width = img.get("width", "")
            height = img.get("height", "")
            style = img.get("style", "")
        except Exception:
            continue

        is_tiny = False

        try:
            if width and int(str(width).replace("px", "")) <= 3:
                is_tiny = True
            if height and int(str(height).replace("px", "")) <= 3:
                is_tiny = True
        except (ValueError, TypeError):
            pass

        if style and re.search(r"(width|height)\s*:\s*[01](px)?", style):
            is_tiny = True
        if style and re.search(r"display\s*:\s*none", style, re.IGNORECASE):
            is_tiny = True

        if is_tiny:
            img.decompose()


def _remove_killlisted_elements(soup: BeautifulSoup) -> None:
    """
    Verwijder elementen die kill-list teksten bevatten:
    - Browser-view prompts ("Bekijk in browser")
    - Social buttons ("Share", "Restack")
    - App download prompts ("Download the app", "Google Play")
    - Placeholder/spam tekst ("Welkom bij onze website")
    - Action buttons ("Subscribe", "Start writing")
    """
    # PASS 1: Zoek in ALLE tekst-bevattende elementen (geen lengte-limiet meer!)
    for elem in list(soup.find_all(["div", "p", "span", "td", "a", "table", "tr",
                                     "section", "center", "li", "h1", "h2", "h3", "h4"])):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue

        if not text:
            continue

        # Voor korte teksten (<= 500 chars): check tegen kill-list en verwijder hele element
        if len(text) <= 500 and _KILLLIST_PATTERN.search(text):
            target = _find_smallest_killable_parent(elem)
            if target and target.parent is not None:
                target.decompose()
                continue

        # Voor langere teksten: check of de EERSTE 200 chars de kill-pattern bevatten
        # (header-rommel staat altijd bovenaan)
        if len(text) > 500:
            first_chunk = text[:200]
            if _KILLLIST_PATTERN.search(first_chunk):
                # Probeer alleen het eerste child-element te verwijderen
                for child in list(elem.children):
                    if child.parent is None:
                        continue
                    if hasattr(child, 'get_text'):
                        child_text = child.get_text(strip=True)
                        if child_text and _KILLLIST_PATTERN.search(child_text) and len(child_text) <= 500:
                            child.decompose()
                            break

    # PASS 2: Verwijder specifieke button-achtige links
    for a_tag in list(soup.find_all("a")):
        if a_tag.parent is None:
            continue
        try:
            link_text = a_tag.get_text(strip=True).lower()
        except Exception:
            continue
        if link_text in _BUTTON_KILL_TEXTS:
            parent = a_tag.parent
            a_tag.decompose()
            if parent and parent.parent is not None:
                try:
                    remaining = parent.get_text(strip=True)
                    if not remaining:
                        parent.decompose()
                except Exception:
                    pass

    # PASS 3: Brute-force tekst-scan voor hardnekkige spook-tekst
    # Zoek letterlijk naar "Welkom bij onze website" in ALLE elementen
    _GHOST_PHRASES = [
        "welkom bij onze website",
        "onze diensten: webontwikkeling",
        "onze diensten:",
        "webontwikkeling",
    ]
    for elem in list(soup.find_all(True)):
        if elem.parent is None:
            continue
        try:
            own_text = elem.string  # directe tekst, niet van kinderen
            if own_text and any(phrase in own_text.lower() for phrase in _GHOST_PHRASES):
                # Verwijder het element en zijn parent als die klein genoeg is
                target = _find_smallest_killable_parent(elem)
                if target and target.parent is not None:
                    logger.info(f"    Spook-tekst verwijderd: '{own_text[:80]}...'")
                    target.decompose()
                    continue
        except Exception:
            continue
        # Check ook get_text voor geneste tekst
        try:
            full_text = elem.get_text(strip=True).lower()
            if len(full_text) < 600 and any(phrase in full_text for phrase in _GHOST_PHRASES):
                target = _find_smallest_killable_parent(elem)
                if target and target.parent is not None:
                    logger.info(f"    Spook-tekst verwijderd (genest): '{full_text[:80]}...'")
                    target.decompose()
        except Exception:
            continue


def _find_smallest_killable_parent(elem) -> object:
    """
    Zoek het kleinste parent-element dat veilig verwijderd kan worden.
    Climbt omhoog zolang de parent niet te groot is en hetzelfde
    kill-list patroon bevat.
    """
    target = elem
    while target.parent and target.parent.name not in ["body", "html", "[document]"]:
        if target.parent.parent is None:
            break
        try:
            parent_text = target.parent.get_text(strip=True)
        except Exception:
            break
        # Als de parent kort genoeg is, neem die als verwijder-target
        # Verhoogd van 200 naar 500 om grotere containers te pakken
        if len(parent_text) < 500:
            target = target.parent
        else:
            break
    return target


def _remove_footers(soup: BeautifulSoup) -> None:
    """
    Verwijder footer-secties die unsubscribe links, adressen etc. bevatten.
    Zoekt van onderaf en verwijdert alles vanaf het eerste footer-marker.
    """
    body = soup.find("body") or soup
    all_elements = list(body.find_all(["div", "table", "tr", "td", "p", "section", "footer"]))

    footer_elements = []
    for elem in reversed(all_elements):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue
        if len(text) < 5:
            continue
        if _FOOTER_PATTERN.search(text):
            footer_elements.append(elem)

    if not footer_elements:
        return

    for elem in footer_elements:
        if elem.parent is None:
            continue

        parent = elem
        while parent.parent and parent.parent.name not in ["body", "html", "[document]"]:
            if parent.parent.parent is None:
                break
            try:
                parent_text = parent.parent.get_text(strip=True)
            except Exception:
                break
            if _FOOTER_PATTERN.search(parent_text) and len(parent_text) < 2000:
                parent = parent.parent
            else:
                break

        try:
            if parent.parent is not None and len(parent.get_text(strip=True)) < 1500:
                parent.decompose()
        except Exception:
            pass


def _remove_empty_containers(soup: BeautifulSoup) -> None:
    """Verwijder lege div/td/tr elementen die alleen whitespace bevatten."""
    for tag in list(soup.find_all(["div", "td", "tr", "span"])):
        if tag.parent is None:
            continue
        if tag.find_all(["img", "a", "input", "button", "video", "iframe"]):
            continue
        try:
            text = tag.get_text(strip=True)
            if not text and not tag.find_all(True):
                tag.decompose()
        except Exception:
            continue
