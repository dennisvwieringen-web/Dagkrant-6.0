"""
web_article.py - Haal een webartikel op en extraheer de inhoud.

Gebruikt Playwright (al aanwezig voor PDF-rendering) om JS-zware sites
zoals De Correspondent te renderen, en BeautifulSoup om de artikeltekst
te isoleren.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

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
