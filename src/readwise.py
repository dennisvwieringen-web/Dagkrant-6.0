"""Opslaan van de Dagkrant als highlightbaar HTML-document in Readwise Reader."""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_SAVE_URL = "https://readwise.io/api/v3/save/"


def save_html_document(
    html_content: str,
    access_token: str,
    title: str,
    source_key: str,
    published_date: str,
) -> str | None:
    """Sla één editie via de officiële Reader API op en retourneer de Reader-URL."""
    payload = {
        "url": f"https://dagkrant.local/editie/{source_key}",
        "html": html_content,
        "title": title,
        "author": "De Dagkrant",
        "language": "nl",
        "published_date": published_date,
        "category": "article",
        "location": "new",
        "saved_using": "De Dagkrant",
        "tags": ["dagkrant"],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        _SAVE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Token {access_token}",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "User-Agent": "De-Dagkrant/1.0",
        },
    )

    try:
        with urlopen(request, timeout=90) as response:
            response_body = response.read().decode("utf-8")
            result = json.loads(response_body) if response_body else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Readwise gaf HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Readwise is niet bereikbaar: {exc}") from exc

    reader_url = result.get("url")
    logger.info("Readwise-document opgeslagen%s.", f": {reader_url}" if reader_url else "")
    return reader_url
