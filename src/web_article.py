"""
web_article.py - Haal een webartikel op en extraheer de inhoud.

Gebruikt Playwright (al aanwezig voor PDF-rendering) om JS-zware sites
zoals De Correspondent te renderen, en BeautifulSoup om de artikeltekst
te isoleren.
"""

import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# Selectoren voor de hoofartikelcontainer, op volgorde van voorkeur
_ARTICLE_SELECTORS = [
    "article",
    "main",
    '[role="main"]',
    ".article-body",
    ".article__body",
    ".post-content",
    ".entry-content",
    ".content-body",
    "#article-body",
]


def _extract_article_content(html: str) -> str:
    """
    Extraheer de artikeltekst uit een volledige webpagina-HTML.

    Verwijdert navigatie, headers, footers en scripts, en probeert
    via veelgebruikte selectors de hoofdtekst te isoleren.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["nav", "header", "footer", "aside", "script", "style", "noscript"]):
        tag.decompose()

    for selector in _ARTICLE_SELECTORS:
        container = soup.select_one(selector)
        if container and len(container.get_text(strip=True)) > 200:
            return str(container)

    body = soup.find("body")
    return str(body) if body else html


def fetch_article(url: str) -> dict | None:
    """
    Haal een webartikel op via Playwright en retourneer een nieuwsbrief-dict.

    Args:
        url: De volledige URL van het artikel.

    Returns:
        Dict met subject, sender, date, html_content — of None bij een fout.
    """
    domain = urlparse(url).netloc.replace("www.", "")
    logger.info(f"Webartikel ophalen: {url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                # Voorkom dat sites de headless browser weigeren
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            title = page.title() or domain
            html = page.content()
            browser.close()

        article_html = _extract_article_content(html)

        return {
            "subject": title,
            "sender": domain,
            "date": datetime.now(timezone.utc).isoformat(),
            "html_content": article_html,
            "plain_content": None,
        }

    except Exception as e:
        logger.error(f"Fout bij ophalen webartikel '{url}': {e}")
        return None


# --- Teaser-mails aanvullen tot het volledige artikel ---
#
# Sommige blogs (o.a. "X, Y of Einstein?" via WordPress) mailen alleen de eerste
# alinea's met een "Lees verder"-link. In de geprinte krant hield zo'n artikel
# dan halverwege op. We halen in dat geval het volledige bericht op.

_READ_MORE_RE = re.compile(
    r"^\s*(lees\s+verder|verder\s+lezen|lees\s+meer|read\s+more|continue\s+reading|"
    r"keep\s+reading)\b",
    re.IGNORECASE,
)
# Teasers zijn kort (inclusief mailfooter ruim onder deze grens).
_MAX_TEASER_CHARS = 3000


def _visible_len(html: str) -> int:
    return len(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def _unwrap_redirect(href: str) -> str:
    """WordPress-maillinks lopen via een tracker met ?redirect_to=<echte url>."""
    qs = parse_qs(urlparse(href).query)
    return qs.get("redirect_to", [href])[0]


def _find_teaser_url(nl: dict) -> str | None:
    """Geeft de URL van het volledige artikel als de mail een teaser is, anders None."""
    soup = BeautifulSoup(nl.get("html_content") or "", "html.parser")
    read_more = [a for a in soup.find_all("a", href=True) if _READ_MORE_RE.match(a.get_text(" ", strip=True))]
    if not read_more:
        return None

    # WordPress-notificaties noemen de bericht-URL letterlijk in de platte tekst.
    m = re.search(r"^URL\s*:\s*(https?://\S+)", nl.get("plain_content") or "", re.MULTILINE)
    if m:
        return m.group(1)
    url = _unwrap_redirect(read_more[0]["href"])
    return url if url.startswith("http") else None


def _fetch_wordpress_post(url: str) -> str | None:
    """
    Haal de inhoud van een WordPress-bericht op via de openbare REST-API.
    Veel WordPress-blogs tonen een botcontrole ("Checking your browser") aan
    een headless browser; de API heeft daar geen last van.
    """
    parsed = urlparse(url)
    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return None
    api = (
        f"https://public-api.wordpress.com/rest/v1.1/sites/{quote(parsed.netloc)}"
        f"/posts/slug:{quote(slug)}?fields=content"
    )
    req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp).get("content") or None


def expand_teaser(nl: dict) -> bool:
    """
    Vervang de inhoud van een teaser-mail door het volledige artikel.
    Onderwerp, afzender en message-id blijven die van de mail. Geeft True als
    de inhoud is vervangen; bij twijfel of fout blijft de mail ongewijzigd.
    """
    # Een lange mail met een "Lees verder"-link (bv. NRC) is geen teaser: die
    # link hoort dan bij een los onderdeel, niet bij het hele stuk.
    teaser_len = _visible_len(nl.get("html_content") or "")
    if teaser_len >= _MAX_TEASER_CHARS:
        return False
    url = _find_teaser_url(nl)
    if not url:
        return False

    content = None
    try:
        content = _fetch_wordpress_post(url)
    except Exception as e:
        logger.debug(f"WordPress-API gaf geen bericht voor {url}: {e}")
    if not content:
        article = fetch_article(url)
        content = article["html_content"] if article else None
    if not content:
        return False

    # Alleen vervangen als het opgehaalde artikel echt langer is dan de teaser —
    # anders is het waarschijnlijk een botcontrole of een verkeerde pagina.
    full_len = _visible_len(content)
    if full_len < teaser_len * 1.2 or "checking your browser" in content.lower()[:500]:
        logger.info(f"    Volledig artikel niet bruikbaar ({full_len} vs {teaser_len} tekens) — teaser blijft.")
        return False

    # Bron bovenaan: onderaan belandde de regel soms in z'n eentje op een nieuwe pagina.
    nl["html_content"] = f'<p class="source-link">Volledig artikel van {url}</p>{content}'
    logger.info(f"    📰 Teaser aangevuld met volledig artikel ({teaser_len} → {full_len} tekens).")
    return True
