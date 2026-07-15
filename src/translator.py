"""
translator.py - OpenAI vertaalmodule.

Detecteert de taal van HTML-content en vertaalt Engelse tekst naar het Nederlands
met behoud van HTML-structuur en originele schrijfstijl/toon.
"""

import logging
import re
import time

from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIUnavailableError(RuntimeError):
    """
    OpenAI is structureel onbereikbaar: het krediet is op, de API-key is ongeldig
    of het account is geblokkeerd. Retryen heeft dan geen enkele zin — elke
    volgende aanroep faalt identiek.

    Dit onderscheid bestaat omdat de krant maandenlang stil in het Engels kon
    verschijnen: `insufficient_quota` werd afgevangen als "gewone" fout, drie keer
    opnieuw geprobeerd en daarna viel de vertaling terug op het origineel — zonder
    dat iemand het merkte. Deze fout wordt bewust NIET binnen de module afgevangen,
    zodat main.py de hele editie kan markeren als "onvertaald" en luid alarm slaat.
    """


# Foutcodes waarbij opnieuw proberen kansloos is (i.t.t. een tijdelijke rate limit,
# een timeout of een 5xx aan de kant van OpenAI).
_PERMANENT_ERROR_MARKERS = (
    "insufficient_quota",
    "invalid_api_key",
    "account_deactivated",
    "billing_hard_limit_reached",
)


def _is_permanent_error(exc: Exception) -> bool:
    """
    True als deze OpenAI-fout niet vanzelf overgaat (krediet op, key ongeldig).

    Let op: een 429 is dubbelzinnig. 'rate_limit_exceeded' is tijdelijk (even
    wachten helpt), 'insufficient_quota' is permanent (er moet geld bij). Daarom
    kijken we naar de foutcode in de body, niet alleen naar de HTTP-status.
    """
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_ERROR_MARKERS)

# Heuristiek voor taaldetectie op basis van veelvoorkomende woorden.
# Let op: "is" en "in" zijn bewust NIET in de Engels-markers opgenomen —
# ze komen ook als gewone Nederlandse woorden voor en vervuilen anders de score.
_DUTCH_MARKERS = {
    "de", "het", "een", "van", "in", "is", "dat", "op", "voor", "met",
    "zijn", "aan", "niet", "ook", "maar", "door", "nog", "dan", "wel",
    "naar", "uit", "bij", "om", "tot", "over", "deze", "wordt", "meer",
    "heeft", "worden", "kan", "dit", "alle", "hun", "veel", "waar",
    # Extra sterke Nederlandse signaalwoorden
    "als", "ze", "hij", "wij", "zij", "wat", "geen", "zo", "al",
    "ons", "per", "werd", "die",
}

_ENGLISH_MARKERS = {
    # "is" en "in" weggelaten — ook gangbaar Nederlands, dus geen bruikbaar signaal
    "the", "a", "an", "of", "to", "and", "for",
    "that", "with", "on", "are", "was", "this", "have", "from", "or",
    "be", "by", "not", "but", "what", "all", "were", "we", "when",
    "your", "can", "has", "more", "will", "been", "would", "who",
    # Extra sterke Engelse signaalwoorden
    "their", "they", "which", "its", "our", "you", "at", "as",
    "if", "up", "about", "out", "just", "do",
}


