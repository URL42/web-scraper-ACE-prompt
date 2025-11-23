import os
import json
import time
import random
import threading
import streamlit as st
import speech_recognition as sr
from typing import Dict, List
from pathlib import Path
from urllib.parse import urlparse
from openai import OpenAI
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from ace import ace_manager

# --- Setup ---
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
GUIDELINES_PATH = "prompt_guidelines.json"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
]
Path("outputs").mkdir(exist_ok=True)

with open(GUIDELINES_PATH, "r", encoding="utf-8") as f:
    prompt_data = json.load(f)

# --- Browser Controller ---
class BrowserController:
    def __init__(self):
        self.playwright = sync_playwright().start()
        viewport = random.choice([
            {"width": 1280, "height": 800},
            {"width": 1366, "height": 768},
            {"width": 1440, "height": 900}
        ])
        ua = random.choice(USER_AGENTS)
        self.browser = self.playwright.chromium.launch(headless=False, slow_mo=200)
        self.context = self.browser.new_context(viewport=viewport, user_agent=ua)
        self.page = self.context.new_page()

    def run_action(self, tool: str, args: Dict[str, str]) -> str:
        try:
            if tool == "navigate":
                self.page.goto(args["url"], wait_until="networkidle", timeout=45000)
                self.simulate_human()
                self.try_autologin(args["url"])
                return f"✅ Navigated to {args['url']}"
            elif tool == "click":
                self.page.click(args["selector"])
                return f"🖱️ Clicked {args['selector']}"
            elif tool == "click_text":
                text = args["text"].lower()
                elements = self.page.query_selector_all("*")
                for el in elements:
                    try:
                        content = el.inner_text().strip().lower()
                        if text in content:
                            el.click()
                            return f"🖱️ Clicked element containing text: '{args['text']}'"
                    except:
                        continue
                return f"⚠️ Could not find element with text: '{args['text']}'"
            elif tool == "type_text":
                self.page.fill(args["selector"], args["text"])
                return f"⌨️ Typed '{args['text']}' into {args['selector']}"
            elif tool == "extract":
                try:
                    content = self.page.inner_text(args["selector"])
                    return content[:2000]
                except Exception as e:
                    try:
                        html = self.page.content()
                        return f"Fallback DOM extract (body): {html[:1500]}"
                    except Exception as inner_e:
                        return f"⚠️ Could not extract from {args['selector']} → {e} / fallback failed: {inner_e}"
            elif tool == "highlight":
                self.page.eval_on_selector(args["selector"], "el => el.style.outline = '3px solid red'")
                return f"🔍 Highlighted {args['selector']}"
            elif tool == "scroll":
                self.page.mouse.wheel(0, 1000)
                return "🌀 Scrolled down"
            elif tool == "wait_for":
                self.page.wait_for_selector(args["selector"])
                return f"⏳ Waited for {args['selector']}"
            else:
                return f"❓ Unknown tool: {tool}"
        except PlaywrightTimeout:
            return f"⚠️ Timeout during {tool}"
        except Exception as e:
            return f"❌ Error during {tool}: {e}"

    def simulate_human(self):
        time.sleep(random.uniform(2.5, 4.5))
        self.page.mouse.wheel(0, 600)
        self.page.mouse.move(100, 300)
        time.sleep(random.uniform(0.5, 1.0))

    def try_autologin(self, url: str):
        domain = urlparse(url).netloc.split('.')[-2].upper()
        username = os.getenv(f"{domain}_USERNAME")
        password = os.getenv(f"{domain}_PASSWORD")
        if not (username and password):
            return
        try:
            email_fields = self.page.query_selector_all('input[type="email"], input[name*="user"], input[name*="email"]')
            pass_fields = self.page.query_selector_all('input[type="password"]')
            if email_fields and pass_fields:
                email_fields[0].fill(username)
                pass_fields[0].fill(password)
                self.page.keyboard.press("Enter")
                print(f"🔐 Auto-login attempted for {domain}")
        except Exception as e:
            print(f"Login check failed: {e}")

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass

# --- Autonomous Monitor ---
def monitor_page(prompt: str, interval_sec=300):
    while True:
        result = run_agent(prompt, stream_output=False)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        with open(f"outputs/monitor_{timestamp}.txt", "w", encoding="utf-8") as f:
            f.write(result or "No result")
        time.sleep(interval_sec)

