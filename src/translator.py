"""
translator.py - OpenAI vertaalmodule.

Detecteert de taal van HTML-content en vertaalt Engelse tekst naar het Nederlands
met behoud van HTML-structuur en originele schrijfstijl/toon.
"""

import logging
import re

from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger(__name__)

# Simpele heuristiek voor taaldetectie op basis van veelvoorkomende woorden
_DUTCH_MARKERS = {
    "de", "het", "een", "van", "in", "is", "dat", "op", "voor", "met",
    "zijn", "aan", "niet", "ook", "maar", "door", "nog", "dan", "wel",
    "naar", "uit", "bij", "om", "tot", "over", "deze", "wordt", "meer",
    "heeft", "worden", "kan", "dit", "alle", "hun", "veel", "waar",
}

_ENGLISH_MARKERS = {
    "the", "a", "an", "is", "in", "it", "of", "to", "and", "for",
    "that", "with", "on", "are", "was", "this", "have", "from", "or",
    "be", "by", "not", "but", "what", "all", "were", "we", "when",
    "your", "can", "has", "more", "will", "been", "would", "who",
}


def detect_language(html_content: str) -> str:
    """
    Detecteer of de HTML-content overwegend Engels of Nederlands is.

    Returns:
        'nl' voor Nederlands, 'en' voor Engels
    """
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator=" ", strip=True).lower()
    words = re.findall(r"\b[a-z]+\b", text)

    if not words:
        return "nl"

    # Tel markerwoorden
    sample = words[:500]  # Eerste 500 woorden is voldoende
    nl_count = sum(1 for w in sample if w in _DUTCH_MARKERS)
    en_count = sum(1 for w in sample if w in _ENGLISH_MARKERS)

    ratio_nl = nl_count / len(sample)
    ratio_en = en_count / len(sample)

    if ratio_nl > ratio_en:
        return "nl"
    elif ratio_en > ratio_nl:
        return "en"
    else:
        return "nl"  # Bij gelijkspel: neem aan dat het Nederlands is


def translate_html(html_content: str, openai_api_key: str) -> str:
    """
    Vertaal Engelse HTML-content naar het Nederlands met behoud van
    HTML-structuur en originele toon.

    Args:
        html_content: De HTML-string om te vertalen.
        openai_api_key: OpenAI API key.

    Returns:
        Vertaalde HTML-string.
    """
    client = OpenAI(api_key=openai_api_key)

    # Splits de HTML in behapbare stukken als het te groot is
    # GPT-4o heeft een groot context window, maar we splitsen op ~12000 tekens
    max_chunk_size = 12000

    if len(html_content) <= max_chunk_size:
        return _translate_chunk(client, html_content)

    # Splits op top-level HTML-elementen
    chunks = _split_html(html_content, max_chunk_size)
    translated_chunks = []
    for i, chunk in enumerate(chunks):
        logger.info(f"  Vertalen deel {i + 1}/{len(chunks)}...")
        translated = _translate_chunk(client, chunk)
        translated_chunks.append(translated)

    return "".join(translated_chunks)


def _translate_chunk(client: OpenAI, html_chunk: str) -> str:
    """Vertaal een enkel stuk HTML via de OpenAI API."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Je bent een professionele vertaler. Vertaal de volgende "
                        "HTML-content van Engels naar Nederlands. "
                        "BELANGRIJK:\n"
                        "- Behoud ALLE HTML-tags, attributen en structuur exact.\n"
                        "- Vertaal ALLEEN de zichtbare tekst.\n"
                        "- Behoud de originele schrijfstijl en toon van de auteur.\n"
                        "- Vertaal NIET: URLs, e-mailadressen, merknamen, productnamen.\n"
                        "- Geef ALLEEN de vertaalde HTML terug, geen uitleg."
                    ),
                },
                {
                    "role": "user",
                    "content": html_chunk,
                },
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI vertaalfout: {e}")
        return html_chunk  # Bij fout: origineel teruggeven


def _split_html(html_content: str, max_size: int) -> list[str]:
    """
    Splits HTML in stukken van maximaal max_size tekens.
    Probeert te splitsen op paragraaf- of div-grenzen.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.find("body")
    if not body:
        body = soup

    chunks = []
    current_chunk = ""

    for element in body.children:
        element_str = str(element)
        if len(current_chunk) + len(element_str) > max_size and current_chunk:
            chunks.append(current_chunk)
            current_chunk = element_str
        else:
            current_chunk += element_str

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [html_content]


def generate_toc_entry(subject: str, sender: str, openai_api_key: str) -> dict:
    """
    Genereer een korte titel en beschrijving voor de inhoudsopgave.

    Returns:
        Dict met 'short_title' (max 10 woorden) en 'description' (korte zin).
    """
    client = OpenAI(api_key=openai_api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Je maakt inhoudsopgave-items voor een dagelijkse krant. "
                        "Geef op basis van het onderwerp en de afzender twee dingen:\n"
                        "1. TITEL: Een korte, pakkende titel van maximaal 8 woorden (Nederlands)\n"
                        "2. BESCHRIJVING: Een beschrijving van maximaal 12 woorden (Nederlands)\n\n"
                        "Formaat (exact zo, zonder aanhalingstekens):\n"
                        "TITEL: ...\n"
                        "BESCHRIJVING: ..."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Onderwerp: {subject}\nAfzender: {sender}",
                },
            ],
            temperature=0.5,
            max_tokens=80,
        )
        text = response.choices[0].message.content.strip()

        # Parse het resultaat
        short_title = subject  # fallback
        description = ""
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("TITEL:"):
                short_title = line.split(":", 1)[1].strip()
            elif line.upper().startswith("BESCHRIJVING:"):
                description = line.split(":", 1)[1].strip()

        return {"short_title": short_title, "description": description}

    except Exception as e:
        logger.error(f"Fout bij genereren TOC entry: {e}")
        return {"short_title": subject, "description": ""}
