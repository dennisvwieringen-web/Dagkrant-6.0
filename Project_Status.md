# Project Status: Dagkrant


## Fase: PLAN (Afgerond)

* [x] Idee definitie
* [x] Requirements helder (Gmail input, PDF output, Vertaling, Toon)
* [x] Technische architectuur bepaald


## Fase: SETUP (Afgerond)

* [x] Mappenstructuur aangemaakt (src/, templates/, .github/workflows/)
* [x] Python dependencies vastgelegd (requirements.txt)
* [x] .env.example en .env aangemaakt
* [x] OpenAI API key geconfigureerd
* [ ] Gmail App Password genereren (actie van gebruiker)


## Fase: BUILD (Afgerond)

* [x] src/fetcher.py - Gmail IMAP module (ophalen nieuwsbrieven)
* [x] src/translator.py - OpenAI vertaalmodule (taaldetectie + vertaling)
* [x] src/renderer.py - PDF generatie (Playwright) + e-mailverzending
* [x] src/main.py - Hoofdscript (orchestratie van alle stappen)
* [x] templates/cover.html - Jinja2 voorblad met inhoudsopgave
* [x] .github/workflows/dagkrant.yml - GitHub Actions workflow (dagelijks 16:00)


## Volgende Stappen

* [ ] Gmail App Password aanmaken (door gebruiker)
* [ ] `pip install -r requirements.txt` draaien
* [ ] `playwright install chromium` draaien
* [ ] Eerste lokale test uitvoeren: `python src/main.py`
* [ ] GitHub repository aanmaken en code pushen
* [ ] GitHub Secrets configureren (GMAIL_USER, GMAIL_APP_PASSWORD, OPENAI_API_KEY, TARGET_EMAIL)