# --- GPT Agent ---
def run_agent(user_input: str, stream_output=True) -> str:
    controller = BrowserController()
    actions_for_ace: List[str] = []
    errors_for_ace: List[str] = []
    messages = [
        {"role": "system", "content": prompt_data["system_instructions"]},
        {"role": "user", "content": user_input}
    ]
    if "tool_descriptions" in prompt_data:
        descriptions = "\n".join(f"- {k}: {v}" for k, v in prompt_data["tool_descriptions"].items())
        messages.append({"role": "system", "content": f"Tools:\n{descriptions}"})
    if "guidelines" in prompt_data:
        messages.append({"role": "system", "content": "Guidelines:\n" + "\n".join(prompt_data["guidelines"])})
    overlay = ace_manager.prompt_overlay(user_input)
    if overlay:
        messages.append({"role": "system", "content": overlay})

    try:
        response = client.chat.completions.create(model="gpt-4.1", messages=messages)
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[-1].strip()
        tool_calls = json.loads(raw)
        if isinstance(tool_calls, dict):
            tool_calls = [tool_calls]
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
        "highlight": ["selector"],
        "wait_for": ["selector"]
    }

    log = []
    summary = ""

    for call in tool_calls:
        tool = call.get("tool")
        args = call.get("args", {})
        if not tool or not isinstance(args, dict):
            st.warning(f"⚠️ Invalid tool call structure: {call}")
            continue
        missing = [arg for arg in required_args.get(tool, []) if arg not in args]
        if missing:
            st.error(f"❌ Missing argument(s) for `{tool}`: {missing}")
            continue
        result = controller.run_action(tool, args)
        actions_for_ace.append(f"{tool}: {result}")
        if result.startswith(("⚠️", "❌")):
            errors_for_ace.append(result)
        log.append(f"🔧 `{tool}` → {args} →\n{result}")
        if stream_output:
            st.markdown(f"**{tool} →** `{json.dumps(args)}`\n\n{result}")
        if tool == "extract":
            summary = result

    if summary:
        st.download_button("📥 Download Extracted Text (.txt)", summary, file_name="extracted.txt")
        md = f"## User Query\n{user_input}\n\n## Extracted Content\n{summary}"
        st.download_button("📥 Download as Markdown", md, file_name="summary.md")
    ace_manager.record_run(
        task=user_input,
        outcome=summary or "",
        actions=actions_for_ace,
        errors=errors_for_ace,
        preferences=st.session_state.get("ace_preferences", []),
    )
    controller.close()
    return summary

# --- Voice Input ---
def recognize_voice() -> str:
    r = sr.Recognizer()
    with sr.Microphone() as source:
        st.info("🎤 Listening...")
        audio = r.listen(source, timeout=5, phrase_time_limit=8)
    try:
        return r.recognize_google(audio)
    except sr.UnknownValueError:
        return "Sorry, could not understand you."
    except sr.RequestError as e:
        return f"Voice error: {e}"

# --- Streamlit UI ---
st.set_page_config(page_title="Browser Agent", layout="wide")
st.title("🧠 GPT Browser Agent")
st.caption("Natural language → browser automation using GPT-4.1 + Playwright")

if "action_log" not in st.session_state:
    st.session_state.action_log = []
if "ace_preferences" not in st.session_state:
    st.session_state.ace_preferences = []

with st.sidebar:
    st.subheader("ACE self-learning")
    pref_text = st.text_area(
        "Preferences to remember",
        value="\n".join(st.session_state.get("ace_preferences", [])),
        placeholder="e.g., Prefer desktop views; summarize bullet-first; avoid pop-ups",
        help="Stored in playbook.json (guardrails applied). Avoid secrets.",
    )
    st.session_state.ace_preferences = [p.strip() for p in pref_text.splitlines() if p.strip()]
    overlay_sidebar = ace_manager.prompt_overlay(st.session_state.get("user_input", ""))
    if overlay_sidebar:
        st.markdown("**Active tips**")
        st.code(overlay_sidebar)

col1, col2 = st.columns([4, 1])
with col1:
    user_input = st.text_input("Enter a task or question:", placeholder="e.g., What's Notion's pricing?")
with col2:
    use_voice = st.button("🎙️ Speak")

if use_voice:
    user_input = recognize_voice()
    st.success(f"🔈 You said: {user_input}")

run = st.button("🚀 Run Agent")
if run and user_input:
    st.markdown("### 🤖 GPT + Playwright Output")
    result = run_agent(user_input)
    st.session_state.action_log.append(f"🗣️ {user_input}\n\n{result}")

if st.button("📡 Start Monitor (5min loop)"):
    threading.Thread(target=monitor_page, args=(user_input,), daemon=True).start()
    st.success("Started background monitor!")

if st.session_state.action_log:
    with st.expander("📝 Session Log", expanded=False):
        for entry in reversed(st.session_state.action_log[-10:]):
            st.markdown(entry)
