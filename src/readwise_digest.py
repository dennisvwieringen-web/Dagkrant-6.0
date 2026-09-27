"""
readwise_digest.py - Bundel alle Readwise-mails van een editie tot één rubriek.

Readwise stuurt dagelijks een mail (plus Themed Connections en Sunday
Favorites). Als losse artikelen vulden die de weekkrant met vijf bijna
identieke stukken vol knoppen ("Favorite / Discard / Tag or Share"),
welkomstblokken en Kindle-locaties. Hier halen we alleen de citaten eruit
(titel, auteur, tekst), ontdubbelen ze en groeperen ze per boek tot één
schone rubriek.

De mails hebben een vaste structuur: elk citaat staat in een
`<table id="highlightNNN">` met de boektitel in `<b>`, de auteur in
`<small>` ("by X") en de tekst in de `<p>`'s ná de `<hr>`.
"""

import html
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UNKNOWN_AUTHORS = {"", "onbekend", "unknown"}


def is_readwise(nl: dict) -> bool:
    """True voor een Readwise-mail (dagelijks, themed of Sunday Favorites)."""
    return "readwise.io" in nl.get("sender", "").lower()


def _normalize(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def _extract_highlights(raw_html: str) -> list[dict]:
    soup = BeautifulSoup(raw_html, "html.parser")
    highlights = []
    for block in soup.select('[id^="highlight"]'):
        title_tag = block.find("b")
        if not title_tag:
            continue
        title = title_tag.get_text(" ", strip=True)
        author_tag = title_tag.find_next("small")
        author = author_tag.get_text(" ", strip=True) if author_tag else ""
        author = re.sub(r"^by\s+", "", author, flags=re.IGNORECASE).strip()

        # Readwise zet de titel soms als "Het Alles by Eggers, Dave" met auteur
        # "Onbekend" — haal dan de echte auteur uit de titel.
        if author.lower() in _UNKNOWN_AUTHORS:
            m = re.match(r"^(.*?)\s+by\s+(.+)$", title, re.IGNORECASE)
            if m:
                title, author = m.group(1).strip(), m.group(2).strip()
            else:
                author = ""
        # Bestandsnaam-achtige titels ("Cal_Newport-Slow_productivity") leesbaar maken.
        title = title.replace("_", " ")

        hr = block.find("hr")
        if not hr:
            continue
        paragraphs = []
        for p in hr.find_all_next("p"):
            if not block in p.parents:
                break
            if p.find_parent("table", class_="highlight-action-row"):
                break
            for small in p.find_all("small"):  # "(Location 529)"
                small.decompose()
            text = p.get_text(" ", strip=True)
            if text:
                paragraphs.append(text)
        if not paragraphs:
            continue
        highlights.append({"title": title, "author": author, "paragraphs": paragraphs})
    return highlights


def build_readwise_bundle(items: list[dict]) -> dict | None:
    """
    Maak van een lijst Readwise-mails één nieuwsbrief-dict met alle unieke
    citaten, gegroepeerd per boek. Geeft None als er geen citaten gevonden
    zijn (dan blijven de mails gewoon losse artikelen).
    """
    books: dict[str, dict] = {}
    seen_texts: set[str] = set()
    total = 0
    # Oudste eerst: zo staan de citaten per boek in de volgorde van de week.
    for nl in sorted(items, key=lambda x: x.get("date", "")):
        for h in _extract_highlights(nl.get("html_content", "")):
            key = _normalize(" ".join(h["paragraphs"]))[:300]
            if key in seen_texts:
                continue  # Sunday Favorites herhaalt citaten van eerder die week
            seen_texts.add(key)
            book = books.setdefault(
                _normalize(h["title"]),
                {"title": h["title"], "author": h["author"], "highlights": []},
            )
            book["highlights"].append(h["paragraphs"])
            total += 1

    if not total:
        return None

    parts = []
    for book in books.values():
        source = html.escape(book["title"])
        if book["author"]:
            source += f' <span class="rw-author">— {html.escape(book["author"])}</span>'
        parts.append(f'<h3 class="rw-book">{source}</h3>')
        for paragraphs in book["highlights"]:
            body = "".join(f"<p>{html.escape(t)}</p>" for t in paragraphs)
            parts.append(f'<blockquote class="rw-highlight">{body}</blockquote>')

    logger.info(
        f"  📚 Readwise-rubriek: {len(items)} mail(s) → {total} unieke citaten "
        f"uit {len(books)} boek(en)."
    )
    newest = max(items, key=lambda x: x.get("date", ""))
    return {
        "subject": "Readwise — citaten van de week",
        "sender": "Readwise",
        "date": newest.get("date", ""),
        "label": "Readwise",
        "message_id": "",
        "html_content": "\n".join(parts),
        "plain_content": None,
        "prebuilt": True,  # zelf gebouwde, schone HTML: niet door clean_html()
    }
