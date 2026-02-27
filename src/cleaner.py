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
    # Fysieke adressen
    r"suite\s+\d+",
    r"\d+\s+\w+\s+(drive|dr|street|st|avenue|ave|boulevard|blvd|road|rd)\b",
    # Generieke "powered by" en "sent by"
    r"powered\s+by\b",
    r"sent\s+by\b",
    # Spam-diensten
    r"\bicegram\b",
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
    # Website-template patronen (gegenereerde placeholder-pagina's)
    r"welkom\s+op\s+onze\s+website",
    r"neem\s+contact\s+met\s+ons\s+op\s+via\s+ons\s+e-?mailadres",
    r"onze\s+missie.*wij\s+streven",
    r"voorbeeldbedrijf\.\s*alle\s+rechten",
    # --- TASK 4: Aanvullende boilerplate & UI kill-list ---
    r"favorite\s*/\s*discard\s*/\s*tag\s+or\s+share",
    r"op\s+de\s+blog\s+of\s+reader\s+lezen",
    r"lees\s+verder",
    r"read\s+full\s+story",
    r"^reactie$",
    r"deze\s+e-?mail\s+doorgestuurd\??\s+abonneer",
    r"forwarded\s+this\s+email\??\s+subscribe",
    r"change\s+your\s+email\s+preferences\s*\|?\s*unsubscribe",
    r"voor\s+alle\s+plus-abonnees",
    r"^webversie$",
    r"^advertentie\s*2?$",
    r"listen\s+now",
    r"preview\s+0:00",
    r"upgrade\s+to\s+paid",
    r"claim\s+my\s+free\s+post",
    r"nrc>",
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


