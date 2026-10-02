# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working Agreement

1. **Autonomy:** Apply code changes automatically without asking for confirmation. Work in a continuous flow until a task is completed.
2. **Updates:** Only stop to provide updates at critical decision points or high-impact architectural choices.
3. **Method:** Use PSB (Plan, Setup, Build). Do not start building until Setup is verified.

---

## Common Commands

```bash
# Local development setup
pip install -r requirements.txt
playwright install chromium

# Run the full pipeline locally (requires .env file)
# ⚠️  Always run from src/ — modules use relative imports and fail from root
cd src && python main.py

# Test a specific module in isolation (run from src/, no .env needed)
cd src && python -c "from cleaner import clean_html; print(clean_html('<p>Test</p>'))"

# Manually trigger a GitHub Actions run
# → GitHub UI: Actions → "De Dagkrant - Nieuwsbundel" → Run workflow

# Analyze a generated PDF (text extraction)
cd src && python -c "from pypdf import PdfReader; r = PdfReader('../path/to/dagkrant.pdf'); [print(p.extract_text()) for p in r.pages]"
```

**Required `.env` file** (copy from `.env.example`):
```
GMAIL_USER=...
GMAIL_APP_PASSWORD=...      # Gmail App Password (not regular password)
OPENAI_API_KEY=...
TARGET_EMAIL=...
KINDLE_EMAIL=...             # Send to Kindle e-reader (optional)
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
```

**No formal test suite.** Testing is done by running the pipeline and inspecting the PDF output. When modifying `cleaner.py`, test changes by feeding raw newsletter HTML through `clean_html()` in isolation and verifying the output still contains the article's real content.

---

## Architecture

The pipeline runs linearly in `src/main.py`. Each article is processed independently — a failure in one article never stops the rest.

```
Gmail IMAP (label: "Nieuwsbrieven")
    ↓  fetcher.py
Fetch emails → deduplicate by subject (>90% similarity) → max 3 per sender
    ↓  main.py
is_website_template()? → skip if true
    ↓  cleaner.py
clean_html() → multi-pass HTML sanitization (see below)
    ↓  main.py
_get_truly_visible_text() < 300 chars? → skip
    ↓  main.py
deduplicate_title() → hide h1/h2 matching subject
    ↓  translator.py
detect_language() → translate_html() if English (gpt-4o-mini)
    ↓  cleaner.py
strip_ai_artifacts() → remove code fences from translator output
    ↓  main.py
FINAL CHECK: _get_truly_visible_text() < 100 chars? → skip
    ↓  translator.py
generate_toc_entry() with content snippet → informative title + description
    ↓  renderer.py
render_cover_page() [Jinja2 → templates/cover.html]
compose_full_html() → single HTML doc with CSS page-break logic
render_pdf() [Playwright Chromium, headless, A4, 60s timeout]
    ↓
send_email_with_pdf() via SMTP → TARGET_EMAIL
send_email_with_pdf() via SMTP → KINDLE_EMAIL (optioneel)
send_email_with_pdf() via SMTP → READWISE_EMAIL (Readwise Reader-feed, standaard aan)
```

### Module responsibilities

| File | Role |
|---|---|
| `src/main.py` | Orchestration. Sender deduplication, content validation (two-pass), PDF page-count check |
| `src/fetcher.py` | Gmail IMAP. Multi-label support (including sublabels like `Nieuwsbrieven/AI Report`). Forwards detection |
| `src/cleaner.py` | HTML sanitization. The largest module (~950 lines). Multi-pass cleaning. **Primary source of content-loss bugs — changes here need careful before/after testing** |
| `src/translator.py` | Language detection (heuristic word-list) + OpenAI translation (gpt-4o-mini). TOC entry generation |
| `src/renderer.py` | Jinja2 cover page, CSS composition, Playwright PDF rendering, SMTP sending (email + Kindle) |
| `templates/cover.html` | Jinja2 template. Receives: `date`, `edition_number`, `newsletter_count`, `toc_entries[]` |

### cleaner.py — cleaning pipeline order

Cleaning runs in strict order; early passes enable later ones:

