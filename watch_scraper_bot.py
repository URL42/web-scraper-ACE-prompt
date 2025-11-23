# watch_scraper_bot.py

import os
import json
import time
from pathlib import Path
from datetime import datetime
import requests
from openai import OpenAI
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from ace import ace_manager

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
USER_PREFERENCES = [p.strip() for p in os.getenv("ACE_PREFERENCES", "").split(",") if p.strip()]

QUERY = "What's the latest GTM manager or director job posting on LinkedIn?"
INTERVAL_MINUTES = 30  # how often to check
PREVIOUS_SCRAPE = Path("scrapes/last_watch_scrape.txt")
Path("scrapes").mkdir(exist_ok=True)

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],  # Telegram max length
        "parse_mode": "Markdown"
    }
    r = requests.post(url, data=data)
    if r.ok:
        print("[📨] Telegram sent.")
    else:
        print("[⚠️] Telegram failed:", r.text)

def scrape_page(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=15000)
        text = page.inner_text("body")[:5000]
        browser.close()
    return text

def resolve_url_with_gpt(query: str) -> str:
    overlay = ace_manager.prompt_overlay(query)
    resp = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": "You are a web assistant. Return the best URL to answer this question."},
            {"role": "user", "content": query},
            *([{"role": "system", "content": overlay}] if overlay else [])
        ]
    )
    guess = resp.choices[0].message.content.strip()
    return guess if guess.startswith("http") else "https://example.com"

def check_for_update():
    url = resolve_url_with_gpt(QUERY)
    print(f"[🔗] Checking URL: {url}")
    new_text = scrape_page(url)

    if PREVIOUS_SCRAPE.exists():
        with open(PREVIOUS_SCRAPE, "r", encoding="utf-8") as f:
            prev = f.read()
        if new_text == prev:
            print(f"[⏱] No change at {datetime.now()}")
            return  # no update

    # Save updated text
    with open(PREVIOUS_SCRAPE, "w", encoding="utf-8") as f:
        f.write(new_text)

    # Generate GPT summary
    reply = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": "Summarize this new page content for a Telegram alert."},
            {"role": "user", "content": f"User is monitoring: {QUERY}"},
            {"role": "assistant", "content": f"The content was updated. Here is the new version:\n{new_text}"}
        ]
    )
    summary = reply.choices[0].message.content
    send_telegram_message(f"*🔔 Watch Alert:*\n{summary}")
    ace_manager.record_run(
        task=query,
        outcome=summary,
        actions=[f"scraped {url}"],
        errors=[],
        preferences=USER_PREFERENCES,
    )

if __name__ == "__main__":
    while True:
        try:
            check_for_update()
        except Exception as e:
            print("[❌] Watcher error:", str(e))
        time.sleep(INTERVAL_MINUTES * 60)