# Boilerplate-intro's van bekende nieuwsbrieven
_BOILERPLATE_INTROS = [
    re.compile(r"de\s+ai[-\s]wereld\s+ontwikkelt\s+zich\s+razendsnel", re.IGNORECASE),
    re.compile(r"tag,?\s+favorite,?\s+share,?\s+track\s+your\s+progress", re.IGNORECASE),
]


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
    - Gebruikershandtekening en disclaimers
    - NRC drop-cap spans en promo-footer
    - Lege containers

    Args:
        html_content: Ruwe HTML van een nieuwsbrief.

    Returns:
        Opgeschoonde HTML.
    """
    if not html_content:
        return html_content

    # Stap 0: Verwijder hardnekkige spooktekst op string-niveau (vóór parsing)
    html_content = _remove_ghost_text_raw(html_content)

    # Stap 0b: Verwijder markdown code-block artefacten van AI-vertaling
    html_content = _remove_ai_artifacts_raw(html_content)

    # Stap 1: Verwijder MSO conditionals vóór BeautifulSoup parsing
    html_content = _remove_mso_conditionals(html_content)

    soup = BeautifulSoup(html_content, "html.parser")

    # Stap 2: Verwijder forwarding-headers en disclaimers
    _remove_forwarding_headers(soup)

    # Stap 2b: Verwijder gebruikershandtekening en institutionele disclaimers
    _remove_user_signature(soup)

    # Stap 3: Verwijder HTML-comments
    _remove_comments(soup)

    # Stap 4: Verwijder script tags
    _remove_tags(soup, ["script", "noscript"])

    # Stap 4b: Verwijder loshangende "html" artefacten
    _remove_html_artifact(soup)

    # Stap 5: Verwijder tracking pixels
    _remove_tracking_pixels(soup)

    # Stap 5b: NRC-specifiek: verwijder drop-cap spans/tables en promo-footer
    _flatten_nrc_drop_caps(soup)
    _remove_nrc_promo_footer(soup)

    # Stap 6: Verwijder kill-list elementen (header-rommel, placeholders, buttons)
    _remove_killlisted_elements(soup)

    # Stap 6b: Verwijder bekende boilerplate-intro's
    _remove_boilerplate_intros(soup)

    # Stap 7: Verwijder ADVERTENTIE-blokken
    _remove_advertisements(soup)

    # Stap 8: Verwijder footer-secties
    _remove_footers(soup)

    # Stap 9: Verwijder lege containers
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

def _remove_ai_artifacts_raw(html: str) -> str:
    """
    Verwijder markdown code-block artefacten die de AI-vertaler soms achterlaat.
    Bijv. loshangende "```html", "```", of de string "html" alleen op een regel.
    Handles ook geneste/dubbele code fences zoals "```html ```html" die ontstaan
    als de AI-vertaler code fences toevoegt aan al bestaande fenced content.
    """
    # Stap 1: Verwijder geneste/dubbele code fences (bijv. "```html ```html")
    html = re.sub(r"(`{3,}html\s*){2,}", "", html, flags=re.IGNORECASE)
    # Stap 2: Verwijder enkelvoudige ```html en ``` code fence markers
    html = re.sub(r"```+html\s*", "", html, flags=re.IGNORECASE)
    html = re.sub(r"```+\s*", "", html)
    # Stap 3: Verwijder een loshangende "html" tag die als tekst-node staat
    html = re.sub(r"^\s*html\s*$", "", html, flags=re.MULTILINE | re.IGNORECASE)
    return html


def _remove_ghost_text_raw(html: str) -> str:
    """
    Verwijder hardnekkige placeholder/spooktekst op string-niveau.
    Dit vangt gevallen die BeautifulSoup mist (bijv. tekst verborgen in
    diep geneste tabellen, inline styles, of ongewone encodering).
    Detecteert ook generieke website-templates (bijv. "Welkom op onze website!")
    die per ongeluk worden opgehaald in plaats van de nieuwsbrief-content.
    """
    _GHOST_REGEXES = [
        # Verwijder volledige HTML-elementen die de spooktekst bevatten
        re.compile(
            r"<[^>]*>[^<]*welkom\s+(bij|op)\s+onze\s+website[^<]*</[^>]+>",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"<[^>]*>[^<]*onze\s+diensten\s*:?\s*webontwikkeling[^<]*</[^>]+>",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"<[^>]*>[^<]*zoekmachine\s*optimalisatie[^<]*</[^>]+>",
            re.IGNORECASE | re.DOTALL,
        ),
        # Plain-text fallback: verwijder de zinnen zelf
        re.compile(r"welkom\s+(bij|op)\s+onze\s+website!?", re.IGNORECASE),
        re.compile(r"onze\s+diensten\s*:?\s*webontwikkeling", re.IGNORECASE),
        re.compile(r"neem\s+contact\s+met\s+ons\s+op", re.IGNORECASE),
        re.compile(r"©\s*\d{4}\s*\w+\s*\.\s*alle\s+rechten\s+voorbehouden", re.IGNORECASE),
    ]
    for pattern in _GHOST_REGEXES:
        html = pattern.sub("", html)
    return html


def is_website_template(html: str) -> bool:
    """
    Detecteer of de HTML-content een generieke website-template is
    (bijv. een webpagina die per ongeluk is opgehaald in plaats van een nieuwsbrief).

    Returns:
        True als de content op een generieke website-template lijkt.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True).lower()

    # Combinatie van typische website-navigatie + placeholder-content
    _TEMPLATE_SIGNALS = [
        r"welkom\s+(bij|op)\s+onze\s+website",
        r"home\s*\|\s*over\s+ons\s*\|\s*diensten",
        r"neem\s+contact\s+met\s+ons\s+op.*e-?mailadres",
        r"onze\s+missie.*wij\s+streven",
        r"voorbeeldbedrijf\.\s*alle\s+rechten\s+voorbehouden",
    ]
    matches = sum(
        1 for p in _TEMPLATE_SIGNALS
        if re.search(p, text, re.IGNORECASE | re.DOTALL)
    )
    return matches >= 2


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


def _remove_html_artifact(soup: BeautifulSoup) -> None:
    """
    Verwijder loshangende 'html' tekst-artefacten.
    Dit ontstaat wanneer geneste <html> tags in e-mail content door
    BeautifulSoup worden omgezet naar kale tekst.
    """
    from bs4 import NavigableString

    # Verwijder geneste <html> tags (binnen de body)
    body = soup.find("body")
    if body:
        for nested_html in list(body.find_all("html")):
            if nested_html.parent is not None:
                nested_html.unwrap()  # Bewaar content, verwijder de tag

    # Verwijder kale NavigableString nodes die alleen "html" bevatten
    for text_node in list(soup.find_all(string=True)):
        if isinstance(text_node, NavigableString):
            stripped = text_node.strip().lower()
            if stripped == "html":
                logger.info("    HTML-artefact verwijderd: loshangende 'html' tekst")
                text_node.extract()


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


