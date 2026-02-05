\# Architectuur \& Data Flow



\## Flow Diagram

1\.  \*\*Trigger:\*\* GitHub Action Cron Schedule (Dagelijks 15:00 UTC = 16:00 NL tijd).

2\.  \*\*Input:\*\* Python script connect met Gmail (via IMAP/API + App Password).

3\.  \*\*Filter:\*\* Script selecteert messages met label `Nieuwsbrieven` van < 24u oud.

4\.  \*\*Processing Loop (per email):\*\*

&nbsp;   \* Extract HTML content.

&nbsp;   \* \*\*Check Language:\*\* Is het Engels?

&nbsp;   \* \*\*IF Engels:\*\* Stuur tekst-nodes naar OpenAI API -> "Vertaal met behoud van HTML tags en originele toon" -> Vervang originele tekst.

&nbsp;   \* \*\*Meta-data extractie:\*\* Haal titel/onderwerp op voor de inhoudsopgave.

5\.  \*\*Composing:\*\*

&nbsp;   \* Genereer HTML Voorblad (Jinja2 template).

&nbsp;   \* Genereer HTML Inhoudsopgave.

&nbsp;   \* Merge alle HTML's (Voorblad + TOC + Nieuwsbrieven) tot één lang HTML document.

&nbsp;   \* Injecteer CSS voor "page-break-inside: avoid" op belangrijke elementen.

6\.  \*\*Rendering:\*\* Converteer gecombineerde HTML naar PDF (via Playwright/WeasyPrint).

7\.  \*\*Output:\*\* Verstuur e-mail met PDF attachment via SMTP naar werkadres.



\## Bestandsstructuur (Voorstel)

/

├── .github/workflows/  # De timer instellingen

├── src/

│   ├── fetcher.py      # Gmail logica

│   ├── translator.py   # OpenAI logica

│   ├── renderer.py     # PDF generatie

│   └── main.py         # De dirigent die alles aanstuurt

├── templates/          # HTML templates voor voorblad

├── requirements.txt

└── .env                # Lokale secrets (niet op GitHub!)

