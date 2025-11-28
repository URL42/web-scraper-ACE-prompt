import os
import calendar
import json
import time
import random
import asyncio
import threading
import sqlite3
import hashlib
import requests
from datetime import datetime, time as dtime, timedelta
import streamlit as st
from typing import Dict, List, Any, Optional
from pathlib import Path
from urllib.parse import urlparse
from openai import OpenAI
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from ace import ace_manager

# --- Config ---
ACE_DOMAIN = "browser_agent"
PERSISTENT_PROFILE_ENABLED = True
STEALTH_MODE_ENABLED = True
PROFILE_PATH = Path("user_profiles/default")
VIEWPORTS = [
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]
TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Singapore",
]
LOCALES = [
    "en-US",
    "en-GB",
    "en-CA",
]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
]

# --- Setup ---
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GUIDELINES_PATH = "prompt_guidelines.json"
Path("outputs").mkdir(exist_ok=True)
LOG_DIR = Path("outputs/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
MONITOR_DIR = Path("outputs/monitor")
MONITOR_DIR.mkdir(parents=True, exist_ok=True)
MONITOR_DB_PATH = MONITOR_DIR / "monitor.db"
PROFILE_PATH.mkdir(parents=True, exist_ok=True)

with open(GUIDELINES_PATH, "r", encoding="utf-8") as f:
    prompt_data = json.load(f)

# --- Monitor persistence ---
class MonitorManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS monitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    urls TEXT,
                    schedule_type TEXT DEFAULT 'interval',
                    interval_minutes INTEGER DEFAULT 300,
                    daily_time TEXT,
                    weekly_day INTEGER,
                    monthly_day INTEGER,
                    notify_mode TEXT DEFAULT 'on_change',
                    keyword_filter TEXT,
                    is_active INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    monitor_id INTEGER NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT,
                    summary TEXT,
                    content_hash TEXT,
                    raw_path TEXT,
                    matched_keywords INTEGER DEFAULT 0,
                    notified INTEGER DEFAULT 0,
                    error TEXT,
                    FOREIGN KEY (monitor_id) REFERENCES monitors(id)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_monitor_time ON runs (monitor_id, started_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_hash ON runs (monitor_id, content_hash)")
            self.conn.commit()

            # Lightweight migration for monthly_day if an existing DB lacks it.
            cur.execute("PRAGMA table_info(monitors)")
            cols = [r[1] for r in cur.fetchall()]
            if "monthly_day" not in cols:
                cur.execute("ALTER TABLE monitors ADD COLUMN monthly_day INTEGER")
                self.conn.commit()

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row) if row else {}

    def list_monitors(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM monitors ORDER BY created_at DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_monitor(self, monitor_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def create_monitor(
        self,
        name: str,
        prompt: str,
        urls: Optional[List[str]],
        schedule_type: str,
        interval_minutes: int,
        daily_time: Optional[str],
        weekly_day: Optional[int],
        monthly_day: Optional[int],
        notify_mode: str,
        keyword_filter: Optional[str],
        is_active: int = 0,
    ) -> int:
        now = datetime.utcnow().isoformat() + "Z"
        urls_json = json.dumps(urls or [])
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO monitors (name, prompt, urls, schedule_type, interval_minutes, daily_time, weekly_day, notify_mode, keyword_filter, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    prompt,
                    urls_json,
                    schedule_type,
                    interval_minutes,
                    daily_time,
                    weekly_day,
                    monthly_day,
                    notify_mode,
                    keyword_filter,
                    is_active,
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def set_active(self, monitor_id: int, active: bool):
        with self.lock:
            self.conn.execute(
                "UPDATE monitors SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if active else 0, datetime.utcnow().isoformat() + "Z", monitor_id),
            )
            self.conn.commit()

    def get_last_run(self, monitor_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE monitor_id = ? ORDER BY started_at DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_last_hash(self, monitor_id: int) -> Optional[str]:
        with self.lock:
            row = self.conn.execute(
                "SELECT content_hash FROM runs WHERE monitor_id = ? AND content_hash IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
        return row["content_hash"] if row else None

    def record_run(
        self,
        monitor_id: int,
        started_at: str,
        finished_at: str,
        status: str,
        summary: str,
        content_hash: Optional[str],
        raw_path: Optional[str],
        matched_keywords: bool,
        notified: bool,
        error: Optional[str] = None,
    ) -> int:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO runs (monitor_id, started_at, finished_at, status, summary, content_hash, raw_path, matched_keywords, notified, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    started_at,
                    finished_at,
                    status,
                    summary,
                    content_hash,
                    raw_path,
                    1 if matched_keywords else 0,
                    1 if notified else 0,
                    error,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def delete_monitor(self, monitor_id: int):
        with self.lock:
            self.conn.execute("DELETE FROM runs WHERE monitor_id = ?", (monitor_id,))
            self.conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
            self.conn.commit()

monitor_db = MonitorManager(MONITOR_DB_PATH)

# --- Monitor scheduling helpers ---
monitor_threads: Dict[int, Dict[str, Any]] = {}


def normalize_text(text: str) -> str:
    return " ".join((text or "").split())


def hash_text(text: str) -> Optional[str]:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_time_str(timestr: Optional[str]) -> dtime:
    try:
        hour, minute = (timestr or "09:00").split(":")
        return dtime(hour=int(hour), minute=int(minute))
    except Exception:
        return dtime(hour=9, minute=0)


def seconds_until_next_run(monitor: Dict[str, Any]) -> int:
    now = datetime.now()
    sched = (monitor.get("schedule_type") or "interval").lower()
    if sched == "interval":
        minutes = int(monitor.get("interval_minutes") or 300)
        return max(minutes * 60, 60)

    run_time = parse_time_str(monitor.get("daily_time"))

    if sched == "daily":
        target = datetime.combine(now.date(), run_time)
        if target <= now:
            target += timedelta(days=1)
        return int((target - now).total_seconds())

    if sched == "weekly":
        weekday = int(monitor.get("weekly_day") or 0)  # Monday = 0
        days_ahead = (weekday - now.weekday()) % 7
        target_date = now.date() + timedelta(days=days_ahead)
        target = datetime.combine(target_date, run_time)
        if target <= now:
            target += timedelta(days=7)
        return int((target - now).total_seconds())

    if sched == "monthly":
        day = int(monitor.get("monthly_day") or 1)
        year = now.year
        month = now.month
        _, last_day = calendar.monthrange(year, month)
        day = min(max(1, day), last_day)
        target = datetime.combine(now.replace(day=day).date(), run_time)
        if target <= now:
            month += 1
            if month > 12:
                month = 1
                year += 1
            _, last_day = calendar.monthrange(year, month)
            day = min(day, last_day)
            target = datetime(year, month, day, run_time.hour, run_time.minute)
        return int((target - now).total_seconds())

    return 300 * 60


def should_notify(monitor: Dict[str, Any], status: str, matched_keywords: bool) -> bool:
    mode = (monitor.get("notify_mode") or "on_change").lower()
    if mode == "none":
        return False
    if status == "error":
        return True
    if mode == "always":
        return True
    if mode == "on_change":
        return status != "no_change"
    if mode == "on_keyword":
        return matched_keywords
    return False


def send_telegram_message(text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


async def execute_monitor_run(monitor: Dict[str, Any]) -> Dict[str, Any]:
    monitor_id = monitor["id"]
    start_time = datetime.utcnow()
    summary = ""
    content_hash = None
    status = "error"
    matched_keywords = False
    raw_path = None
    error = None
    notified = False

    try:
        summary = await run_agent(monitor["prompt"], stream_output=False)
        raw_dir = MONITOR_DIR / f"monitor_{monitor_id}"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{start_time.strftime('%Y%m%dT%H%M%S')}.txt"
        raw_path.write_text(summary or "No result", encoding="utf-8")

        normalized = normalize_text(summary)
        content_hash = hash_text(normalized)
        last_hash = monitor_db.get_last_hash(monitor_id)

        keyword_filter = (monitor.get("keyword_filter") or "").strip()
        if keyword_filter and normalized:
            matched_keywords = keyword_filter.lower() in normalized.lower()

        if content_hash and last_hash and content_hash == last_hash:
            status = "no_change"
        elif matched_keywords:
            status = "keyword_match"
        else:
            status = "success"

    except Exception as e:
        error = str(e)
        status = "error"

    should_alert = should_notify(monitor, status, matched_keywords)
    if should_alert:
        snippet = (summary or "No summary")[:800]
        parts = [
            f"*Monitor*: {monitor.get('name')}",
            f"*Status*: {status}",
        ]
        if matched_keywords:
            parts.append("Keyword matched ✅")
        if error:
            parts.append(f"*Error*: {error}")
        parts.append(f"*Summary*: {snippet}")
        if raw_path:
            parts.append(f"*File*: {raw_path}")
        notified = send_telegram_message("\n".join(parts))

    run_id = monitor_db.record_run(
        monitor_id=monitor_id,
        started_at=start_time.isoformat() + "Z",
        finished_at=datetime.utcnow().isoformat() + "Z",
        status=status,
        summary=summary or "",
        content_hash=content_hash,
        raw_path=str(raw_path) if raw_path else None,
        matched_keywords=matched_keywords,
        notified=notified,
        error=error,
    )

    return {
        "run_id": run_id,
        "status": status,
        "matched_keywords": matched_keywords,
        "notified": notified,
        "raw_path": raw_path,
        "error": error,
    }


async def monitor_loop(monitor_id: int, stop_event: threading.Event):
    while not stop_event.is_set():
        monitor = monitor_db.get_monitor(monitor_id)
        if not monitor:
            break

        await execute_monitor_run(monitor)

        delay = seconds_until_next_run(monitor)
        waited = 0
        while waited < delay and not stop_event.is_set():
            await asyncio.sleep(min(5, delay - waited))
            waited += min(5, delay - waited)


def start_monitor_runner(monitor_id: int) -> bool:
    monitor = monitor_db.get_monitor(monitor_id)
    if not monitor:
        return False
    if monitor_id in monitor_threads and monitor_threads[monitor_id]["thread"].is_alive():
        return False

    stop_event = threading.Event()

    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(monitor_loop(monitor_id, stop_event))

    thread = threading.Thread(target=run_loop, daemon=True)
    monitor_threads[monitor_id] = {"thread": thread, "stop_event": stop_event}
    monitor_db.set_active(monitor_id, True)
    thread.start()
    return True


def stop_monitor_runner(monitor_id: int):
    entry = monitor_threads.get(monitor_id)
    if entry:
        entry["stop_event"].set()
    monitor_db.set_active(monitor_id, False)
    monitor_threads.pop(monitor_id, None)


def resume_active_monitors():
    for monitor in monitor_db.list_monitors():
        if monitor.get("is_active"):
            start_monitor_runner(monitor["id"])


def delete_monitor(monitor_id: int):
    stop_monitor_runner(monitor_id)
    monitor_db.delete_monitor(monitor_id)

# --- Async Browser Controller ---
class AsyncBrowserController:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        self.playwright = await async_playwright().start()
        viewport = random.choice(VIEWPORTS)
        locale = random.choice(LOCALES)
        timezone_id = random.choice(TIMEZONES)
        extra_headers = {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": locale}

        if PERSISTENT_PROFILE_ENABLED:
            self.browser = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_PATH),
                headless=False,
                viewport=viewport,
                locale=locale,
                timezone_id=timezone_id,
                extra_http_headers=extra_headers,
            )
            self.page = self.browser.pages[0] if self.browser.pages else await self.browser.new_page()
        else:
            self.browser = await self.playwright.chromium.launch(headless=False, slow_mo=200)
            self.context = await self.browser.new_context(
                viewport=viewport,
                locale=locale,
                timezone_id=timezone_id,
                extra_http_headers=extra_headers,
            )
            self.page = await self.context.new_page()

        if STEALTH_MODE_ENABLED:
            await self.page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """)

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def simulate_human(self):
        await asyncio.sleep(random.uniform(2.0, 3.5))
        await self.page.mouse.wheel(0, random.randint(400, 900))
        await self.page.mouse.move(random.randint(50, 300), random.randint(150, 400))
        await asyncio.sleep(random.uniform(0.4, 0.9))

    async def run_action(self, tool: str, args: Dict[str, str]) -> str:
        try:
            if tool == "navigate":
                await self.page.goto(args["url"], wait_until='networkidle', timeout=120000)
                await asyncio.sleep(random.uniform(1.5, 3.5))
                await self.simulate_human()

                # Consent screen bypass (Google)
                if "google.com" in args["url"]:
                    try:
                        await self.page.click("button:has-text('Accept all')", timeout=3000)
                        return "✅ Navigated and accepted Google consent"
                    except:
                        pass

                return f"✅ Navigated to {args['url']}"

            elif tool == "click":
                try:
                    await self.page.click(args["selector"], timeout=5000)
                    await self.simulate_human()
                    return f"🖱️ Clicked {args['selector']}"
                except Exception as e:
                    try:
                        await self.page.hover(args["selector"])
                        await asyncio.sleep(1)
                        await self.page.click(args["selector"], timeout=3000)
                        await self.simulate_human()
                        return f"🖱️ Clicked {args['selector']} after hover"
                    except:
                        await self.page.eval_on_selector(args["selector"], "el => el.style.outline = '3px solid red'")
                        return f"⚠️ Failed to click {args['selector']} (highlighted instead) → {e}"

            elif tool == "click_text":
                text = args["text"].lower()
                elements = await self.page.query_selector_all("*")
                for el in elements:
                    try:
                        content = (await el.inner_text()).strip().lower()
                        if text in content:
                            await el.click()
                            await self.simulate_human()
                            return f"🖱️ Clicked element containing text: '{args['text']}'"
                    except:
                        continue
                return f"⚠️ Could not find element with text: '{args['text']}'"

            elif tool == "type_text":
                try:
                    element = await self.page.wait_for_selector(args["selector"], timeout=7000)
                    await element.click()
                    await element.fill("")
                    await element.type(args["text"])
                    await self.simulate_human()
                    return f"⌨️ Typed '{args['text']}' into {args['selector']}"
                except Exception as e:
                    # Retry with fallback selector if Google
                    if "google.com" in self.page.url and args["selector"] == "input[name='q']":
                        try:
                            fallback = "textarea[name='q']"
                            el = await self.page.wait_for_selector(fallback, timeout=5000)
                            await el.click()
                            await el.fill("")
                            await el.type(args["text"])
                            await self.simulate_human()
                            return f"⌨️ Fallback typed into '{fallback}'"
                        except:
                            pass
                    await self.page.eval_on_selector(args["selector"], "el => el.style.outline = '3px solid orange'")
                    return f"⚠️ Could not type into {args['selector']} (highlighted) → {e}"

            elif tool == "extract":
                try:
                    captcha_check_attempted = False
                    while True:
                        content = await self.page.inner_text(args["selector"])
                        if (not captcha_check_attempted and 
                            any(term in content.lower() for term in ["captcha", "verify", "robot", "access denied"])):
                            await self.page.eval_on_selector("body", "el => el.style.outline = '3px solid orange'")
                            st.warning("🧍 CAPTCHA detected — pausing browser for manual solve.")
                            await self.page.pause()

                            # Post-CAPTCHA resume
                            await self.page.mouse.wheel(0, 200)
                            await asyncio.sleep(1.5)
                            await self.page.wait_for_selector(args["selector"], timeout=10000)
                            captcha_check_attempted = True
                            continue  # Retry extract after pause

                        return content[:2000]

                except Exception as e:
                    try:
                        html = await self.page.content()
                        return f"Fallback DOM extract (body): {html[:1500]}"
                    except Exception as inner_e:
                        return f"⚠️ Could not extract from {args['selector']} → {e} / fallback failed: {inner_e}"

            elif tool == "screenshot":
                path = args.get("path", "outputs/page.png")
                await self.page.screenshot(path=path, full_page=True)
                return f"📷 Screenshot saved to {path}"

            elif tool == "highlight":
                await self.page.eval_on_selector(args["selector"], "el => el.style.outline = '3px solid red'")
                return f"🔍 Highlighted {args['selector']}"

            elif tool == "scroll":
                await self.page.mouse.wheel(0, 1000)
                return "🌀 Scrolled down"

            elif tool == "wait_for":
                try:
                    await self.page.wait_for_selector(args["selector"], timeout=15000)
                    return f"⏳ Waited for {args['selector']}"
                except PlaywrightTimeout:
                    await self.page.mouse.wheel(0, 300)
                    await asyncio.sleep(2)
                    try:
                        await self.page.wait_for_selector("body", timeout=8000)
                        return f"⏳ Fallback: waited for generic 'body' after failing '{args['selector']}'"
                    except:
                        return f"⚠️ Timeout waiting for: {args['selector']} (even after fallback)"
            else:
                return f"❓ Unknown tool: {tool}"

        except PlaywrightTimeout:
            return f"⚠️ Timeout waiting for: {args}"
        except Exception as e:
            return f"❌ Error during {tool}: {e}"

# --- Telemetry helpers ---
def build_action_record(tool: str, args: Dict[str, Any], result: str, latency_ms: int, url: str, retries: int = 0) -> Dict[str, Any]:
    def sanitize_args(a: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if a.get("url"):
            out["url"] = str(a.get("url"))[:200]
        if a.get("selector"):
            out["selector"] = str(a.get("selector"))[:200]
        if a.get("text"):
            out["text_length"] = len(str(a.get("text")))
        return out

    msg_lower = (result or "").lower()
    error_category = "none"
    if "captcha" in msg_lower:
        error_category = "captcha"
    elif "timeout" in msg_lower:
        error_category = "timeout"
    elif "login" in msg_lower:
        error_category = "login_required"
    elif "selector" in msg_lower or "could not" in msg_lower:
        error_category = "selector_fail"

    result_type = "ok"
    if result.startswith("⚠️"):
        result_type = "soft_fail"
    if result.startswith("❌") or error_category in {"timeout", "captcha", "login_required", "selector_fail"}:
        result_type = "hard_fail"

    return {
        "tool": tool,
        "args": sanitize_args(args),
        "result_type": result_type,
        "error_category": error_category,
        "message": (result or "")[:300],
        "latency_ms": int(latency_ms),
        "url": (url or "")[:200],
        "retries": retries,
    }

def infer_goal(summary: str, action_records, errors) -> Dict[str, str]:
    summary_ok = bool(summary and summary.strip())
    error_cats = [a.get("error_category", "none") for a in action_records if isinstance(a, dict)]
    if any(cat in {"captcha", "login_required"} for cat in error_cats):
        status = "blocked"
    elif summary_ok and not errors:
        status = "success"
    elif summary_ok:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "partial"
    reason = next((c for c in error_cats if c and c != "none"), "no_relevant_content" if not summary_ok else "")
    return {"goal_status": status, "reason_for_status": reason}

# --- GPT + Tool Execution + Summary ---
def log_run(task: str, outcome: str, actions, errors, status: str):
    try:
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "task": task,
            "status": status,
            "outcome_preview": (outcome or "")[:600],
            "outcome_length": len(outcome or ""),
            "actions": actions,
            "errors": errors,
        }
        fname = LOG_DIR / f"run_{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"
        fname.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass

async def run_agent(user_input: str, stream_output=True) -> str:
    controller = AsyncBrowserController()
    await controller.start()
    action_records: List[Dict[str, Any]] = []
    errors_for_ace: List[str] = []

    # Stealth User-Agent spoofing
    await controller.page.set_extra_http_headers({
        "User-Agent": random.choice(USER_AGENTS)
    })

    messages = [{"role": "system", "content": prompt_data["system_instructions"]}]

    # Memory injection
    if st.session_state.get("last_summary"):
        messages.append({"role": "assistant", "content": f"The last thing you summarized was:\n\n{st.session_state['last_summary']}"})
    if st.session_state.get("last_query"):
        messages.append({"role": "user", "content": f"The last query was: {st.session_state['last_query']}"})

    # Current prompt
    messages.append({"role": "user", "content": user_input})

    if "tool_descriptions" in prompt_data:
        descriptions = "\n".join(f"- {k}: {v}" for k, v in prompt_data["tool_descriptions"].items())
        messages.append({"role": "system", "content": f"Tools:\n{descriptions}"})
    if "guidelines" in prompt_data:
        messages.append({"role": "system", "content": "Guidelines:\n" + "\n".join(prompt_data["guidelines"])})
    overlay_text, used_tip_ids = ace_manager.prompt_overlay(user_input, domain=ACE_DOMAIN)
    if overlay_text:
        messages.append({"role": "system", "content": overlay_text})

    try:
        response = client.chat.completions.create(model="gpt-5.1", messages=messages)
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[-1].strip()

        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "actions" in parsed:
            plan = parsed.get("plan", "")
            tool_calls = parsed["actions"]
            if stream_output and plan:
                st.markdown("### 🧠 Agent Plan")
                st.info(plan)
        else:
            tool_calls = parsed if isinstance(parsed, list) else [parsed]

        # Fallback for lazy GPT outputs
        if len(tool_calls) == 1 and tool_calls[0]["tool"] == "navigate":
            st.warning("⚠️ GPT only returned a navigation step. Adding wait/extract fallback.")
            tool_calls += [
                {"tool": "wait_for", "args": {"selector": "body"}},
                {"tool": "extract", "args": {"selector": "body"}}
            ]

    except Exception as e:
        st.error("⚠️ GPT returned invalid or malformed JSON.")
        st.text_area("🔎 Raw GPT response", value=response.choices[0].message.content, height=200)
        return f"❌ GPT error or invalid JSON:\n\n{e}"

    required_args = {
        "navigate": ["url"],
        "click": ["selector"],
        "click_text": ["text"],
        "type_text": ["selector", "text"],
        "extract": ["selector"],
        "screenshot": ["path"],
        "highlight": ["selector"],
        "wait_for": ["selector"]
    }

    summary = ""
    for call in tool_calls:
        tool = call.get("tool")
        args = call.get("args", {})
        if not tool or not isinstance(args, dict):
            st.warning(f"⚠️ Invalid tool call structure: {call}")
            continue
        missing = [arg for arg in required_args.get(tool, []) if arg not in args]
        if missing:
            st.warning(f"⚠️ `{tool}` is missing: {missing}")
            if tool == "navigate":
                args["url"] = args.get("url", "https://example.com")
            elif tool == "extract":
                args["selector"] = args.get("selector", "body")
            else:
                st.error(f"❌ Cannot safely default missing args for `{tool}`")
                continue

        start_ts = time.perf_counter()
        result = await controller.run_action(tool, args)
        latency_ms = (time.perf_counter() - start_ts) * 1000
        current_url = controller.page.url if controller.page else ""
        action_records.append(build_action_record(tool, args, result, latency_ms, current_url, retries=0))
        if result.startswith(("⚠️", "❌")):
            errors_for_ace.append(result)

        if tool == "extract" and ("could not" in result.lower() or "error" in result.lower()):
            st.info("🔁 Rechecking extract with fallback...")
            args = {"selector": "body"}
            start_ts = time.perf_counter()
            result = await controller.run_action("extract", args)
            latency_ms = (time.perf_counter() - start_ts) * 1000
            current_url = controller.page.url if controller.page else ""
            action_records.append(build_action_record("extract", args, result, latency_ms, current_url, retries=1))

        st.session_state["last_tool"] = tool
        st.session_state["last_args"] = args

        if tool == "extract" and "Oops!" not in result and "not exist" not in result:
            summary = result

        if stream_output:
            st.markdown(f"---\n🔧 **{tool.upper()}**")
            st.code(json.dumps(args, indent=2), language="json")
            if "timeout" in result.lower() or "error" in result.lower():
                st.error(result)
            else:
                st.success(result)

    if not summary:
        st.info("🤖 No extract step found. Auto-extracting from 'body'.")
        summary = await controller.run_action("extract", {"selector": "body"})
        if stream_output:
            st.markdown(f"**Auto extract →** `body`\n\n{summary}")

    await controller.stop()

    # Save session memory
    st.session_state["last_summary"] = summary
    st.session_state["last_query"] = user_input
    final_output = summary

    # Cleanup + Summarize
    if summary and all(x not in summary for x in ["Oops!", "not exist", "Timeout"]):
        st.download_button("📥 Download Extracted Text (.txt)", summary, file_name="extracted.txt")
        md = f"## User Query\n{user_input}\n\n## Extracted Content\n{summary}"
        st.download_button("📥 Download as Markdown", md, file_name="summary.md")

        try:
            clean_response = client.chat.completions.create(
                model="gpt-5.1",
                messages=[
                    {"role": "system", "content": "You are cleaning raw HTML or scraped page text to extract just the meaningful content (e.g., job titles, product descriptions, listings)."},
                    {"role": "user", "content": summary}
                ]
            )
            cleaned_summary = clean_response.choices[0].message.content.strip()
            st.session_state["cleaned_summary"] = cleaned_summary

            st.markdown("---")
            st.markdown("#### 🧠 GPT Summary (based on scrape + original question)")
            followup = client.chat.completions.create(
                model="gpt-5.1",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant summarizing scraped web content."},
                    {"role": "user", "content": f"Here is what the user asked: {user_input}"},
                    {"role": "assistant", "content": cleaned_summary}
                ]
            )
            final_summary = followup.choices[0].message.content.strip()
            st.markdown(final_summary)
            final_output = final_summary

        except Exception as e:
            st.error(f"⚠️ GPT failed during summarization: {e}")
            final_output = summary

    else:
        st.warning("⚠️ Skipping summarization due to failed or invalid extract.")
        final_output = summary

    goal_data = infer_goal(final_output, action_records, errors_for_ace)
    ace_manager.record_run(
        task=user_input,
        outcome=final_output or "",
        actions=action_records,
        errors=errors_for_ace,
        preferences=st.session_state.get("ace_preferences", []),
        goal_status=goal_data.get("goal_status"),
        reason_for_status=goal_data.get("reason_for_status"),
        answer_relevance_score=None,
        used_tip_ids=used_tip_ids,
        domain=ACE_DOMAIN,
    )
    log_run(user_input, final_output, action_records, errors_for_ace, goal_data.get("goal_status", "partial"))
    return final_output


# --- Streamlit UI ---
st.set_page_config(page_title="Async Browser Agent", layout="wide")
st.title("🧠 GPT Browser Agent (Async)")
st.caption("Natural language → browser automation using GPT-4.1 + Playwright (async)")

if "action_log" not in st.session_state:
    st.session_state.action_log = []

if "user_input" not in st.session_state:
    st.session_state.user_input = ""
if "ace_preferences" not in st.session_state:
    st.session_state.ace_preferences = []

# Restart any monitors that were marked active.
resume_active_monitors()

with st.sidebar:
    st.subheader("ACE self-learning")
    pref_text = st.text_area(
        "Preferences to remember",
        value="\n".join(st.session_state.get("ace_preferences", [])),
        placeholder="e.g., Prefer desktop views; summarize bullet-first; avoid pop-ups",
        help="Stored in playbook.json (guardrails applied). Avoid secrets.",
    )
    st.session_state.ace_preferences = [p.strip() for p in pref_text.splitlines() if p.strip()]
    sidebar_overlay, _ = ace_manager.prompt_overlay(st.session_state.get("user_input", ""), domain=ACE_DOMAIN)
    if sidebar_overlay:
        st.markdown("**Active tips**")
        st.code(sidebar_overlay)

col1, col2 = st.columns([4, 1])
with col1:
    user_input = st.text_input("What would you like to do?", value=st.session_state.get("user_input", ""), placeholder="e.g., Find GTM jobs on Atlassian site")
with col2:
    run = st.button("🚀 Run Agent")

if run and user_input:
    st.markdown("### 🤖 GPT + Playwright Output")
    st.session_state.user_input = user_input  # Save it again just in case
    asyncio.run(run_agent(user_input))
    st.session_state.action_log.append(f"🗣️ {user_input}")

# 🔁 Follow-up input (based on last scrape/summary)
if st.session_state.get("last_summary") and st.session_state.get("last_query"):
    follow_up = st.text_input("💬 Ask a follow-up based on the last scrape:")
    if follow_up:
        st.markdown("### 💬 Follow-up Answer")
        try:
            follow_response = client.chat.completions.create(
                model="gpt-5.1",
                messages=[
                    {"role": "system", "content": "You are continuing a conversation based on a previous scrape."},
                    {"role": "user", "content": st.session_state["last_query"]},
                    {"role": "assistant", "content": st.session_state["last_summary"]},
                    {"role": "user", "content": follow_up}
                ]
            )
            st.markdown(follow_response.choices[0].message.content)
        except Exception as e:
            st.error(f"⚠️ GPT follow-up failed: {e}")

st.markdown("---")
st.markdown("### 📡 Background Monitors")

with st.form("create_monitor"):
    st.markdown("Create a monitor to run on a schedule and notify on changes or keyword matches.")
    mon_name = st.text_input("Monitor name", placeholder="e.g., Black Friday deals")
    mon_prompt = st.text_area("Prompt to run", placeholder="What changed on the Notion pricing page?")
    mon_urls = st.text_area("Target URLs (optional, one per line)", placeholder="https://example.com")

    sched_type = st.selectbox("Schedule type", ["interval", "daily", "weekly", "monthly"], index=0)
    interval_minutes = 300
    daily_time_val = dtime(hour=9, minute=0)
    weekly_day_val = 0
    monthly_day_val = 1

    if sched_type == "interval":
        interval_minutes = int(st.number_input("Every N minutes", min_value=5, max_value=1440, value=300, step=5))
    elif sched_type == "daily":
        daily_time_val = st.time_input("Run at (local time)", value=dtime(hour=9, minute=0))
    elif sched_type == "weekly":
        weekly_day_val = st.selectbox("Day of week", list(range(7)), index=0, format_func=lambda i: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i])
        daily_time_val = st.time_input("Run at (local time)", value=dtime(hour=9, minute=0))
    elif sched_type == "monthly":
        monthly_day_val = int(st.number_input("Day of month", min_value=1, max_value=31, value=1, step=1))
        daily_time_val = st.time_input("Run at (local time)", value=dtime(hour=9, minute=0))

    notify_mode = st.selectbox("Notify mode", ["on_change", "on_keyword", "always", "none"], index=0)
    keyword_filter = st.text_input("Keyword filter (optional)", placeholder="e.g., discount, senior engineer")
    start_now = st.checkbox("Start immediately", value=True)

    submitted = st.form_submit_button("Create monitor")
    if submitted:
        if not mon_name or not mon_prompt:
            st.warning("Name and prompt are required.")
        else:
            url_list = [u.strip() for u in mon_urls.splitlines() if u.strip()]
            monitor_id = monitor_db.create_monitor(
                name=mon_name.strip(),
                prompt=mon_prompt.strip(),
                urls=url_list,
                schedule_type=sched_type,
                interval_minutes=interval_minutes,
                daily_time=daily_time_val.strftime("%H:%M") if daily_time_val else None,
                weekly_day=weekly_day_val if sched_type == "weekly" else None,
                monthly_day=monthly_day_val if sched_type == "monthly" else None,
                notify_mode=notify_mode,
                keyword_filter=keyword_filter.strip() or None,
                is_active=1 if start_now else 0,
            )
            if start_now:
                start_monitor_runner(monitor_id)
            st.success(f"Monitor '{mon_name}' created{' and started' if start_now else ''}.")


def schedule_label(m: Dict[str, Any]) -> str:
    sched = (m.get("schedule_type") or "interval").lower()
    if sched == "interval":
        return f"Every {m.get('interval_minutes', 300)} min"
    if sched == "daily":
        return f"Daily @ {m.get('daily_time', '09:00')}"
    if sched == "weekly":
        weekday = int(m.get("weekly_day") or 0)
        return f"Weekly {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][weekday]} @ {m.get('daily_time', '09:00')}"
    if sched == "monthly":
        return f"Monthly day {m.get('monthly_day', 1)} @ {m.get('daily_time', '09:00')}"
    return "Interval"


monitors = monitor_db.list_monitors()
if not monitors:
    st.info("No monitors yet. Create one above to start scheduled checks.")
else:
    st.markdown("#### Active & Saved Monitors")
    for m in monitors:
        last_run = monitor_db.get_last_run(m["id"])
        running_entry = monitor_threads.get(m["id"])
        running = bool(running_entry and running_entry["thread"].is_alive())
        status_text = "Never run"
        if last_run:
            status_text = f"{last_run.get('status', 'unknown')} @ {last_run.get('finished_at', '')}"

        next_time = datetime.now() + timedelta(seconds=seconds_until_next_run(m))
        next_text = next_time.strftime("%Y-%m-%d %H:%M")

        col1, col2, col3, col4, col5, col6 = st.columns([2, 2, 2, 1.5, 1.5, 1.5])
        with col1:
            st.markdown(f"**{m.get('name')}**")
            st.caption(schedule_label(m))
        with col2:
            st.markdown(f"Status: {status_text}")
        with col3:
            st.markdown(f"Next run: {next_text}")
        with col4:
            if running:
                st.success("Running")
            elif m.get("is_active"):
                st.warning("Starting...")
            else:
                st.info("Stopped")
        with col5:
            start_btn = st.button("Start", key=f"start_{m['id']}", disabled=running)
            pause_btn = st.button("Pause", key=f"pause_{m['id']}", disabled=not (running or m.get("is_active")))
            run_now_btn = st.button("Run now", key=f"run_now_{m['id']}")
        with col6:
            delete_btn = st.button("Delete", key=f"delete_{m['id']}")

        if start_btn:
            start_monitor_runner(m["id"])
            st.rerun()
        if pause_btn:
            stop_monitor_runner(m["id"])
            st.rerun()
        if run_now_btn:
            fresh = monitor_db.get_monitor(m["id"])
            if fresh:
                asyncio.run(execute_monitor_run(fresh))
                st.success("Run completed.")
            else:
                st.warning("Monitor not found.")
        if delete_btn:
            delete_monitor(m["id"])
            st.rerun()

# 📝 Show history
if st.session_state.action_log:
    with st.expander("📝 Session Log", expanded=False):
        for entry in reversed(st.session_state.action_log[-10:]):
            st.markdown(entry)
