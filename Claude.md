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
7. `_remove_comments()` + scripts/noscript
8. `_remove_html_artifact()` — stray "html" text nodes from nested `<html>` tags
9. `_remove_tracking_pixels()` — 1×1 images
10. `_flatten_nrc_drop_caps()` + `_remove_nrc_promo_footer()` — NRC-specific fixes
11. `_remove_killlisted_elements()` — kill-list patterns (browser-view prompts, social buttons, placeholders, website templates)
12. `_remove_boilerplate_intros()` — known newsletter opening phrases
13. `_remove_advertisements()` — ad blocks + their siblings
14. `_remove_footers()` — unsubscribe sections, addresses, powered-by footers (bottom 40% only)
15. `_remove_empty_containers()` — cleanup orphaned divs/tds

Public utility functions: `is_website_template()`, `deduplicate_title()`, `strip_ai_artifacts()`

### Key thresholds (defined in `main.py`)

| Constant | Value | Purpose |
|---|---|---|
| `MAX_PER_SENDER` | 3 | Max articles per unique sender per edition |
| min visible text | 300 chars | Articles below this after cleaning are skipped (early check) |
| min final text | 100 chars | Articles below this after ALL processing are skipped (final safety net) |

**Geen artikel- of lengtebeperkingen:** nieuwsbrieven worden volledig weergegeven, zonder afkap. Er is geen maximumaantal artikelen per PDF.

### Schedule & CI

**Workflow file:** `.github/workflows/dagkrant.yml` · **Lokale trigger:** `run_dagkrant.ps1`

**Bezorging op ma/wo/do/vr om 16:00 CEST.** Altijd 24 uur terugkijken. **De pijplijn draait altijd in de cloud (GitHub Actions); de pc start die alleen stipt op tijd.**

**Trigger-architectuur (waarom dit zo is):** Een **Windows Taakplanner-taak `Dagkrant-1600`** op de pc van Dennis (ma/wo/do/vr, 16:00) draait `run_dagkrant.ps1`, dat via de GitHub-API `workflow_dispatch` aanroept — die start de cloud-run binnen seconden, zonder GitHub-wachtrij. De krant wordt dus volledig in de cloud opgehaald, vertaald, gerenderd en gemaild. Voorwaarde: pc aan + ingelogd om 16:00 (geldt, want de krant wordt op het werk geprint).

*Waarom niet lokaal draaien:* het werknetwerk **blokkeert de mailpoorten** (IMAP 993 / SMTP 587) — getest met `Test-NetConnection`. Alleen HTTPS (443) werkt, precies genoeg om de cloud-run aan te sturen. *Waarom niet GitHub-cron als primair:* die wordt best-effort uitgesteld (geobserveerd: 18:33 CEST i.p.v. 16:00). De eerdere externe trigger (cron-job.org) viel uit — vermoedelijk een verlopen PAT.

**Authenticatie zonder aparte PAT:** `run_dagkrant.ps1` haalt het GitHub-token op uit **Git Credential Manager** (`git credential fill` — hetzelfde `gho_`-token dat `git push` gebruikt; bleek `workflow_dispatch`-rechten te hebben). Geen secret in de repo, geen losse PAT. Werkt non-interactief zolang de taak draait als de ingelogde gebruiker (vandaar `-LogonType Interactive`). Verloopt het token → eenmalig `git push` of `git fetch` doen ververst het via GCM.

**Achtervang + geen dubbele verzending:** de `schedule`-cron (`0 15 * * 1,3,4,5`, ~17:00 CEST, wintertijd `0 16`) blijft staan als **late vangnet** voor dagen dat de pc om 16:00 uit stond. Dubbel-verzending is uitgesloten door de **dagmarkering in de Actions-cache** (key `dagkrant-sent-<datum>`, Europe/Amsterdam): op een normale dag verstuurt de 16:00-dispatch en zet de markering; de late `schedule`-cron ziet de cache-hit en slaat alles over. Beide triggers draaien dezelfde workflow, dus de cache geldt voor allebei. De markering wordt alleen bij `success()` geschreven, zodat een gefaalde run opnieuw mag. Een `concurrency`-group (`dagkrant-edition`) serialiseert gelijktijdige runs.

**Lokale taak beheren** (PowerShell): `Get-ScheduledTaskInfo -TaskName Dagkrant-1600` (volgende/laatste run + resultaat), `Start-ScheduledTask -TaskName Dagkrant-1600` (nu triggeren/testen), `Disable-ScheduledTask` / `Enable-ScheduledTask` (tijdelijk uit/aan). Aangemaakt met `Register-ScheduledTask` + `-LogonType Interactive` (geen wachtwoordopslag). **Let op:** een taaknaam mag geen `:` bevatten — vandaar `Dagkrant-1600` i.p.v. `Dagkrant 16:00`.