1. `_remove_ai_artifacts_raw()` — strip code fences (`` ```html ``, including nested/double fences)
2. `_remove_ghost_text_raw()` — remove placeholder/website-template text at string level
3. `_remove_mso_conditionals()` — strip Outlook/MSO conditional comments
4. BeautifulSoup parsing starts
5. `_remove_forwarding_headers()` — email forward metadata
6. `_remove_user_signature()` — Fioretti College-specific disclaimers
7. `_remove_comments()` + `<script>`/`<noscript>`/`<style>`/`<link>` + niet-renderbare media (`iframe`/`audio`/`video`/`embed`/`object`). **`<style>`/`<link>` worden verwijderd omdat globale resets (`body { background }`) anders over het HELE PDF lekken — incl. de voorpagina (oorzaak van de roze cover-bug).**
8. `_remove_html_artifact()` — stray "html" text nodes from nested `<html>` tags
9. `_remove_tracking_pixels()` — 1×1 images
10. `_flatten_nrc_drop_caps()` + `_remove_nrc_promo_footer()` — NRC-specific fixes
11. `_remove_killlisted_elements()` — kill-list patterns (browser-view prompts, social buttons, placeholders, website templates)
12. `_remove_boilerplate_intros()` — known newsletter opening phrases
13. `_remove_advertisements()` — ad blocks + their siblings
14. `_remove_footers()` — unsubscribe sections, addresses, powered-by footers (bottom 40% only)
15. `_remove_empty_containers()` — cleanup orphaned divs/tds

Public utility functions: `is_website_template()`, `deduplicate_title()`, `strip_ai_artifacts()`, `minimal_clean()`.

**`minimal_clean()` — vangnet tegen content-loss:** doet alleen de veilige verwijderingen (scripts/styles/media/comments/tracking, géén kill-list/footers/ads/boilerplate/lege-containers). `main.py` valt hierop terug wanneer `clean_html()` de zichtbare tekst onder de 300-drempel brengt terwijl het origineel wél inhoud had — zo verdwijnt een artikel niet meer doordat de agressieve opschoning het volledig wegvaagt.

### Key thresholds (defined in `main.py`)

| Constant | Value | Purpose |
|---|---|---|
| `_HOURS_BACK` | 192 | Terugkijkvenster weekkrant (8 dagen); verzonden-administratie voorkomt duplicaten |
| `_SEEN_RETENTION_DAYS` | 14 | Bewaartermijn verzonden-administratie (twee edities) |
| `MAX_PER_SENDER` | 5 | Max articles per unique sender per edition |
| min visible text | 300 chars | Articles below this after cleaning are skipped (early check) |
| min final text | 100 chars | Articles below this after ALL processing are skipped (final safety net) |

**Geen artikel- of lengtebeperkingen:** nieuwsbrieven worden volledig weergegeven, zonder afkap. Er is geen maximumaantal artikelen per PDF.

### Schedule & CI

**Workflow file:** `.github/workflows/dagkrant.yml` · **Lokale trigger:** `run_dagkrant.ps1`

**De krant is een weekkrant: elke vrijdag, klaar vóór de print om 12:00** (sinds 20 september 2026; daarvoor een dagkrant, 09:00 dagelijks). Dennis print hem vrijdag om 12:00 en leest hem in het weekend. **Terugkijkvenster: 8 dagen** (7 dagen = één editie, plus 1 dag overlap voor mails die pas na de vorige editie hun label kregen; Dennis labelt handmatig). De **verzonden-administratie** (message-id/URL → verzenddatum, in de Actions-cache onder key `dagkrant-seen-<run_id>`, `restore-keys: dagkrant-seen-` pakt de recentste; lokaal `logs/seen_ids.json`, pad via env `SEEN_IDS_FILE`) voorkomt duplicaten tussen edities. Regels: registratie gebeurt pas ná geslaagde verzending; álle meegewogen mails tellen als gedekt (ook wat door limieten/contentchecks afviel — elke mail krijgt precies één kans); webartikelen (stap 1b) worden op URL geregistreerd; magazine-runs raken de administratie niet aan; zonder administratie (eerste run of verlopen cache) telt alles in het venster mee. **Let op:** GitHub verwijdert Actions-cache-items die 7 dagen niet zijn gebruikt — bij een weekritme is dat krap. Gaat de administratie verloren, dan komt hooguit de overlapdag uit de vorige editie dubbel. **De pijplijn draait altijd in de cloud (GitHub Actions); de pc start die alleen stipt op tijd.**

**Trigger-architectuur (waarom dit zo is):** Een **Windows Taakplanner-taak `Weekkrant-Vrijdag`** op de pc van Dennis (wekelijks, **vrijdag 08:00**) draait `run_dagkrant.ps1`, dat via de GitHub-API `workflow_dispatch` aanroept — die start de cloud-run binnen seconden, zonder GitHub-wachtrij. De krant wordt dus volledig in de cloud opgehaald, vertaald, gerenderd en gemaild. De trigger staat om 08:00 omdat een weekkrant veel artikelen bevat en de cloud-run (vooral het vertalen) een uur of langer kan duren; zo is de krant ruim vóór de print om **12:00** binnen. De taak heeft **StartWhenAvailable**: staat de pc om 08:00 uit, dan vuurt hij alsnog zodra de pc weer aan en ingelogd is. *Historie:* de eerdere taak `Dagkrant-1500` (ma/wo/do/vr 14:30) bleek in juli 2026 spoorloos verdwenen (vermoedelijk gewist door een Windows-update); op 17 juli opnieuw geregistreerd als `Dagkrant-0830` (dagelijks 08:30); op 20 september 2026 vervangen door `Weekkrant-Vrijdag`.

*Waarom niet lokaal draaien:* het werknetwerk **blokkeert de mailpoorten** (IMAP 993 / SMTP 587) — getest met `Test-NetConnection`. Alleen HTTPS (443) werkt, precies genoeg om de cloud-run aan te sturen. *Waarom niet GitHub-cron als primair:* die wordt best-effort uitgesteld (geobserveerd: 18:33 CEST i.p.v. op tijd). De eerdere externe trigger (cron-job.org) viel uit — vermoedelijk een verlopen PAT.

**Authenticatie zonder aparte PAT:** `run_dagkrant.ps1` haalt het GitHub-token op uit **Git Credential Manager** (`git credential fill` — hetzelfde `gho_`-token dat `git push` gebruikt; bleek `workflow_dispatch`-rechten te hebben). Geen secret in de repo, geen losse PAT. Werkt non-interactief zolang de taak draait als de ingelogde gebruiker (vandaar `-LogonType Interactive`). Verloopt het token → eenmalig `git push` of `git fetch` doen ververst het via GCM.

**Achtervang + geen dubbele verzending:** de `schedule`-cron (`0 6 * * 5` = vrijdag 08:00 CEST, 07:00 CET in de winter; beheerd via de Planning-kaart in `dashboard.html`) blijft staan als **vangnet** voor een vrijdag dat de pc uit blijft — GitHub stelt scheduled crons vaak uren uit, dus de stipte route is de Taakplanner-dispatch. Dubbel-verzending is uitgesloten door de **dagmarkering in de Actions-cache** (key `dagkrant-sent-<datum>`, Europe/Amsterdam): op een normale vrijdag verstuurt de 08:00-dispatch en zet de markering; de latere `schedule`-cron ziet de cache-hit en slaat alles over. Beide triggers draaien dezelfde workflow, dus de cache geldt voor allebei. De markering wordt alleen bij `success()` geschreven, zodat een gefaalde run opnieuw mag. Een `concurrency`-group (`dagkrant-edition`) serialiseert gelijktijdige runs.

**Lokale taak beheren** (PowerShell): `Get-ScheduledTaskInfo -TaskName Weekkrant-Vrijdag` (volgende/laatste run + resultaat), `Start-ScheduledTask -TaskName Weekkrant-Vrijdag` (nu triggeren/testen), `Disable-ScheduledTask` / `Enable-ScheduledTask` (tijdelijk uit/aan). Aangemaakt met `Register-ScheduledTask` + `-LogonType Interactive` (geen wachtwoordopslag) en `-StartWhenAvailable`. **Let op:** een taaknaam mag geen `:` bevatten — vandaar `Weekkrant-Vrijdag` i.p.v. een naam met een tijd erin.

Runs on `ubuntu-latest` met Python 3.12; Playwright Chromium wordt gecachet. Secrets (`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `OPENAI_API_KEY`, `TARGET_EMAIL`, `KINDLE_EMAIL`) staan in GitHub repository secrets.