def _remove_forwarding_headers(soup: BeautifulSoup) -> None:
    """
    Verwijder standaard e-mail forwarding blokken en disclaimers.
    Patronen: "---------- Forwarded message ---------", "Oorspronkelijk van:",
    "From: ... Date: ... Subject: ... To: ...", "Dit e-mailbericht is uitsluitend bestemd voor..."
    """
    _FORWARD_PATTERNS = [
        re.compile(r"-{3,}\s*forwarded\s+message\s*-{3,}", re.IGNORECASE),
        re.compile(r"-{3,}\s*doorgestuurd\s+bericht\s*-{3,}", re.IGNORECASE),
        re.compile(r"oorspronkelijk\s+(van|bericht)\s*:", re.IGNORECASE),
        re.compile(r"^van\s*:.*datum\s*:.*onderwerp\s*:", re.IGNORECASE | re.DOTALL),
        re.compile(r"^from\s*:.*date\s*:.*subject\s*:.*to\s*:", re.IGNORECASE | re.DOTALL),
        re.compile(r"dit\s+e-?mailbericht\s+is\s+uitsluitend\s+bestemd\s+voor", re.IGNORECASE),
        re.compile(r"dit\s+bericht\s+is\s+uitsluitend\s+bestemd", re.IGNORECASE),
        re.compile(r"this\s+e-?mail\s+is\s+(solely\s+)?intended\s+for", re.IGNORECASE),
        re.compile(r"begin\s+forwarded\s+message", re.IGNORECASE),
        re.compile(r"begin\s+doorgestuurd\s+bericht", re.IGNORECASE),
    ]

    for elem in list(soup.find_all(["div", "p", "span", "td", "tr", "table",
                                     "blockquote", "section", "pre"])):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue

        if not text or len(text) < 10:
            continue

        for pattern in _FORWARD_PATTERNS:
            if pattern.search(text):
                # Als het element relatief klein is, verwijder het geheel
                if len(text) < 1500:
                    logger.info(f"    Forward-header verwijderd: '{text[:80]}...'")
                    elem.decompose()
                else:
                    # Bij grotere elementen: probeer alleen het matchende child te verwijderen
                    for child in list(elem.children):
                        if child.parent is None:
                            continue
                        if hasattr(child, 'get_text'):
                            child_text = child.get_text(strip=True)
                            if child_text and pattern.search(child_text) and len(child_text) < 1500:
                                logger.info(f"    Forward-header (child) verwijderd: '{child_text[:80]}...'")
                                child.decompose()
                break


def _remove_boilerplate_intros(soup: BeautifulSoup) -> None:
    """
    Verwijder bekende boilerplate-intro's van specifieke nieuwsbrieven.
    Bijv. AI Report's standaard openingszin, Readwise's vaste intro.
    """
    for elem in list(soup.find_all(["div", "p", "span", "td", "section", "tr"])):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue

        if not text or len(text) > 800:
            continue

        for pattern in _BOILERPLATE_INTROS:
            if pattern.search(text):
                target = _find_smallest_killable_parent(elem)
                if target and target.parent is not None:
                    logger.info(f"    Boilerplate-intro verwijderd: '{text[:80]}...'")
                    target.decompose()
                break


def _remove_advertisements(soup: BeautifulSoup) -> None:
    """
    Verwijder elementen die 'ADVERTENTIE', 'Gesponsord' of 'Sponsored' bevatten
    als standalone blok/header. Verwijdert ook het volgende sibling-element
    (dat doorgaans de advertentie-inhoud bevat).
    """
    _AD_PATTERN = re.compile(r"^\s*(advertentie|gesponsord|sponsored)\s*$", re.IGNORECASE)

    for elem in list(soup.find_all(["div", "p", "span", "td", "h1", "h2", "h3",
                                     "h4", "h5", "h6", "section", "center"])):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue

        if _AD_PATTERN.match(text):
            # Zoek het volgende sibling-element (de advertentie-inhoud)
            next_sib = elem.find_next_sibling()
            if next_sib and next_sib.parent is not None:
                try:
                    sib_text_len = len(next_sib.get_text(strip=True))
                except Exception:
                    sib_text_len = 0
                if sib_text_len < 3000:
                    logger.info(f"    Advertentie-inhoud verwijderd (sibling, {sib_text_len} chars)")
                    next_sib.decompose()

            # Verwijder het header-element zelf + eventueel de parent als die klein is
            target = _find_smallest_killable_parent(elem)
            if target and target.parent is not None:
                logger.info(f"    Advertentie-blok verwijderd: '{text[:60]}'")
                target.decompose()


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
            if parent.parent is not None and len(parent.get_text(strip=True)) < 2000:
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