Runs on `ubuntu-latest` met Python 3.12; Playwright Chromium wordt gecachet. Secrets (`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `OPENAI_API_KEY`, `TARGET_EMAIL`, `KINDLE_EMAIL`) staan in GitHub repository secrets.

**Debugging:** trigger lokaal → `logs/dagkrant-<datum>.log` (alleen de dispatch-status). De inhoudelijke run → GitHub UI → Actions → klik de run → vouw "Run De Dagkrant" uit. Via de API: `GET /repos/dennisvwieringen-web/Dagkrant-6.0/actions/runs` met het GCM-token.

---

## Known Behaviour & Solved Issues

**Substack-style newsletters (Cal Newport, Lenny's Newsletter)** have two previously solved problems worth remembering:

1. **Empty content (solved):** `display:none` preview text and `&nbsp;` spacers inflated the character count past the 300-char threshold. Fixed via `_get_truly_visible_text()` which strips these before counting.
2. **English content not translated (solved):** Substack HTML is one giant nested `<table>`. The old `_split_html()` produced a single chunk larger than GPT-4o-mini's output limit → silent fallback to English. Fixed by making `_split_html()` recursive (descends into oversized elements to find split points). `max_tokens=16000` added to prevent output truncation.

---

## Key Design Decisions

**Run context is `src/`, not root:** All modules use bare imports (`from fetcher import ...`). Running `python src/main.py` from the repo root fails with `ModuleNotFoundError`. GitHub Actions handles this via `cd src && python main.py`.

**Cleaning before translation:** HTML is always cleaned before sending to OpenAI. This reduces token cost and prevents the translator from adding code-fence artefacts around already-processed content.

**Two-pass visible-text check:** `_get_truly_visible_text()` strips `<style>`, `display:none` elements, and `&nbsp;` spacers before counting characters. The first pass (300 chars) runs after cleaning; the second pass (100 chars) runs after deduplicate_title + translation as a final safety net.

**Language detection bias:** The heuristic requires Dutch to score ≥ 1.3× English markers before classifying as Dutch. When in doubt, it translates — false positives (unnecessary translation) are preferred over leaving English in the output.

**Per-article resilience:** Every article is wrapped in `try/except`. A crash in one article logs the error and continues — the PDF is never blocked by a single bad email.

**TOC uses content snippet:** `generate_toc_entry()` receives the first 400 chars of visible article text, enabling factual descriptions instead of subject-line guesses. The prompt explicitly forbids clickbait phrases like "Ontdek..." or "Verken...".

**PDF rendering via Playwright:** WeasyPrint was considered but Playwright (Chromium headless) gives better CSS support. HTML is written to a temp file and loaded via `file:///`. Timeout is 60s with a 2-second buffer after `networkidle`. After rendering, pypdf validates the page count.

**Edition numbering:** `(today - 2025-01-01).days + 1` — purely date-based, no state file needed.

**Footer removal is position-aware:** `_remove_footers()` only scans the bottom 40% of elements (min. 30). This prevents footer patterns in article body text from triggering removal. The parent-climb limit (`_find_smallest_killable_parent`) only climbs if the parent adds ≤ 60 chars.

**Kill-list respects mixed containers:** `_remove_killlisted_elements()` checks whether a container also has valuable non-kill content (> 80 chars). If so, only the kill-matching children are removed, preserving article text.

**HTML splitting for translation is recursive:** `_split_html()` descends into child elements when a top-level element exceeds `max_chunk_size` (12K chars). This is essential for Substack/Lenny's-style emails where the entire newsletter is wrapped in one giant nested `<table>`. Without recursion, the whole email becomes a single oversized chunk that exceeds GPT-4o-mini's output limit and silently falls back to the original English.

**AI artifacts are stripped twice:** Once inside `clean_html()` before translation, and once after `translate_html()` via `strip_ai_artifacts()`. The translator (GPT-4o-mini) can introduce new code fences that the initial cleaning pass cannot anticipate.

**Print link colour:** `a { color: #333 !important }` in `compose_full_html()` overrides browser-blue links for print readability.

**Cover TOC uses CSS columns:** `column-count: 2` on `.toc-list` in `cover.html`. Requires `break-inside: avoid` on `.toc-item` — without it Chromium splits individual TOC entries across columns.
