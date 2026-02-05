\# Project Specificaties: De Dagkrant



\## 1. Doel \& Visie

Een geautomatiseerd systeem dat dagelijks om 16:00 uur een persoonlijke "krant" (PDF) genereert op basis van ontvangen nieuwsbrieven. Het doel is om leesrust te creëren door losse mails te bundelen in één overzichtelijk, mooi vormgegeven document, vertaald naar het Nederlands waar nodig.



\## 2. Core Functionaliteiten

1\.  \*\*Email Fetching:\*\*

&nbsp;   \* Verbinding maken met Gmail via IMAP of Gmail API.

&nbsp;   \* Filteren op label: `Nieuwsbrieven` (en sublabels).

&nbsp;   \* Tijdframe: Alleen e-mails ontvangen in de laatste 24 uur (sinds de vorige run).

2\.  \*\*Content Verwerking \& Vertaling:\*\*

&nbsp;   \* Detectie van taal (Engels vs Nederlands).

&nbsp;   \* \*\*Vertaling:\*\* Engelse nieuwsbrieven worden "in-place" vertaald met behoud van originele HTML-structuur.

&nbsp;   \* \*\*Toon:\*\* De vertaling moet de originele 'stem' en toon van de auteur behouden (niet generiek/zakelijk).

&nbsp;   \* \*\*Gebruikte AI:\*\* OpenAI API.

3\.  \*\*PDF Generatie:\*\*

&nbsp;   \* \*\*Voorblad:\*\* Met datum, editienummer en een AI-gegenereerde, korte inhoudsopgave (titels/afzenders).

&nbsp;   \* \*\*Body:\*\* De nieuwsbrieven achter elkaar geplaatst.

&nbsp;   \* \*\*Layout:\*\* Behoud van originele styling (CSS/HTML) van de nieuwsbrief.

&nbsp;   \* \*\*Paginering:\*\* 'Smart page breaks' toepassen om te voorkomen dat afbeeldingen of tekstblokken lelijk worden doorgeknipt.

4\.  \*\*Distributie:\*\*

&nbsp;   \* Verzending van de PDF als bijlage via e-mail.

&nbsp;   \* Ontvanger: `WGN@fioretti.nl`.



\## 3. Technische Constraints \& Keuzes

\* \*\*Platform:\*\* GitHub Actions (Scheduled workflow, cronjob).

\* \*\*Taal:\*\* Python (voorkeur vanwege sterke libraries voor data, email en AI).

\* \*\*Authenticatie:\*\* Gmail App Password (2FA is actief).

\* \*\*Privacy:\*\* Verwerking gebeurt in ephemeral runners; geen permanente opslag van e-mailinhoud buiten de run.



\## 4. Mijlpalen (Roadmap)

\* \*\*Mijlpaal 1 (Proof of Concept):\*\* Script kan inloggen, 1 mail ophalen en als ruwe PDF mailen.

\* \*\*Mijlpaal 2 (De Krant):\*\* Meerdere mails samenvoegen, voorblad toevoegen, PDF styling verbeteren (page breaks).

\* \*\*Mijlpaal 3 (De Vertaler):\*\* Integratie OpenAI voor vertaling van Engelse content en generatie inhoudsopgave.

\* \*\*Mijlpaal 4 (Automatisering):\*\* Deployment naar GitHub Actions met timer op 16:00.

