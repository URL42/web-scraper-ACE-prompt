# web-scraper-ACE-prompt

A sandbox of agent prototypes that call the OpenAI API alongside Playwright, Streamlit, and assorted helper scripts. The code base currently contains:

- `smart_scraper_app2.py` — Variant for iterating on tool-calling prompts without the UI baggage.
- `browser_agent.py` — Voice-enabled browsing agent that drives Chromium with Playwright, maintains a persistent profile, and follows instructions loaded from `prompt_guidelines.json`.
- `watch_scraper_bot.py` — Task runner that watches the `scrapes/` folder and summarizes new captures to `summaries/`.
- `simple_agent.py` — Minimal example of calling the Responses API and logging outputs.
- `ace.py` + `playbook.json` — Lightweight ACE (Generator/Reflector/Curator) loop with guardrails-backed self-learning.

Utility folders:
- `scrapes/`, `summaries/`, and `outputs/` store artifacts generated at runtime.
- `user_profiles/` contains Playwright user data for persistent sessions.
- `playbook.json` is created at runtime for ACE learnings (gitignored).

## Requirements
- Python 3.10+
- Dependencies declared in `requirements.txt` (install with `pip install -r requirements.txt`)
- An `OPENAI_API_KEY` in your environment for any script that instantiates `OpenAI()`
- Playwright installed with the Chromium browser for scraping features (`playwright install chromium`)

## Setup
```bash
cd web-scraper-ACE-prompt
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium  # once per machine
cp .env.template .env  # fill in your keys
```
Populate environment variables such as:
```env
OPENAI_API_KEY=sk-...
DISCORD_WEBHOOK=...
```
Some agents may expect additional keys (see prompts or inline comments for the most current list).

## Running Examples
- Smart scraper UI: `streamlit run smart_scraper_app.py`
- Background scraping / answering: `python smart_scraper_app2.py "What changed on the Notion pricing page?"`
- Browser agent UI: `streamlit run browser_agent.py` (required for the Streamlit dashboard + Playwright browser; running `python browser_agent.py` alone will show ScriptRunContext warnings)
- Watcher bot: `python watch_scraper_bot.py`
- Simple CLI demo: `python simple_agent.py`

Each script logs intermediate artifacts either to stdout or into the folders noted above so you can inspect GPT’s tool calls and responses.

## ACE self-learning (Generator → Reflector → Curator)
- Files: `ace.py`, runtime `playbook.json` (gitignored; auto-created), `guardrails.json` (what not to store).
- Prompt overlay: Each agent loads curated tips + user preferences into an extra system message on every run while keeping `prompt_guidelines.json` as the base. Tips are signature-matched to the current task with confidence scores, decay, and pruning to stay relevant.
- Preferences: In the Streamlit apps (`browser_agent.py`, `smart_scraper_app2.py`), use the sidebar “Preferences to remember” box (stored in `playbook.json` with guardrails). For `watch_scraper_bot.py`, set `ACE_PREFERENCES="comma,separated,values"` in your environment. `simple_agent.py` records learnings automatically; no UI.
- Loop cadence: After every task run (success or failure), Reflector + Curator append observations, filter with guardrails, and refresh the active tips used on the next invocation.
- Guardrails: `guardrails.json` blocks secrets/tokens and keeps learnings high-level while allowing user preference capture. Avoid pasting secrets into preferences.

## License
Licensed under the Polyform Noncommercial License 1.0.0 (no commercial use; forks and noncommercial derivatives allowed). See `LICENSE`.

## Scraping resilience defaults
- Rotating user-agents + randomized viewport sizes per run.
- Network-idle waits on navigation and human-like scroll/hover jitter.
- Extract fallback grabs full DOM snapshot if selectors fail.
- Persistent profiles kept for Playwright to reuse cookies/sessions (can disable in code).
- Selector/action errors get logged into ACE learnings to steer retries.

## Agent differences
- `browser_agent.py`: Streamlit UI + async Playwright, persistent profiles, voice input, background monitor, richer tools (screenshots/highlights/scroll), ACE sidebar, multi-step plans.
- `smart_scraper_app2.py`: Simpler Streamlit loop on sync Playwright, focuses on navigate/extract with fewer tools; good for quick prompt iteration and Q&A.
- `simple_agent.py`: Minimal CLI demo of tool-calling scrape + answer.
- `watch_scraper_bot.py`: Headless watcher that resolves a URL with GPT, scrapes, and pushes summaries.
