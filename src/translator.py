"""
translator.py - OpenAI vertaalmodule.

Detecteert de taal van HTML-content en vertaalt Engelse tekst naar het Nederlands
met behoud van HTML-structuur en originele schrijfstijl/toon.
"""

import logging
import os
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


# Vertaalmodel. gpt-4o-mini gaf op papier zichtbare fouten ("Intern intern",
# letterlijk "Programmatic commentaar"); gpt-4.1 vertaalt merkbaar beter en kost
# bij 2-3 vertaalde stukken per week enkele centen. Overschrijfbaar via env.
# Bestaat het model (niet meer) op dit account, dan valt de run terug op
# gpt-4o-mini i.p.v. alles onvertaald te laten.
_FALLBACK_MODEL = "gpt-4o-mini"
_translation_model = os.getenv("TRANSLATION_MODEL", "gpt-4.1")


def _handle_model_error(exc: Exception) -> bool:
    """Schakel over op het fallback-model bij 'model bestaat niet'. True = opnieuw proberen."""
    global _translation_model
    text = str(exc).lower()
    if _translation_model != _FALLBACK_MODEL and (
        "model_not_found" in text or "does not exist" in text
    ):
        logger.warning(f"  ⚠️ Vertaalmodel '{_translation_model}' niet beschikbaar — val terug op {_FALLBACK_MODEL}.")
        _translation_model = _FALLBACK_MODEL
        return True
    return False


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
_DUTCH_MARKERS = {
    # "is" en "in" bewust weggelaten — net als bij de Engelse markers hieronder:
    # ze komen in beide talen evengoed voor en gaven de Nederlandse score een
    # oneerlijke voorsprong (elke gewone Engelse tekst bevat volop "is"/"in",
    # maar kreeg daar tot voor kort geen Engels tegenwicht voor terug).
    "de", "het", "een", "van", "dat", "op", "voor", "met",
    "zijn", "aan", "niet", "ook", "maar", "door", "nog", "dan", "wel",
    "naar", "uit", "bij", "om", "tot", "over", "deze", "wordt", "meer",
    "heeft", "worden", "kan", "dit", "alle", "hun", "veel", "waar",
    # Extra sterke Nederlandse signaalwoorden
    "als", "ze", "hij", "wij", "zij", "wat", "geen", "zo", "al",
    "ons", "per", "werd", "die",
}

