"""
substack_feed.py - Haal Substack-posts op via RSS i.p.v. via de mailbox.

Waarom: Substack stopte begin augustus 2026 ongemerkt met mailen naar Dennis
(vermoedelijk omdat de mails nooit geopend worden — hij leest ze op papier,
via de weekkrant). De posts staan wél gewoon in de feed van elke publicatie
(https://<naam>.substack.com/feed, met de volledige HTML in content:encoded),
dus de krant hangt zo niet meer af van Substack's mailbeslissingen.

De lijst met publicaties staat in substack_feeds.json (repo-root): naam (zoals
het Gmail-label, wordt de afzender in de krant) + feed-URL. Betaalde posts
geven in de feed alleen een voorvertoning; komt dezelfde post ook per mail
binnen, dan wint de mail (zie main.py).
"""

import html
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import feedparser

logger = logging.getLogger(__name__)

_FEEDS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "substack_feeds.json")
# Substack geeft een kale Python-UA soms een 403; een gewone UA niet.
_USER_AGENT = "Mozilla/5.0 (compatible; Weekkrant RSS reader)"


def load_feeds(path: str = _FEEDS_FILE) -> list[dict]:
    """Lees substack_feeds.json; lege lijst als het bestand ontbreekt of kapot is."""
    try:
        with open(path, encoding="utf-8") as f:
            feeds = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"substack_feeds.json onleesbaar ({e}) — geen RSS-posts.")
        return []
    return [f for f in feeds if f.get("feed") and f.get("enabled", True)]


def _canonical_url(url: str) -> str:
    """Link zonder query/fragment (utm-parameters): stabiele sleutel voor de administratie."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))


def _entry_date(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _entry_html(entry) -> str:
    """Volledige HTML uit content:encoded, anders de samenvatting."""
    for content in entry.get("content") or []:
        if content.get("value"):
            return content["value"]
    return entry.get("summary", "")


def fetch_feed_articles(since: datetime, feeds: list[dict] | None = None) -> list[dict]:
    """
    Alle posts sinds `since` uit de geconfigureerde feeds, als nieuwsbrief-dicts
    (zelfde vorm als fetcher.py). Een feed die faalt wordt overgeslagen.
    """
    feeds = load_feeds() if feeds is None else feeds
    articles = []
    for feed in feeds:
        name = feed.get("name") or feed["feed"]
        try:
            parsed = feedparser.parse(feed["feed"], agent=_USER_AGENT)
        except Exception as e:
            logger.warning(f"  RSS {name}: ophalen mislukt ({e})")
            continue
        status = parsed.get("status")
        if status and status >= 400:
            logger.warning(f"  RSS {name}: HTTP {status}")
            continue
        if parsed.bozo and not parsed.entries:
            logger.warning(f"  RSS {name}: onleesbare feed ({parsed.get('bozo_exception')})")
            continue

        count = 0
        for entry in parsed.entries:
            published = _entry_date(entry)
            link = entry.get("link", "")
            body = _entry_html(entry)
            if not published or published < since or not link or not body:
                continue
            domain = urlparse(link).netloc.removeprefix("www.")
            source = (
                f'<p class="source-link">Via de feed van '
                f'<a href="{html.escape(link)}">{html.escape(domain)}</a></p>'
            )
            articles.append({
                "subject": entry.get("title", "(zonder titel)"),
                "sender": name,
                "date": published.isoformat(),
                "label": name,
                "message_id": _canonical_url(link),
                "html_content": source + body,
                "plain_content": None,
                "from_feed": True,
            })
            count += 1
        if count:
            logger.info(f"  RSS {name}: {count} post(s)")
    return articles