def detect_language(html_content: str) -> str:
    """
    Detecteer of de HTML-content overwegend Engels of Nederlands is.

    Samples woorden verdeeld over begin, midden en einde van de tekst
    zodat een Nederlandstalige doorstuurheader de detectie niet verstoort.

    Returns:
        'nl' voor Nederlands, 'en' voor Engels
    """
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator=" ", strip=True).lower()
    words = re.findall(r"\b[a-z]+\b", text)

    if not words:
        return "nl"

    # Verdeeld samplen: begin + midden + einde voor betere dekking
    n = len(words)
    if n > 900:
        sample = words[:400] + words[n // 2 - 150 : n // 2 + 150] + words[-300:]
    else:
        sample = words

    nl_count = sum(1 for w in sample if w in _DUTCH_MARKERS)
    en_count = sum(1 for w in sample if w in _ENGLISH_MARKERS)

    ratio_nl = nl_count / len(sample)
    ratio_en = en_count / len(sample)

    logger.debug(
        f"Taaldetectie: NL={nl_count} ({ratio_nl:.1%}), EN={en_count} ({ratio_en:.1%})"
    )

    # Vertaal tenzij Nederlands DUIDELIJK domineert (factor 1.3).
    # Bij twijfel liever onnodig vertalen dan Engels in de Dagkrant laten staan.
    if ratio_nl >= ratio_en * 1.3:
        return "nl"
    else:
        return "en"


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

    # Splits de HTML in behapbare stukken als het te groot is.
    # Bewust ruim onder de output-tokenlimiet van gpt-4o-mini: bij ~8000 tekens
    # invoer past de vertaling comfortabel binnen max_tokens, zodat het model niet
    # halverwege afkapt (finish_reason="length") en er geen Engelse staart blijft staan.
    max_chunk_size = 8000

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


_TRANSLATE_SYSTEM_PROMPT = (
    "Je bent een professionele vertaler die Engels naar idiomatisch "
    "Nederlands vertaalt voor een dagelijkse nieuwskrant.\n"
    "STRUCTUUR — verplicht:\n"
    "- Behoud ALLE HTML-tags, attributen en structuur exact.\n"
    "- Vertaal ALLEEN de zichtbare tekst, nooit CSS of URLs.\n"
    "- Geef ALLEEN de vertaalde HTML terug — geen uitleg, geen code-fences.\n"
    "- Herhaal NOOIT de Engelse brontekst; geef uitsluitend de Nederlandse vertaling.\n\n"
    "TAAL — verplicht:\n"
    "- Vertaal ELKE Engelse zin; laat geen enkele Engelse zin onvertaald staan.\n"
    "- Schrijf vloeiend, idiomatisch Nederlands. Geen letterlijke vertalingen.\n"
    "- Vertaal NIET: URLs, e-mailadressen, merknamen, eigennamen, productnamen.\n"
    "- Gangbaar tech-jargon dat in het Nederlands gebruikelijk is blijft Engels: "
    "AI, startup, senior, product manager, podcast, pitch, sprint, feedback.\n"
    "- Vertaal anglicismen wél naar goed Nederlands:\n"
    "  'settle' → 'genoegen nemen met' (niet 'settelen')\n"
    "  'north star' → 'leidster' of 'kompas' (niet 'noordster')\n"
    "  'playbook' → 'aanpak' of 'werkwijze' (niet 'speelboek')\n"
    "  'insane' (informeel) → 'enorm' of 'extreem' (niet 'idioot')\n"
    "  'later-stage' → 'groeiende' of 'volwassen' (niet 'later-stage')\n"
    "  'tenure' → 'diensttijd' of 'periode' (niet 'tenure')\n"
    "  'leverage' (werkwoord) → 'benutten' of 'inzetten' (niet 'leveragen')\n"
    "- Behoud de toon van de auteur: informele stukken blijven informeel, "
    "analytische stukken blijven analytisch."
)


def _translate_chunk(client: OpenAI, html_chunk: str, max_attempts: int = 3) -> str:
    """
    Vertaal een enkel stuk HTML via de OpenAI API.

    Robuust tegen de twee manieren waarop een chunk stil Engels kon blijven:
    (1) een API-fout of lege/None-respons die het origineel teruggaf, en
    (2) een respons die (deels) onvertaald Engels bleef. Bij beide wordt opnieuw
    geprobeerd (tot max_attempts). Pas als álle pogingen falen valt de functie
    terug op het origineel — beter imperfect dan een crash, maar dat is nu de
    uitzondering, niet de stille regel.

    Uitzondering: bij een permanente fout (krediet op, key ongeldig) wordt géén
    poging herhaald en gaat er een OpenAIUnavailableError omhoog. Retryen zou daar
    alleen tijd kosten, en het stilzwijgend terugvallen op het Engelse origineel is
    precies hoe de krant onopgemerkt onvertaald kon blijven.
    """
    # Kan er überhaupt iets te vertalen zijn? Zo niet, dan is een 'nog Engels'-
    # verificatie zinloos (korte code/URL-fragmenten geven vals alarm).
    verify_language = _has_translatable_text(html_chunk)
    last_result = html_chunk

    for attempt in range(1, max_attempts + 1):
        try:
            logger.debug(f"  Vertalen chunk van {len(html_chunk)} tekens (poging {attempt})...")
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": _TRANSLATE_SYSTEM_PROMPT},
                    {"role": "user", "content": html_chunk},
                ],
                temperature=0.3,
                max_tokens=16000,
            )
            choice = response.choices[0]
            content = choice.message.content

            # None/lege content (bv. bij content-filter of afgekapt op token 0):
            # behandel als fout zodat de retry aanslaat i.p.v. .strip() te laten crashen.
            if not content or not content.strip():
                raise ValueError("lege of ontbrekende respons van het model")
            translated = content.strip()

            # Truncatie: het model kapte af op de tokenlimiet. De staart is dan
            # onvertaald/afgebroken. Kleinere chunk bij de volgende poging helpt niet
            # (zelfde chunk), dus log het duidelijk maar accepteer wat er is.
            if getattr(choice, "finish_reason", None) == "length":
                logger.warning(
                    f"  ⚠️ Vertaling afgekapt op tokenlimiet ({len(html_chunk)} tekens chunk) — "
                    f"resultaat mogelijk onvolledig."
                )

            # Verifieer dat er daadwerkelijk vertaald is. Blijft het resultaat Engels,
            # dan negeerde het model de opdracht — opnieuw proberen.
            if verify_language and detect_language(translated) == "en":
                logger.warning(
                    f"  ⚠️ Chunk kwam nog als Engels terug (poging {attempt}/{max_attempts}) — opnieuw proberen."
                )
                last_result = translated  # bewaar de beste tot nu toe
                continue

            return translated

        except OpenAIUnavailableError:
            raise

        except Exception as e:
            if _is_permanent_error(e):
                raise OpenAIUnavailableError(str(e)) from e

            logger.warning(
                f"  ⚠️ Vertaalpoging {attempt}/{max_attempts} mislukt "
                f"({len(html_chunk)} tekens chunk): {e}"
            )
            if attempt < max_attempts:
                # Exponentiële backoff: een tijdelijke rate limit of 5xx is meestal
                # binnen enkele seconden over. Direct opnieuw vuren maakt het erger.
                time.sleep(2**attempt)

    logger.error(
        f"OpenAI vertaling definitief niet gelukt na {max_attempts} pogingen "
        f"({len(html_chunk)} tekens chunk) — beste beschikbare resultaat behouden."
    )
    return last_result