_ENGLISH_MARKERS = {
    # "is" en "in" weggelaten — ook gangbaar Nederlands, dus geen bruikbaar signaal.
    # Om dezelfde reden ook "of", "was" en "we": in een Nederlandse alinea telden
    # die als Engels, waardoor bv. AI Report's "…Sponsor onze nieuwsbrief of
    # podcast" als Engels blok werd 'vertaald' (weekkrant 2 okt 2026).
    "the", "a", "an", "to", "and", "for",
    "that", "with", "on", "are", "this", "have", "from", "or",
    "be", "by", "not", "but", "what", "all", "were", "when",
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


def _text_len(html_fragment: str) -> int:
    return len(BeautifulSoup(html_fragment, "html.parser").get_text(" ", strip=True))


def translate_html(html_content: str, openai_api_key: str) -> tuple[str, float]:
    """
    Vertaal de Engelse delen van HTML-content naar het Nederlands, met behoud
    van HTML-structuur en originele toon.

    Werkt op blokniveau (top-level HTML-elementen), niet op het hele document:
    sommige nieuwsbrieven bundelen Engelse en Nederlandse fragmenten in één mail
    (bv. Readwise's dagelijkse digest, die highlights uit boeken in beide talen
    samenvoegt). Een taalbeslissing over het HELE document liet dan de Engelse
    stukken onvertaald staan zodra Nederlandse tekst in het geheel overheerste.
    Aangrenzende blokken met dezelfde taal worden samengevoegd tot een chunk
    (tot max_chunk_size), zodat vertalen niet per zin een aparte API-aanroep kost.

    Args:
        html_content: De HTML-string om te (deels) te vertalen.
        openai_api_key: OpenAI API key.

    Returns:
        (html, translated_share) — het aandeel (0..1) van de zichtbare tekst dat
        vertaald is. > 0 betekent dat de HTML gewijzigd is; main.py toont het
        label "vertaald" pas bij een substantieel aandeel, zodat een Nederlandse
        nieuwsbrief met één Engels videobijschrift niet als vertaald te boek staat.
    """
    client = OpenAI(api_key=openai_api_key)

    # Verlaagd van 8000: bij lange, markup-zware nieuwsbrieven (veel geneste
    # <table>/inline-style opmaak, zoals Lenny's Newsletter) kon een chunk van
    # 8000 tekens tóch over de output-tokenlimiet van gpt-4o-mini heen gaan en
    # halverwege afkappen (finish_reason="length") — met een Engelse staart
    # tot gevolg. _translate_chunk() splitst zo'n chunk nu alsnog verder bij
    # afkapping (zie daar), maar een kleinere start-chunk maakt dat minder nodig.
    max_chunk_size = 6000

    grouped = _group_by_language(html_content, max_chunk_size)
    if not grouped:
        return html_content, 0.0

    total_chars = _text_len(html_content) or 1
    translated_chars = 0
    result_parts = []
    for chunk_html, lang in grouped:
        if lang != "en":
            result_parts.append(chunk_html)
            continue
        logger.info(f"  Vertalen blok ({len(chunk_html)} tekens)...")
        result_parts.append(_translate_chunk(client, chunk_html))
        translated_chars += _text_len(chunk_html)

    # Tweede ronde: losse Engelse alinea's die in een overwegend Nederlands blok
    # zaten, werden als "Nederlands" doorgelaten (bv. een Engelse zin tussen
    # Nederlandse Readwise-citaten).
    result, swept_chars = _translate_residual_english(client, "".join(result_parts))
    translated_chars += swept_chars
    return result, min(1.0, translated_chars / total_chars)


def translate_quotes(html_content: str, openai_api_key: str, selector: str) -> tuple[str, float]:
    """
    Vertaal alleen de duidelijk Engelse elementen die `selector` raakt (bv. de
    citaten van de Readwise-rubriek) en laat al het andere — boektitels,
    auteursnamen — onaangeroerd. Zelfde returnwaarde als translate_html().
    """
    client = OpenAI(api_key=openai_api_key)
    total_chars = _text_len(html_content) or 1
    result, chars = _translate_residual_english(client, html_content, selector=selector)
    return result, min(1.0, chars / total_chars)


_LEAF_BLOCK_TAGS = [
    "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th",
    "blockquote", "figcaption", "dt", "dd", "div",
]


def _is_clearly_english(text: str) -> bool:
    """Strenger dan detect_language(): alleen voor korte, losse alinea's."""
    words = re.findall(r"\b[a-z]+\b", text.lower())
    if len(words) < 6:
        return False
    en = sum(1 for w in words if w in _ENGLISH_MARKERS)
    nl = sum(1 for w in words if w in _DUTCH_MARKERS)
    return en >= 2 and en > nl * 1.5


def _translate_residual_english(
    client: OpenAI, html_content: str, max_batch_chars: int = 6000, max_items: int = 80,
    selector: str | None = None,
) -> tuple[str, int]:
    """
    Zoek blokken zonder geneste blokken (alinea's, lijstitems, cellen) die nog
    duidelijk Engels zijn en vertaal hun inhoud in-place, gebundeld per
    API-aanroep. Faalt een batch, dan blijft die tekst ongewijzigd staan.
    Met `selector` worden alleen die elementen bekeken. Geeft de HTML en het
    aantal vertaalde teksttekens terug.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    candidates = soup.select(selector) if selector else soup.find_all(_LEAF_BLOCK_TAGS)
    leaves = [
        el for el in candidates
        if not el.find(_LEAF_BLOCK_TAGS) and _is_clearly_english(el.get_text(" ", strip=True))
    ][:max_items]
    if not leaves:
        return html_content, 0

    logger.info(f"  Nog {len(leaves)} losse Engelse alinea('s) — navertalen...")
    batches, current, size = [], [], 0
    for el in leaves:
        inner = el.decode_contents()
        if current and size + len(inner) > max_batch_chars:
            batches.append(current)
            current, size = [], 0
        current.append((el, inner))
        size += len(inner)
    if current:
        batches.append(current)

    translated_chars = 0
    for batch in batches:
        translated = _translate_items(client, [inner for _, inner in batch])
        if not translated:
            continue
        for (el, _), new_inner in zip(batch, translated):
            translated_chars += len(el.get_text(" ", strip=True))
            el.clear()
            el.append(BeautifulSoup(new_inner, "html.parser"))

    return (str(soup), translated_chars) if translated_chars else (html_content, 0)


def _translate_items(client: OpenAI, items: list[str]) -> list[str] | None:
    """Vertaal een lijst HTML-fragmenten in één aanroep (JSON in, JSON uit)."""
    import json

    try:
        response = client.chat.completions.create(
            model=_translation_model,
            messages=[
                {"role": "system", "content": _TRANSLATE_SYSTEM_PROMPT + (
                    "\n\nJe krijgt JSON {\"items\": [...]} met HTML-fragmenten. Geef JSON "
                    "{\"items\": [...]} terug met exact evenveel fragmenten, in dezelfde "
                    "volgorde, elk vertaald naar het Nederlands."
                )},
                {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)},
            ],
            temperature=0.3,
            max_tokens=16000,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content or "{}").get("items")
    except Exception as e:
        if _is_permanent_error(e):
            raise OpenAIUnavailableError(str(e)) from e
        if _handle_model_error(e):
            return _translate_items(client, items)
        logger.warning(f"  ⚠️ Navertalen mislukt: {e}")
        return None
    if not isinstance(result, list) or len(result) != len(items) or not all(isinstance(r, str) for r in result):
        logger.warning("  ⚠️ Navertalen gaf een onverwacht antwoord — originele tekst behouden.")
        return None
    return result


def _group_by_language(html_content: str, max_chunk_size: int) -> list[tuple[str, str]]:
    """
    Groepeer aangrenzende top-level HTML-elementen per gedetecteerde taal tot
    chunks van maximaal max_chunk_size tekens.

    Elementen zonder eigen (genoeg) tekst — een los plaatje, een scheidingslijn —
    forceren geen taalwissel: ze sluiten aan bij de lopende groep, zodat zo'n
    element een doorlopend Engels of Nederlands blok niet nodeloos opknipt.
    """
    grouped: list[tuple[str, str]] = []
    current_html = ""
    current_lang: str | None = None

    for element_str in _iter_elements(html_content, max_chunk_size):
        if _has_translatable_text(element_str):
            lang = detect_language(element_str)
        elif current_lang is not None:
            lang = current_lang
        else:
            lang = "nl"

        if current_lang == lang and len(current_html) + len(element_str) <= max_chunk_size:
            current_html += element_str
        else:
            if current_html:
                grouped.append((current_html, current_lang))
            current_html = element_str
            current_lang = lang

    if current_html:
        grouped.append((current_html, current_lang))

    return grouped


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


def _translate_chunk(
    client: OpenAI, html_chunk: str, max_attempts: int = 3, _min_split_size: int = 1500
) -> str:
    """
    Vertaal een enkel stuk HTML via de OpenAI API.

    Robuust tegen de drie manieren waarop een chunk stil Engels kon blijven:
    (1) een API-fout of lege/None-respons die het origineel teruggaf,
    (2) een respons die (deels) onvertaald Engels bleef, en
    (3) een respons die halverwege afkapte op de output-tokenlimiet
    (finish_reason="length") — geobserveerd bij lange, markup-zware
    nieuwsbrieven (bv. Lenny's Newsletter). Bij (1) en (2) wordt opnieuw
    geprobeerd (tot max_attempts); bij (3) wordt de chunk in tweeën gesplitst
    en apart vertaald (zie _translate_split), zodat er geen Engelse staart
    overblijft die de taalcheck toch als "grotendeels Nederlands" doorlaat.
    Pas als het echt niet anders kan (chunk niet verder deelbaar) valt de
    functie terug op het (afgekapte) resultaat — beter imperfect dan een crash.

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
                model=_translation_model,
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
            # onvertaald/afgebroken. Dezelfde chunk nog eens proberen helpt niet
            # (identieke input geeft dezelfde afkap) — splits 'm in plaats daarvan
            # in tweeën en vertaal de helften apart. Alleen als de chunk al te
            # klein is om nog te splitsen, accepteren we het afgekapte resultaat.
            if getattr(choice, "finish_reason", None) == "length":
                if len(html_chunk) > _min_split_size:
                    logger.warning(
                        f"  ⚠️ Vertaling afgekapt op tokenlimiet ({len(html_chunk)} tekens "
                        f"chunk) — splits in tweeën en probeer opnieuw."
                    )
                    return _translate_split(client, html_chunk)
                logger.warning(
                    f"  ⚠️ Vertaling afgekapt op tokenlimiet ({len(html_chunk)} tekens chunk, "
                    f"al op minimumgrootte) — resultaat mogelijk onvolledig."
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
            if _handle_model_error(e):
                continue

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


def _translate_split(client: OpenAI, html_chunk: str) -> str:
    """
    Splits een chunk die tegen de output-tokenlimiet afkapte in twee helften
    langs element-grenzen en vertaal ze apart. Voorkomt dat een lange,
    markup-zware nieuwsbrief (bv. Lenny's Newsletter) halverwege een
    onvertaalde Engelse staart overhoudt.
    """
    halves = _split_html(html_chunk, max(len(html_chunk) // 2, 1))
    if len(halves) < 2:
        # Niet verder te splitsen (één ondeelbaar element, bv. kale tekst
        # zonder tags) — meer valt er niet aan te doen dan het te accepteren.
        return halves[0] if halves else html_chunk
    return "".join(_translate_chunk(client, half) for half in halves)


def _has_translatable_text(html_chunk: str, min_words: int = 12) -> bool:
    """
    True als de chunk genoeg gewone tekstwoorden bevat om een taalverificatie
    zinvol te maken. Voorkomt vals 'nog Engels'-alarm op stukken die vooral uit
    URLs, code of losse merknamen bestaan.
    """
    text = BeautifulSoup(html_chunk, "html.parser").get_text(separator=" ", strip=True)
    words = re.findall(r"\b[a-zA-Z]{2,}\b", text)
    return len(words) >= min_words


def _iter_elements(html_content: str, max_leaf_size: int):
    """
    Enumereer top-level HTML-elementen zonder ze samen te voegen.

    Werkt recursief: als een top-level element zelf groter is dan max_leaf_size
    (typisch bij Substack-stijl HTML met één grote geneste <table>), wordt er
    dieper in de boom gezocht naar kleinere elementen. Gebruikt door zowel
    _split_html (samenvoegen tot een grootte-chunk) als _group_by_language
    (samenvoegen tot een taal-chunk) — die twee mogen elementen niet op
    dezelfde manier groeperen, dus de enumeratie zelf is de gedeelde stap.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.find("body") or soup

    def _walk(elements):
        for element in elements:
            element_str = str(element)
            if len(element_str) > max_leaf_size and hasattr(element, "children"):
                yield from _walk(list(element.children))
            else:
                yield element_str

    yield from _walk(list(body.children))


def _split_html(html_content: str, max_size: int) -> list[str]:
    """Splits HTML in stukken van maximaal max_size tekens."""
    chunks: list[str] = []
    current_chunk: str = ""

    for element_str in _iter_elements(html_content, max_size):
        if len(current_chunk) + len(element_str) > max_size and current_chunk:
            chunks.append(current_chunk)
            current_chunk = element_str
        else:
            current_chunk += element_str

    if current_chunk:
        chunks.append(current_chunk)

    if not chunks:
        return [html_content]

    logger.debug(f"  HTML gesplitst in {len(chunks)} chunks (max_size={max_size})")
    return chunks


_DIGEST_SYSTEM_PROMPT = (
    "Je vat een nieuwsbrief met productupdates samen voor een Nederlandse weekkrant "
    "die op papier gelezen wordt.\n"
    "- Maak per update één blok: <h3>korte Nederlandse kop</h3> gevolgd door één <p> "
    "van 2-3 zinnen: wat verandert er, voor wie is het nuttig, en wanneer (uitroldatum) "
    "als dat genoemd wordt.\n"
    "- Laat weg: lijsten met licenties/edities, links naar helpcentra, beheerdersinstellingen "
    "zonder inhoud, 'eerdere berichten' en mailvoet.\n"
    "- Feitelijk en nuchter, geen hype. Verzin niets.\n"
    "- Geef ALLEEN de HTML terug, zonder uitleg of code-fences."
)


def summarize_digest(html_content: str, openai_api_key: str) -> str | None:
    """
    Vat een update-nieuwsbrief (bv. Google Workspace Updates: uitroldata,
    licentielijsten, helplinks — al snel 7 pagina's) samen tot korte Nederlandse
    blokken per update. Geeft None bij een fout; dan blijft het volledige stuk staan.
    """
    text = BeautifulSoup(html_content, "html.parser").get_text("\n", strip=True)[:40000]
    client = OpenAI(api_key=openai_api_key)
    for attempt in (1, 2):
        try:
            response = client.chat.completions.create(
                model=_translation_model,
                messages=[
                    {"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                temperature=0.2,
                max_tokens=4000,
            )
            content = (response.choices[0].message.content or "").strip()
            content = re.sub(r"^```(?:html)?\s*|\s*```$", "", content)
            return content if _text_len(content) >= 100 else None
        except Exception as e:
            if _is_permanent_error(e):
                raise OpenAIUnavailableError(str(e)) from e
            if _handle_model_error(e):
                continue
            logger.warning(f"  ⚠️ Samenvatten mislukt: {e}")
            return None
    return None


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
