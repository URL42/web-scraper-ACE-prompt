import os
import json
from openai import OpenAI
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from ace import ace_manager

ACE_DOMAIN = "simple_agent"
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def scrape_page(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=250, devtools=True)
        page = browser.new_page()
        page.goto(url, timeout=15000)
        text = page.inner_text("body")[:5000]
        browser.close()
        return text

tools = [
    {
        "type": "function",
        "function": {
            "name": "scrape_page",
            "description": "Scrapes visible text from a webpage",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch"}
                },
                "required": ["url"]
            }
        }
    }
]

while True:
    user_input = input("\nYou: ")
    if user_input.lower() in ["exit", "quit"]:
        break

    # Phase 1: GPT decides if it wants to call scrape_page
    overlay_text, used_tip_ids = ace_manager.prompt_overlay(user_input, domain=ACE_DOMAIN)
    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {
                "role": "system",
                "content": """
You are a research assistant with the ability to scrape web pages.

Instructions:
- If a user question suggests looking up live data or job listings, call the `scrape_page` tool with the most relevant URL.
- You must use a full https:// URL if calling the tool.
"""
            },
            {"role": "user", "content": user_input},
            *([{"role": "system", "content": overlay_text}] if overlay_text else [])
        ],
        tools=tools,
        tool_choice="auto"
    )

    msg = response.choices[0].message
    actions_for_ace = []
    errors_for_ace = []
    final_output = ""

    if msg.tool_calls:
        tool_call = msg.tool_calls[0]
        args = json.loads(tool_call.function.arguments)
        url = args.get("url", "https://example.com")
        print(f"[🔍] GPT chose to scrape: {url}")

        try:
            scraped = scrape_page(url)
            actions_for_ace.append(f"scrape_page: {url}")
        except Exception as e:
            print("[❌] Scrape failed:", str(e))
            errors_for_ace.append(str(e))
            ace_manager.record_run(
                task=user_input,
                outcome="Scrape failed",
                actions=actions_for_ace,
                errors=errors_for_ace,
                preferences=[],
                goal_status="failed",
                reason_for_status="scrape_failed",
                answer_relevance_score=None,
                used_tip_ids=used_tip_ids,
                domain=ACE_DOMAIN,
            )
            continue

        # Phase 2: Send scraped content back to GPT
        overlay_text, used_tip_ids = ace_manager.prompt_overlay(user_input, domain=ACE_DOMAIN)
        final_response = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant who analyzes scraped website data to answer questions."
                },
                {"role": "user", "content": user_input},
                msg,
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": scraped
                },
                *([{"role": "system", "content": overlay_text}] if overlay_text else [])
            ]
        )

        final_output = final_response.choices[0].message.content
        print("\nAgent:", final_output)
    else:
        final_output = msg.content
        print("\nAgent:", final_output)

    goal_status = "success" if final_output and not errors_for_ace else ("partial" if final_output else "failed")
    reason = ""
    joined_err = " ".join(errors_for_ace).lower() if errors_for_ace else ""
    if "captcha" in joined_err:
        goal_status = "blocked"; reason = "captcha_block"
    elif "login" in joined_err:
        goal_status = "blocked"; reason = "login_required"
    elif "timeout" in joined_err:
        reason = "timeout"
    elif errors_for_ace:
        reason = "selector_fail"
    ace_manager.record_run(
        task=user_input,
        outcome=final_output or "",
        actions=actions_for_ace,
        errors=errors_for_ace,
        preferences=[],
        goal_status=goal_status,
        reason_for_status=reason,
        answer_relevance_score=None,
        used_tip_ids=used_tip_ids,
        domain=ACE_DOMAIN,
    )