def _has_translatable_text(html_chunk: str, min_words: int = 12) -> bool:
    """
    True als de chunk genoeg gewone tekstwoorden bevat om een taalverificatie
    zinvol te maken. Voorkomt vals 'nog Engels'-alarm op stukken die vooral uit
    URLs, code of losse merknamen bestaan.
    """
    text = BeautifulSoup(html_chunk, "html.parser").get_text(separator=" ", strip=True)
    words = re.findall(r"\b[a-zA-Z]{2,}\b", text)
    return len(words) >= min_words


def _split_html(html_content: str, max_size: int) -> list[str]:
    """
    Splits HTML in stukken van maximaal max_size tekens.

    Werkt recursief: als een top-level element zelf groter is dan max_size
    (typisch bij Substack-stijl HTML met één grote geneste <table>),
    wordt er dieper in de boom gezocht naar splitspunten.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.find("body") or soup

    chunks: list[str] = []
    current_chunk: str = ""

    def _collect(elements) -> None:
        nonlocal current_chunk
        for element in elements:
            element_str = str(element)

            if len(element_str) > max_size and hasattr(element, "children"):
                # Element te groot — recursief afdalen in de kinderen
                _collect(list(element.children))

            elif len(current_chunk) + len(element_str) > max_size and current_chunk:
                # Huidige chunk vol — sla op en begin nieuw
                chunks.append(current_chunk)
                current_chunk = element_str

            else:
                current_chunk += element_str

    _collect(list(body.children))

    if current_chunk:
        chunks.append(current_chunk)

    if not chunks:
        return [html_content]

    logger.debug(f"  HTML gesplitst in {len(chunks)} chunks (max_size={max_size})")
    return chunks


def generate_toc_entry(
    subject: str, sender: str, openai_api_key: str, content_snippet: str = ""
) -> dict:
    """
    Genereer een korte titel en beschrijving voor de inhoudsopgave.
    Gebruikt een content-snippet voor betere, inhoudelijkere beschrijvingen.

    Returns:
        Dict met 'short_title' (max 8 woorden) en 'description' (feitelijke samenvatting).
    """
    client = OpenAI(api_key=openai_api_key)

    # Bouw de user-content op met optionele snippet
    user_content = f"Onderwerp: {subject}\nAfzender: {sender}"
    if content_snippet:
        # Gebruik max 400 tekens van de snippet voor context
        user_content += f"\nBegin van artikel: {content_snippet[:400]}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Je maakt bondige inhoudsopgave-items voor een dagelijkse krant.\n"
                        "Schrijf INFORMATIEVE, FEITELIJKE tekst op basis van de daadwerkelijke inhoud.\n"
                        "VERBODEN: vage clickbait zoals 'Ontdek...', 'Verken...', 'Leer meer over...',\n"
                        "'Bekijk...', 'Kom erachter...'. Beschrijf de KERNBOODSCHAP concreet.\n\n"
                        "Geef twee dingen:\n"
                        "1. TITEL: Een beknopte, informatieve titel van max 8 woorden (Nederlands)\n"
                        "2. BESCHRIJVING: Een feitelijke samenvatting van max 12 woorden (Nederlands)\n\n"
                        "Formaat (exact zo, zonder aanhalingstekens of extra tekst):\n"
                        "TITEL: ...\n"
                        "BESCHRIJVING: ..."
                    ),
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            temperature=0.3,
            max_tokens=100,
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
        if _is_permanent_error(e):
            raise OpenAIUnavailableError(str(e)) from e
        logger.error(f"Fout bij genereren TOC entry: {e}")
        return {"short_title": subject, "description": ""}