# Handtekening-patronen van de gebruiker (Fioretti College)
_SIGNATURE_PATTERNS = [
    re.compile(r"docent\s+maatschappijleer", re.IGNORECASE),
    re.compile(r"decaan\s+vwo", re.IGNORECASE),
    re.compile(r"digicoach", re.IGNORECASE),
    re.compile(r"mijn\s+werkdagen\s+zijn\s+maandag", re.IGNORECASE),
    re.compile(r"fioretti\s+college", re.IGNORECASE),
    re.compile(r"www\.fioretti\.nl", re.IGNORECASE),
    re.compile(r"dit\s+e-?mailbericht\s+is\s+uitsluitend\s+bestemd\s+voor\s+de\s+geadresseerde", re.IGNORECASE),
]


def _remove_user_signature(soup: BeautifulSoup) -> None:
    """
    Verwijder de handtekening en institutionele disclaimers van de gebruiker.
    Herkent Fioretti College-specifieke tekst en generieke disclaimers.
    """
    for elem in list(soup.find_all(["div", "p", "span", "td", "tr", "table",
                                     "blockquote", "section", "pre"])):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue

        if not text or len(text) < 5:
            continue

        for pattern in _SIGNATURE_PATTERNS:
            if pattern.search(text):
                # Verwijder het element als het klein genoeg is (handtekening-blok)
                if len(text) < 2000:
                    logger.info(f"    Handtekening/disclaimer verwijderd: '{text[:80]}...'")
                    elem.decompose()
                    break
                # Bij grotere containers: zoek het matching child
                for child in list(elem.children):
                    if child.parent is None:
                        continue
                    if hasattr(child, 'get_text'):
                        child_text = child.get_text(strip=True)
                        if child_text and pattern.search(child_text) and len(child_text) < 2000:
                            logger.info(f"    Handtekening-child verwijderd: '{child_text[:80]}...'")
                            child.decompose()
                break


def _flatten_nrc_drop_caps(soup: BeautifulSoup) -> None:
    """
    Verwijder NRC-specifieke drop-cap constructies die letters laten zweven.

    NRC gebruikt spans of tables met een grote eerste letter (drop cap).
    In PDF-rendering veroorzaakt dit grote witruimtes en zwevende letters.
    Oplossing: unwrap de spans/tables zodat de tekst aaneensluit.
    """
    # Patroon 1: Spans met display:table of float:left als drop-cap
    for span in list(soup.find_all("span")):
        if span.parent is None:
            continue
        style = span.get("style", "")
        # Drop-cap spans hebben typisch een grote font-size of float:left
        if re.search(r"float\s*:\s*left", style, re.IGNORECASE):
            # Controleer of de span slechts 1-2 tekens bevat (drop cap letter)
            text = span.get_text(strip=True)
            if len(text) <= 2:
                logger.info(f"    NRC drop-cap span afgevlakt: '{text}'")
                span.unwrap()

    # Patroon 2: Specifieke NRC table-drop-cap structuur
    # <table><tr><td style="...font-size:large...">E</td><td>n dan de rest...</td></tr></table>
    for table in list(soup.find_all("table")):
        if table.parent is None:
            continue
        rows = table.find_all("tr")
        if len(rows) != 1:
            continue
        cells = rows[0].find_all("td")
        if len(cells) != 2:
            continue

        first_cell_text = cells[0].get_text(strip=True)
        first_cell_style = cells[0].get("style", "")

        # Als de eerste cel maar 1 teken bevat met grote font-size → drop cap table
        if (len(first_cell_text) <= 2 and
                re.search(r"font-size\s*:\s*(\d{2,}px|[3-9]\d*pt|xx?-large|[3-9]\d*em)", first_cell_style, re.IGNORECASE)):
            # Vervang de hele table door de gecombineerde tekst als <p>
            combined_text = table.get_text(strip=False)
            new_p = soup.new_tag("p")
            new_p.string = combined_text.strip()
            logger.info(f"    NRC drop-cap table vervangen door <p>: '{combined_text[:40]}'")
            if table.parent is not None:
                table.replace_with(new_p)

    # Patroon 3: CSS-gebaseerde drop-caps via ::first-letter pseudo — niet te verwijderen via HTML,
    # maar we kunnen de style-attributen die drop-caps veroorzaken forceren naar inline
    for elem in list(soup.find_all(True)):
        if elem.parent is None:
            continue
        style = elem.get("style", "")
        if re.search(r"display\s*:\s*table", style, re.IGNORECASE):
            elem["style"] = re.sub(
                r"display\s*:\s*table\b", "display: inline", style, flags=re.IGNORECASE
            )


