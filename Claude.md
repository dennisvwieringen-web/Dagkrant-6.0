\# Claude Code Guidelines \& House Rules



\## Working Agreement / House Rules

1\.  \*\*Autonomy:\*\* You are authorized to apply code changes automatically without asking for confirmation for every edit. Work in a continuous flow until a task is completed.

2\.  \*\*Updates:\*\* Only stop to provide updates if you encounter a critical decision point or a high-impact choice that deviates from the plan.

3\.  \*\*Context:\*\* Use the PSB (Plan, Setup, Build) method. Do not start building until Setup is verified.



\## Tech Stack

\* \*\*Language:\*\* Python 3.x

\* \*\*Dependency Management:\*\* `pip` (use `requirements.txt`)

\* \*\*Key Libraries (Suggested):\*\*

&nbsp;   \* `google-api-python-client` / `imaplib` (Email fetching)

&nbsp;   \* `openai` (Translation \& Summarization)

&nbsp;   \* `weasyprint` OR `playwright` (HTML to PDF rendering - Playwright preferred for better CSS support)

&nbsp;   \* `jinja2` (Templating for Cover page)



\## Coding Conventions

\* \*\*Modular:\*\* Keep fetching, translating, and PDF generation in separate modules.

\* \*\*Error Handling:\*\* The script must not crash if one specific email fails to parse; skip and log instead.

\* \*\*Secrets:\*\* Never hardcode credentials. Use environment variables (load via `python-dotenv` locally).

