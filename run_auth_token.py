#!/usr/bin/env python3
"""Run the browser agent with token-based authentication (cookie or header)."""

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
    target_url = input("Enter the target URL:\n> ").strip()
    if not target_url:
        print("No URL provided. Exiting.")
        return

    token = input("Enter the auth token: ").strip()
    if not token:
        print("No token provided. Exiting.")
        return

    print("\nHow should the token be injected?")
    print("  1) As a cookie (default)")
    print("  2) As an Authorization header")
    choice = input("Choice [1/2]: ").strip() or "1"

    # Extract domain from URL for cookie
    from urllib.parse import urlparse
    parsed = urlparse(target_url)
    domain = parsed.hostname

    print(f"\nLaunching browser and navigating to {target_url} ...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()

        if choice == "1":
            # Inject token as a cookie
            cookie_name = input("Cookie name [default: session]: ").strip() or "session"
            context.add_cookies([{
                "name": cookie_name,
                "value": token,
                "domain": domain,
                "path": "/",
            }])
            print(f"Injected cookie '{cookie_name}' for domain {domain}")
        else:
            # Inject token as Authorization header
            context.set_extra_http_headers({
                "Authorization": f"Bearer {token}",
            })
            print("Injected Authorization header")

        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

        task = input("\nEnter the task / instruction for the agent:\n> ").strip()
        if not task:
            print("No task provided. Exiting.")
            browser.close()
            return

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