**Debugging:** trigger lokaal → `logs/dagkrant-<datum>.log` (alleen de dispatch-status). De inhoudelijke run → GitHub UI → Actions → klik de run → vouw "Run De Weekkrant" uit. Via de API: `GET /repos/dennisvwieringen-web/Dagkrant-6.0/actions/runs` met het GCM-token.

### Magazine-modus (bundel van één nieuwsbrief over een vast datumbereik)

Naast de wekelijkse editie kan dezelfde workflow ook een **magazine** genereren: **alle edities van precies één nieuwsbrief** binnen een gekozen datumbereik — bv. "elke Oliver Burkeman van mei t/m juli". Getriggerd via `dashboard.html` (kaart "Magazine maken") of direct via `workflow_dispatch` met `mode: magazine`.

**Eén nieuwsbrief per magazine is de definitie, geen toevallige beperking** (vastgesteld 29-07-2026). De nieuwsbriefkeuze is verplicht: zonder keuze stopt `main.py` met een foutmelding in plaats van "alles uit de periode" te bundelen.

- **Inputs:** `mode` (`dagkrant`/`magazine`), `newsletter`, `date_from`/`date_to` (`YYYY-MM-DD`), `title` — gedefinieerd in `.github/workflows/dagkrant.yml`, doorgegeven aan `main.py` als env vars (`MODE`, `MAGAZINE_NEWSLETTER`, `MAGAZINE_FROM`, `MAGAZINE_TO`, `MAGAZINE_TITLE`). `MAGAZINE_SENDER` wordt nog als fallback gelezen zodat een oude aanroep niet stilvalt.
- **Nieuwsbrief-rolmenu:** het dashboard toont een rolmenu met **radio-knoppen** (één keuze). De lijst in `newsletters.json` (repo-root) spiegelt de Gmail-sublabels onder "Nieuwsbrieven" en is vanuit het rolmenu zelf te beheren (+ Toevoegen / ✕ verwijderen schrijft via de GitHub Contents-API terug).
- **Titelconventies:** weekkrant-mail/PDF heet "Weekkrant — <datum>" resp. "Weekkrant <datum>.pdf"; de cover toont masthead "De Weekkrant" en het ISO-weeknummer ("Week 38") als editielabel, de PDF heeft paginanummers in de voet (`render_pdf(footer_label=...)`). Een magazine heet "Magazine — <nieuwsbrief>"; de cover krijgt masthead "Magazine" met de nieuwsbriefnaam als ondertitel. Een handmatige covertitel (`title`-input) overschrijft dit.
- **fetcher.py:** `fetch_newsletters()` accepteert `since_date`/`until_date` (i.p.v. `hours_back`) en `newsletter_label`. **Het filter kijkt uitsluitend naar de Gmail-labelnaam** en slaat niet-matchende folders in hun geheel over (scheelt ook het ophalen van al die mails); een dieper genest sublabel als "Oliver Burkeman/Archief" telt wél mee. Elke nieuwsbrief-dict krijgt een `label`-key (UTF-7-gedecodeerd, zonder "Nieuwsbrieven/"-prefix; zie `_imap_utf7_decode()` voor labels met bv. een ö). **Dedup:** Message-ID's worden pas bij *acceptatie* als gezien gemarkeerd — dezelfde mail hangt vaak onder hoofdlabel én sublabel. IMAP `BEFORE` is exclusief, dus `until_date` = gekozen einddatum + 1 dag.
- **Waarom niet op afzender/onderwerp matchen (bug gevonden 29-07-2026):** het filter matchte eerder op afzender **óf** onderwerp **óf** label als case-insensitive substring. Een magazine "Oliver Burkeman" over 1 mei – 29 juli leverde daardoor 7 artikelen op waarvan er maar 3 van Burkeman waren: de andere 4 waren **Readwise-dagmails die zijn naam in het onderwerp noemden** ("Dagelijkse Readwise: Oliver Burkeman en Ines Minten"). Het Gmail-label is de enige bron die zegt *van wie* een mail is — afzendernaam en onderwerp zeggen alleen waar hij *over* gaat. Niet terugdraaien naar substring-matching.
- **main.py:** in magazine-modus vervalt `MAX_PER_SENDER` (het hele punt is om alles van de gekozen periode/afzender te bundelen) en wordt stap 1b (handmatige "Dagkrant/Lezen"-artikelen) overgeslagen.
- **Cover & mail:** `render_cover_page()` en `send_email_with_pdf()` accepteren optionele overrides (`masthead_title`, `masthead_subtitle`, `edition_label`, `subject`, `body`, `filename`) zodat een magazine een eigen titel/onderwerp/bestandsnaam krijgt i.p.v. "De Weekkrant, Week N".
- **Dagmarkering-cache blijft ongemoeid:** een magazine-run schrijft de `dagkrant-sent-<datum>`-markering **niet** (anders zou een geslaagd magazine de dagelijkse krant van diezelfde dag blokkeren) en negeert 'm bij het bepalen of de run mag starten (anders zou een magazine niet meer werken op een dag dat de dagkrant al verstuurd is). Zie de `if:`-condities in `dagkrant.yml` (`... || github.event.inputs.mode == 'magazine'` resp. `... && github.event.inputs.mode != 'magazine'`).

