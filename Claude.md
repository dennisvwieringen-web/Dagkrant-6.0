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
```

**Required `.env` file** (copy from `.env.example`):
```
GMAIL_USER=...
GMAIL_APP_PASSWORD=...      # Gmail App Password (not regular password)
OPENAI_API_KEY=...
TARGET_EMAIL=...
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
```

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
send_email_with_pdf() via SMTP
```

### Module responsibilities

| File | Role |
|---|---|
| `src/main.py` | Orchestration. Sender deduplication, content validation (two-pass), PDF page-count check |
| `src/fetcher.py` | Gmail IMAP. Multi-label support (including sublabels like `Nieuwsbrieven/AI Report`). Forwards detection |
| `src/cleaner.py` | HTML sanitization. The largest module (~950 lines). Multi-pass cleaning |
| `src/translator.py` | Language detection (heuristic word-list) + OpenAI translation. TOC entry generation |
| `src/renderer.py` | Jinja2 cover page, CSS composition, Playwright PDF rendering, SMTP sending |
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

### Schedule (GitHub Actions)

| Day | Hours lookback | Why |
|---|---|---|
| Monday | 72h | Covers Fri 16:00 → Mon 16:00 (weekend + Monday) |
| Wednesday | 48h | Covers Mon 16:00 → Wed 16:00 |
| Thursday | 24h | Previous day only |
| Friday | 24h | Previous day only |

---

## Key Design Decisions

**Cleaning before translation:** HTML is always cleaned before sending to OpenAI. This reduces token cost and prevents the translator from adding code-fence artefacts around already-processed content.

**Two-pass visible-text check:** `_get_truly_visible_text()` strips `<style>`, `display:none` elements, and `&nbsp;` spacers before counting characters. This catches Substack-style emails with hidden preview text and email layout tables that inflate the character count. The first pass (300 chars) runs after cleaning; the second pass (100 chars) runs after deduplicate_title + translation as a final safety net.

**Language detection bias:** The heuristic requires Dutch to score ≥ 1.3× English markers before classifying as Dutch. When in doubt, it translates. False positives (unnecessary translation) are preferred over leaving English in the output.

**Article processing is per-article resilient:** Every article is wrapped in `try/except`. A crash in one article logs the error and continues — the PDF is never blocked by a single bad email.

**TOC uses content snippet:** `generate_toc_entry()` receives the first 400 chars of visible article text, enabling factual descriptions instead of subject-line guesses. The prompt explicitly forbids clickbait phrases like "Ontdek..." or "Verken...".

**PDF rendering via Playwright:** WeasyPrint was considered but Playwright (Chromium headless) gives better CSS support. The HTML is written to a temp file and loaded via `file:///` to avoid network latency. Timeout is 60s with a 2-second buffer after `networkidle` for images. After rendering, pypdf validates the page count and warns if pages are missing.

**Edition numbering:** `(today - 2025-01-01).days + 1` — purely date-based, no state file needed.

**Run context is `src/`, not root:** All modules use bare imports (`from fetcher import ...`). Running `python src/main.py` from the repo root fails with `ModuleNotFoundError`. GitHub Actions handles this via `cd src && python main.py`.

**Footer removal is position-aware:** `_remove_footers()` only scans the bottom 40% of elements (min. 30). This prevents footer patterns (like `© 2025`) appearing in article body text from triggering removal of the entire article content. The 600-char parent-climb limit still applies.

**Kill-list respects mixed containers:** `_remove_killlisted_elements()` checks whether a container also has valuable non-kill content (> 80 chars). If so, only the kill-matching children are removed, preserving article text. This prevents Readwise-style blocks (quote + "Favorite/Share" boilerplate) from being wiped entirely.

**AI artifacts are stripped twice:** Once inside `clean_html()` before translation, and once after `translate_html()` via `strip_ai_artifacts()`. The translator (GPT-4o-mini) can introduce new code fences that the initial cleaning pass cannot anticipate.

**Print link colour:** `a { color: #333 !important }` in `compose_full_html()` overrides browser-blue links for all article content. Weaken this only if a specific newsletter needs coloured links.

**Cover TOC uses CSS columns:** `column-count: 2` on `.toc-list` in `cover.html`. Requires `break-inside: avoid` on `.toc-item` — without it Chromium splits individual TOC entries across columns.
