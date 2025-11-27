# web-scraper-ACE-prompt

A sandbox of agent prototypes that call the OpenAI API alongside Playwright, Streamlit, and assorted helper scripts. The code base currently contains:

- `browser_agent.py` — Streamlit browsing agent that drives Chromium with Playwright, maintains a persistent profile, and follows instructions loaded from `prompt_guidelines.json`.
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
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```
Some agents may expect additional keys (see prompts or inline comments for the most current list).

## Running Examples
- Browser agent UI: `streamlit run browser_agent.py` (required for the Streamlit dashboard + Playwright browser; running `python browser_agent.py` alone will show ScriptRunContext warnings)
- Watcher bot: `python watch_scraper_bot.py`
- Simple CLI demo: `python simple_agent.py`

Each script logs intermediate artifacts either to stdout or into the folders noted above so you can inspect GPT’s tool calls and responses.

## ACE self-learning (Generator → Reflector → Curator)
- Files: `ace.py`, runtime `playbook.json` (gitignored; auto-created), `guardrails.json` (what not to store).
- Prompt overlay: Each agent loads curated tips + user preferences into an extra system message on every run while keeping `prompt_guidelines.json` as the base. Tips are signature-matched to the current task with confidence scores, decay, and pruning to stay relevant.
- Preferences: In the Streamlit app (`browser_agent.py`), use the sidebar “Preferences to remember” box (stored in `playbook.json` with guardrails). For `watch_scraper_bot.py`, set `ACE_PREFERENCES="comma,separated,values"` in your environment. `simple_agent.py` records learnings automatically; no UI.
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
- `browser_agent.py`: Streamlit UI + async Playwright, persistent profiles, background monitor, richer tools (screenshots/highlights/scroll), ACE sidebar, multi-step plans.
- `simple_agent.py`: Minimal CLI demo of tool-calling scrape + answer.
- `watch_scraper_bot.py`: Headless watcher that resolves a URL with GPT, scrapes, and pushes summaries.

## Background monitors (browser_agent)
- Create multiple monitors from the Streamlit UI with schedules (`interval`, `daily`, `weekly`, `monthly`) and per-monitor labels.
- Start/pause/run-now/delete per monitor; active monitors auto-resume on app reload.
- Change detection via content hashing and optional keyword filters (`notify_mode`: on_change/on_keyword/always/none).
- Runs are stored in SQLite at `outputs/monitor/monitor.db` with raw outputs saved under `outputs/monitor/monitor_<id>/`. Export to CSV with `python3 export_monitor_db.py`.
- Optional Telegram notifications when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.

## Logging
- Per-run logs are written to `outputs/logs/run_*.json` with task, status, action/error notes, and a preview of the extracted output.
- If a run fails to extract, status is `no_extract`; check errors in the log for selector/timeouts.
- Background monitors also record into SQLite (`outputs/monitor/monitor.db`) and save raw summaries to `outputs/monitor/monitor_<monitor_id>/`.

## ACE telemetry & methodology
- Structured per-run signals: `goal_status` (`success`/`partial`/`failed`/`blocked`), `reason_for_status` (e.g., `selector_fail`, `timeout`, `captcha_block`, `login_required`, `no_relevant_content`), and a heuristic `answer_relevance_score` (higher when success with no errors; lower when failed/blocked). Hook is ready for an LLM critic later.
- Structured per-action records: each tool call logs `tool`, sanitized `args` (url/selector/text_length), `result_type` (`ok`/`soft_fail`/`hard_fail`), `error_category` (timeout/selector_fail/captcha/login_required/wrong_page/none), `message`, `latency_ms`, `url/domain`, and `retries`.
- Guardrails still apply to all stored text via `guardrails.json`; sensitive tokens/PII are redacted before persistence.
- Playbook learning: `ace.py` stores structured entries in `playbook.json` (gitignored) and curates matched tips for future prompts; tips decay and are pruned to stay relevant. Preferences are preserved and injected as an overlay, not by editing `prompt_guidelines.json`.
