#!/usr/bin/env python3
"""Run the browser agent without authentication — for public sites."""

import json
import requests
from playwright.sync_api import sync_playwright

API_BASE = "http://localhost:8000/v1"


def send_prompt(prompt: str, page_content: str) -> str:
    """Send the task prompt along with current page context to the model."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a browser automation agent. You are given a task and the "
                "current page content. Respond with a JSON action to perform.\n"
                "Supported actions:\n"
                '  {"action": "goto", "url": "<url>"}\n'
                '  {"action": "click", "selector": "<css selector>"}\n'
                '  {"action": "fill", "selector": "<css selector>", "value": "<text>"}\n'
                '  {"action": "scroll", "direction": "up|down"}\n'
                '  {"action": "done", "result": "<summary>"}\n'
            ),
        },
        {
            "role": "user",
            "content": f"Task: {prompt}\n\nCurrent page content:\n{page_content[:4000]}",
        },
    ]
    resp = requests.post(
        f"{API_BASE}/chat/completions",
        json={"model": "default", "messages": messages, "max_tokens": 512},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def execute_action(page, action: dict) -> bool:
    """Execute a browser action. Returns False when the agent is done."""
    act = action.get("action")
    if act == "goto":
        page.goto(action["url"], wait_until="domcontentloaded")
    elif act == "click":
        page.click(action["selector"])
    elif act == "fill":
        page.fill(action["selector"], action["value"])
    elif act == "scroll":
        direction = action.get("direction", "down")
        page.evaluate(f"window.scrollBy(0, {'300' if direction == 'down' else '-300'})")
    elif act == "done":
        print(f"\nAgent finished: {action.get('result', '')}")
        return False
    else:
        print(f"Unknown action: {act}")
    return True


def main():
    task = input("Enter the task / instruction for the agent:\n> ").strip()
    if not task:
        print("No task provided. Exiting.")
        return

    start_url = input("Enter the starting URL [default: https://www.google.com]: ").strip()
    if not start_url:
        start_url = "https://www.google.com"

    print(f"\nLaunching browser and navigating to {start_url} ...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        for step in range(1, 21):
            content = page.content()
            print(f"\n--- Step {step} ---")
            response_text = send_prompt(task, content)
            print(f"Model response: {response_text}")

            try:
                action = json.loads(response_text)
            except json.JSONDecodeError:
                print("Could not parse model response as JSON. Stopping.")
                break

            if not execute_action(page, action):
                break

            page.wait_for_timeout(1000)

        browser.close()


if __name__ == "__main__":
    main()