def truncate_html_content(html: str, max_words: int = 700) -> str:
    """
    Beperk de lengte van HTML-content tot max_words zichtbare woorden.
    Verwijdert complete blok-elementen van achteren totdat we onder de limiet zitten.
    Voegt een afkap-noot toe onderaan het artikel.

    Args:
        html: De opgeschoonde HTML-content.
        max_words: Maximum aantal zichtbare woorden (default 700 ≈ ~3 A4-pagina's).

    Returns:
        HTML met maximaal max_words woorden, of ongewijzigd als al onder limiet.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    words = text.split()

    if len(words) <= max_words:
        return html

    logger.info(f"    Artikel ingekort: {len(words)} → max {max_words} woorden")

    # Verwijder blok-elementen van achteren totdat we onder de limiet zitten
    body = soup.find("body") or soup
    block_tags = ["p", "div", "section", "article", "blockquote", "ul", "ol",
                  "h1", "h2", "h3", "h4", "h5", "h6", "table", "figure"]

    # Verzamel alle top-level block-elementen (directe kinderen van body/root)
    blocks = [el for el in body.children if hasattr(el, "get_text")]

    while blocks:
        current_words = len(soup.get_text(separator=" ", strip=True).split())
        if current_words <= max_words:
            break
        last = blocks.pop()
        if last.parent is not None and hasattr(last, "decompose"):
            last.decompose()

    # Voeg afkap-noot toe
    note = soup.new_tag("p")
    note["style"] = (
        "color:#888; font-style:italic; margin-top:20px; "
        "border-top:1px solid #ddd; padding-top:8px; font-size:11px;"
    )
    note.string = "▸ Artikel ingekort. Lees het volledige artikel via de originele bron."
    (soup.find("body") or soup).append(note)

    return str(soup)


def _remove_nrc_promo_footer(soup: BeautifulSoup) -> None:
    """
    Verwijder de NRC promo-footer die begint bij "REACTIES" of
    "Bekijk al onze nieuwsbrieven". Verwijdert dit element EN alle
    volgende siblings tot het einde van de parent.
    """
    _NRC_FOOTER_TRIGGERS = [
        re.compile(r"^reacties$", re.IGNORECASE),
        re.compile(r"bekijk\s+al\s+onze\s+nieuwsbrieven", re.IGNORECASE),
        re.compile(r"broncode\s+van\s+de\s+week", re.IGNORECASE),
        re.compile(r"week\s+van\s+de\s+hoofdredactie", re.IGNORECASE),
    ]

    for elem in list(soup.find_all(True)):
        if elem.parent is None:
            continue
        try:
            text = elem.get_text(strip=True)
        except Exception:
            continue

        if not text:
            continue

        for pattern in _NRC_FOOTER_TRIGGERS:
            if pattern.search(text) and len(text) < 500:
                logger.info(f"    NRC promo-footer gevonden bij: '{text[:60]}' — rest verwijderd")
                # Verwijder dit element EN alle volgende siblings
                siblings_to_remove = list(elem.find_next_siblings())
                for sib in siblings_to_remove:
                    if sib.parent is not None:
                        sib.decompose()
                if elem.parent is not None:
                    elem.decompose()
                return  # Klaar, maar één keer doen