---

## Known Behaviour & Solved Issues

**Dashboard-saves vermangelden UTF-8 → workflow "kwijt" bij GitHub (solved, juli 2026):** `getFile()` in `dashboard.html` decodeerde base64 met kaal `atob()` (= Latin-1), terwijl `putFile()` als UTF-8 codeert. Elke opslag via het dashboard dubbel-codeerde daardoor alle niet-ASCII-tekens. In `dagkrant.yml` werden em-dashes/lijntekens in de commentaren zo C1-controltekens (U+0080/U+0094) — verboden in YAML — waarna GitHub het bestand niet meer kon parsen en **stilletjes álle triggers deregistreerde** (workflow-`name` valt dan terug op het bestandspad; handmatige dispatch geeft een misleidende 422 "Workflow does not have workflow_dispatch trigger", en ook de schedule-cron vuurt niet meer). Fix: `getFile()` decodeert nu via `TextDecoder('utf-8')`. Herken het patroon: verschijnt de workflow in de Actions-API onder z'n pad i.p.v. z'n naam, dan is de YAML onparseerbaar.

**Substack-style newsletters (Cal Newport, Lenny's Newsletter)** have two previously solved problems worth remembering:

1. **Empty content (solved):** `display:none` preview text and `&nbsp;` spacers inflated the character count past the 300-char threshold. Fixed via `_get_truly_visible_text()` which strips these before counting.
2. **English content not translated (solved):** Substack HTML is one giant nested `<table>`. The old `_split_html()` produced a single chunk larger than GPT-4o-mini's output limit → silent fallback to English. Fixed by making `_split_html()` recursive (descends into oversized elements to find split points). `max_tokens=16000` added to prevent output truncation.
3. **Mixed English/Dutch in one edition — "eerst Engels, daarna Nederlands" (solved):** met meerdere chunks per nieuwsbrief kon één chunk stil onvertaald (Engels) terugkomen — door een API-fout, een `None`/lege respons (`.strip()` op `None` → `AttributeError`), of doordat het model de opdracht negeerde — terwijl de andere chunks wél Nederlands waren. De oude `_translate_chunk()` ving elke fout af met `return html_chunk` (origineel) en verifieerde nooit of er echt vertaald was, dus glipte gedeeltelijk-Engels ongemerkt door (VANGNET B in `main.py` grijpt alléén bij een *lege* vertaling in, niet bij een *Engelse*). Opgelost door `_translate_chunk()` robuust te maken: **retries** (`max_attempts=3`), **`None`/lege respons afvangen**, **truncatie loggen** (`finish_reason=="length"`), en na afloop **verifiëren met `detect_language()`** dat het resultaat Nederlands is — anders opnieuw proberen. De taalverificatie wordt overgeslagen bij chunks met te weinig gewone woorden (`_has_translatable_text()`, drempel 12 woorden) om vals alarm op URL/code-fragmenten te voorkomen. `max_chunk_size` verlaagd van 12000 → 8000 tekens voor meer output-marge.

**Weekkrant-verbeteringen (27 september 2026):**
- **Eigen edities genegeerd:** mails "Dagkrant/Weekkrant/Magazine — …" van `GMAIL_USER` (komen via een Gmail-filter onder "Extra artikelen") worden direct na het ophalen weggefilterd (`_is_own_edition()` in `main.py`).
- **Teasers → volledig artikel:** `expand_teaser()` in `web_article.py`. Korte mail (< 3000 tekens) mét "Lees verder"-link → volledige post ophalen via de **WordPress public REST-API** (`public-api.wordpress.com/rest/v1.1/sites/<host>/posts/slug:<slug>`); Playwright gaf op pedrodebruyckere.blog een botcontrole ("Checking your browser"). Alleen vervangen als het resultaat ≥ 1,2× langer is. Bronregel bovenaan (onderaan belandde hij soms alleen op een pagina).
- **Readwise-rubriek:** `readwise_digest.py` bundelt alle Readwise-mails (afzender `readwise.io`) tot één artikel "Readwise — citaten van de week": citaten uit `table[id^=highlight]`, ontdubbeld, per boek gegroepeerd, zonder knoppen/Location/welkomstblokken. Geen `MAX_PER_SENDER` voor Readwise. `prebuilt=True` → slaat `clean_html()` over. Niet in magazine-modus.
- **Navertalen:** na de blokvertaling zoekt `_translate_residual_english()` losse, duidelijk Engelse alinea's in een Nederlands blok en vertaalt die gebundeld (JSON in/uit).
- **Opmaak:** artikelen < 2000 tekens lopen door onder het vorige (`.flow`); tweede renderronde `find_sparse_breaks()` laat een artikel doorlopen op een pagina die bijna leeg bleef (`.fill`); bijna lege laatste pagina → herrender met `scale` 0,96/0,92.
- **Cleaner:** Readwise-knoppenrij verwijderd op class `highlight-action-row` (een tekstpatroon raakte het hele citaatblok); `_remove_trailing_footer_text()` knipt een mailvoet die als losse tekst in één groot element hangt (WilfredRubens.com: hele artikel in één `<p>`).

**Na de eerste volledige weekkrant (2 oktober 2026):**
- **Afbeeldingen verdwenen (bug):** `_remove_tracking_pixels()` matchte `width:100%` als 1px-pixel (de "1" van 100) — 24 van de 25 AI Report-afbeeldingen en alle Google Workspace-afbeeldingen werden weggegooid; alleen het bijschrift "Bron afbeelding: …" bleef staan. Regex nu verankerd op een echte waarde `0`/`1(px)` gevolgd door `;` of einde.
- **Leesvolgorde:** `_reading_order()` in `main.py` zet artikelen per bron bij elkaar, binnen een bron **oudste eerst**; bronnen op volgorde van hun nieuwste stuk, rubrieken (Readwise) achteraan. Nieuwste-eerst over de hele krant zette AI Report's donderdagnummer 30 pagina's vóór de dinsdag-voorbeschouwing, en deel 2 van een serie vóór deel 1.
- **Inhoudsopgave met paginanummers:** `render_edition()` rendert in rondes tot de opmaak stabiel is (doorloop op lege pagina's + startpagina per artikel via `article_start_pages()` op de "NR. k"-markering), max. 4 rondes.
- **Bronnamen:** `_display_sender()` + `_SENDER_NAMES` ("X, Y of Einstein?" → Pedro De Bruyckere, "WilfredRubens.com over leren en ICT" → Wilfred Rubens). Cover telt "N artikelen uit M bronnen"; datum zonder voorloopnul.
- **Update-nieuwsbrieven samenvatten:** afzenders in `_DIGEST_SENDERS` (Google Workspace) gaan door `summarize_digest()` i.p.v. integraal vertaald te worden (was 7 pagina's licentielijsten en helplinks). Label "samengevat". Mislukt het, dan volgt de normale vertaling.
- **Vertaling:** model `gpt-4.1` (env `TRANSLATION_MODEL`; valt bij "model bestaat niet" terug op `gpt-4o-mini`) — 4o-mini gaf zichtbare fouten. `translate_html()` geeft nu het **vertaalde aandeel** (0..1) terug; het label "vertaald" pas vanaf 20%. "of", "was" en "we" zijn geen Engelse markers meer (ook gewoon Nederlands): daardoor werd een Nederlandse sponsorregel van AI Report als Engels "vertaald".
- **Readwise-rubriek:** vaste kop "Readwise — citaten van de week" (`fixed_toc`), alleen de citaten worden vertaald (`translate_quotes()` met `translate_selector`), boektitels blijven origineel; citaten die alleen "(Location 1,651)" zijn vallen weg.
- **Extra boilerplate weg:** AI Report "Online lezen"/sponsorblok/"Zit je ergens mee", Cal Newport "To read or comment…"/"P.S. If someone forwarded…", Google "Previous Posts:" (`_remove_killed_sections()`) en "We've recently changed how we send these emails", Wilfred Rubens "Mijn bronnen over (generatieve) AI". Bronregel bij opgehaalde blogposts toont alleen het domein.

---

## Key Design Decisions

**Run context is `src/`, not root:** All modules use bare imports (`from fetcher import ...`). Running `python src/main.py` from the repo root fails with `ModuleNotFoundError`. GitHub Actions handles this via `cd src && python main.py`.

**Cleaning before translation:** HTML is always cleaned before sending to OpenAI. This reduces token cost and prevents the translator from adding code-fence artefacts around already-processed content.

**Two-pass visible-text check:** `_get_truly_visible_text()` strips `<style>`, `display:none` elements, and `&nbsp;` spacers before counting characters. The first pass (300 chars) runs after cleaning; the second pass (100 chars) runs after deduplicate_title + translation as a final safety net.

**Language detection bias:** The heuristic requires Dutch to score ≥ 1.3× English markers before classifying as Dutch. When in doubt, it translates — false positives (unnecessary translation) are preferred over leaving English in the output.

**Per-article resilience:** Every article is wrapped in `try/except`. A crash in one article logs the error and continues — the PDF is never blocked by a single bad email.

**Content-loss vangnetten (toon liever imperfect dan niets):** twee fallbacks in `main.py` voorkomen dat goede nieuwsbrieven verdwijnen. **(A)** Brengt `clean_html()` de zichtbare tekst onder de 300-drempel terwijl het origineel ≥300 had, dan valt de pijplijn terug op `minimal_clean()` i.p.v. het artikel te droppen. **(B)** Levert de vertaling (bijna) lege inhoud terwijl het Engelse origineel inhoud had, dan blijft het Engelse origineel staan (`was_translated` weer op `False`). Beide zijn geobserveerd in productie (Wilfred Rubens resp. The New Yorker, 18 juni 2026): zonder de vangnetten werden die artikelen volledig overgeslagen.

**TOC uses content snippet:** `generate_toc_entry()` receives the first 400 chars of visible article text, enabling factual descriptions instead of subject-line guesses. The prompt explicitly forbids clickbait phrases like "Ontdek..." or "Verken...".

**PDF rendering via Playwright:** WeasyPrint was considered but Playwright (Chromium headless) gives better CSS support. HTML is written to a temp file and loaded via `file:///`. Timeout is 60s with a 2-second buffer after `networkidle`. After rendering, pypdf validates the page count.

**Edition numbering:** `(today - 2025-01-01).days + 1` — purely date-based, no state file needed.

**Footer removal is position-aware:** `_remove_footers()` only scans the bottom 40% of elements (min. 30). This prevents footer patterns in article body text from triggering removal. The parent-climb limit (`_find_smallest_killable_parent`) only climbs if the parent adds ≤ 60 chars.

**Kill-list respects mixed containers:** `_remove_killlisted_elements()` checks whether a container also has valuable non-kill content (> 80 chars). If so, only the kill-matching children are removed, preserving article text.

**HTML splitting for translation is recursive:** `_split_html()` descends into child elements when a top-level element exceeds `max_chunk_size` (12K chars). This is essential for Substack/Lenny's-style emails where the entire newsletter is wrapped in one giant nested `<table>`. Without recursion, the whole email becomes a single oversized chunk that exceeds GPT-4o-mini's output limit and silently falls back to the original English.

**AI artifacts are stripped twice:** Once inside `clean_html()` before translation, and once after `translate_html()` via `strip_ai_artifacts()`. The translator (GPT-4o-mini) can introduce new code fences that the initial cleaning pass cannot anticipate.

**Print link colour:** `a { color: #333 !important }` in `compose_full_html()` overrides browser-blue links for print readability.

**Cover TOC uses CSS columns:** `column-count: 2` on `.toc-list` in `cover.html`. Requires `break-inside: avoid` on `.toc-item` — without it Chromium splits individual TOC entries across columns.
