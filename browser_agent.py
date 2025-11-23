import os
import json
import time
import random
import asyncio
import threading
import streamlit as st
import speech_recognition as sr
from typing import Dict, List
from pathlib import Path
from urllib.parse import urlparse
from openai import OpenAI
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from ace import ace_manager

# --- Config ---
PERSISTENT_PROFILE_ENABLED = True
STEALTH_MODE_ENABLED = True
PROFILE_PATH = Path("user_profiles/default")

# --- Setup ---
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
GUIDELINES_PATH = "prompt_guidelines.json"
Path("outputs").mkdir(exist_ok=True)
PROFILE_PATH.mkdir(parents=True, exist_ok=True)

with open(GUIDELINES_PATH, "r", encoding="utf-8") as f:
    prompt_data = json.load(f)

# --- Async Browser Controller ---
class AsyncBrowserController:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        self.playwright = await async_playwright().start()
        if PERSISTENT_PROFILE_ENABLED:
            self.browser = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_PATH),
                headless=False,
                viewport={"width": 1280, "height": 800},
                locale="en-US"
            )
            self.page = self.browser.pages[0] if self.browser.pages else await self.browser.new_page()
        else:
            self.browser = await self.playwright.chromium.launch(headless=False, slow_mo=200)
            self.context = await self.browser.new_context(viewport={"width": 1280, "height": 800})
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
        await asyncio.sleep(random.uniform(2.5, 4.5))
        await self.page.mouse.wheel(0, 600)
        await self.page.mouse.move(100, 300)
        await asyncio.sleep(random.uniform(0.5, 1.0))

    async def run_action(self, tool: str, args: Dict[str, str]) -> str:
        try:
            if tool == "navigate":
                await self.page.goto(args["url"], wait_until='domcontentloaded', timeout=120000)
                await asyncio.sleep(3)
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
                    return f"🖱️ Clicked {args['selector']}"
                except Exception as e:
                    try:
                        await self.page.hover(args["selector"])
                        await asyncio.sleep(1)
                        await self.page.click(args["selector"], timeout=3000)
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
                    return f"⚠️ Could not extract from {args['selector']} → {e}"

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

# --- GPT + Tool Execution + Summary ---
async def run_agent(user_input: str, stream_output=True) -> str:
    controller = AsyncBrowserController()
    await controller.start()
    actions_for_ace: List[str] = []
    errors_for_ace: List[str] = []

    # Stealth User-Agent spoofing
    await controller.page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/113.0.0.0 Safari/537.36"
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
    overlay = ace_manager.prompt_overlay(user_input)
    if overlay:
        messages.append({"role": "system", "content": overlay})

    try:
        response = client.chat.completions.create(model="gpt-4.1", messages=messages)
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

        result = await controller.run_action(tool, args)
        actions_for_ace.append(f"{tool}: {result}")
        if result.startswith(("⚠️", "❌")):
            errors_for_ace.append(result)

        if tool == "extract" and ("could not" in result.lower() or "error" in result.lower()):
            st.info("🔁 Rechecking extract with fallback...")
            args = {"selector": "body"}
            result = await controller.run_action("extract", args)

        st.session_state["last_tool"] = tool
        st.session_state["last_args"] = args

        if tool == "extract" and "Oops!" not in result and "not exist" not in result:
            summary = result

        # Streaming log
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
                model="gpt-4.1",
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
                model="gpt-4.1",
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

    ace_manager.record_run(
        task=user_input,
        outcome=final_output or "",
        actions=actions_for_ace,
        errors=errors_for_ace,
        preferences=st.session_state.get("ace_preferences", []),
    )
    return final_output


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

# --- Background Monitor ---
def start_monitoring(prompt: str):
    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(monitor_loop(prompt))
    threading.Thread(target=run_loop, daemon=True).start()

async def monitor_loop(prompt: str):
    while True:
        await run_agent(prompt, stream_output=False)
        await asyncio.sleep(300)

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

with st.sidebar:
    st.subheader("ACE self-learning")
    pref_text = st.text_area(
        "Preferences to remember",
        value="\n".join(st.session_state.get("ace_preferences", [])),
        placeholder="e.g., Prefer desktop views; summarize bullet-first; avoid pop-ups",
        help="Stored in playbook.json (guardrails applied). Avoid secrets.",
    )
    st.session_state.ace_preferences = [p.strip() for p in pref_text.splitlines() if p.strip()]
    sidebar_overlay = ace_manager.prompt_overlay(st.session_state.get("user_input", ""))
    if sidebar_overlay:
        st.markdown("**Active tips**")
        st.code(sidebar_overlay)

col1, col2 = st.columns([4, 1])
with col1:
    user_input = st.text_input("What would you like to do?", value=st.session_state.get("user_input", ""), placeholder="e.g., Find GTM jobs on Atlassian site")
with col2:
    use_voice = st.button("🎙️ Speak")

if use_voice:
    user_input = recognize_voice()
    st.session_state["user_input"] = user_input
    st.success(f"🔈 You said: {user_input}")

# 🚀 Run agent
if st.button("🚀 Run Agent") and user_input:
    st.session_state["user_input"] = user_input
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
                model="gpt-4.1",
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

# 📡 Monitor button
if st.button("📡 Start Monitor (5 min loop)"):
    start_monitoring(st.session_state["user_input"])
    st.success("✅ Background monitor started!")

# 📝 Show history
if st.session_state.action_log:
    with st.expander("📝 Session Log", expanded=False):
        for entry in reversed(st.session_state.action_log[-10:]):
            st.markdown(entry)
