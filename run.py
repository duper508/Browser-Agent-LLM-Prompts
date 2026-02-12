#!/usr/bin/env python3
"""Unified browser agent runner with selectable authentication modes.

Uses the TIGER-AI-Lab BrowserAgent prompt format with accessibility tree
observations. Data is extracted generically from any website — tables are
tagged with a page-context label and screenshots are taken as fallback.

All parameters can be passed as CLI arguments (skipping interactive prompts)
or left unset to get prompted interactively.

Example usage:
  python run.py --auth 1 --url https://finance.yahoo.com --port 5001 \\
    --task "Search for RTX, go to Historical Data, extract the table"

Auth modes:
  1) No authentication       — public sites
  2) Username/password auth  — login form
  3) Token auth              — cookie or Bearer token
  4) Session takeover        — log in manually, agent takes over
"""

import argparse
import csv
import hashlib
import os
import re
import sys
import requests
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

API_BASE = None  # Set in main()
MODEL_NAME = None  # Auto-detected from server
MAX_STEPS = 50
MAX_CONTEXT_CHARS = 80000  # ~20K tokens; keeps prompt under 32K context with room for completion
MAX_TREE_LINES = 600  # Cap the accessibility tree to prevent huge pages from blowing context

SYSTEM_PROMPT = r"""You are a browser interaction assistant designed to execute step-by-step browser operations efficiently and precisely to complete the user's task. You are provided with specific tasks and webpage-related information, and you need to output accurate actions to accomplish the user's task.

Here's the information you'll have:
The user's objective: This is the task you're trying to complete.
The current web page's accessibility tree: This is a simplified representation of the webpage, providing key information.
The open tabs: These are the tabs you have open.
The previous actions: There are the actions you just performed. It may be helpful to track your progress.
Information already found: Information related to the current query that has been identified in historical actions. You need to integrate and supplement this information.

The actions you can perform fall into several categories:

Page Operation Actions:
`click [id] [content]`: This action clicks on an element with a specific id on the webpage.
`type [id] [content] [press_enter_after=0|1]`: Use this to type the content into the field with id. By default, the "Enter" key is pressed after typing unless press_enter_after is set to 0.
`hover [id] [content]`: Hover over an element with id.
`press [key_comb]`:  Simulates the pressing of a key combination on the keyboard (e.g., Ctrl+v).
`scroll [down|up]`: Scroll the page up or down.

Tab Management Actions:
`new_tab`: Open a new, empty browser tab.
`tab_focus [tab_index]`: Switch the browser's focus to a specific tab using its index.
`close_tab`: Close the currently active tab.

URL Navigation Actions:
`goto [url]`: Navigate to a specific URL.
`go_back`: Navigate to the previously viewed page.
`go_forward`: Navigate to the next page (if a previous 'go_back' action was performed).

Data Extraction Action:
`extract [label]`: Capture data from the current page immediately. The label is used to tag the rows in the output CSV (e.g. `extract [quarterly earnings]`). Use this when you see data worth saving.

Completion Action:
`stop [answer]`: Issue this action when you believe the task is complete. If the objective is to find a text-based answer, provide the answer in the bracket. If you believe the task is impossible to complete, provide the answer as "N/A" in the bracket.

To be successful, it is very important to follow the following rules:
1. You should only issue an action that is valid given the current observation.
2. You should only issue one action at a time.
3. You should follow the examples to reason step by step and then issue the next action.
4. You should refer to historical actions when issue an action and try not to make repetitive actions
5. All reasoning must be inside `<think></think>` tags, and there must be no output before `<think></think>`.
6. After `<think></think>`, only the action should be generated in the correct format, enclosed in code fences. For example:
   <think>This button looks relevant to my goal. Clicking it should take me to the next step.</think>
   ```click [id] [content]```
7. Issue the stop action when you think you have achieved the objective. Don't generate anything after stop.
8. Always format actions correctly:
```command [parameters]```
For example, if searching for "death row inmates in the US" in a search field with ID `21`, correctly format it as:
```type [21] [death row inmates in the US] [1]```
Avoid incorrect formats that omit brackets around parameters or numeric values.
9.Between <think></think>, you need to use <conclusion></conclusion> to enclose the information obtained in this round that is relevant to the current query. Note that if there is no valid information, this part is not required. The enclosed information must be directly usable to answer the original query."""

USER_PROMPT_TEMPLATE = """Objective: {objective}
Observation: {observation}
HISTORY_ACTION: {history_action}
HISTORY_info: {history_info}
"""


# Global mapping from tree observation ID -> backendDOMNodeId, rebuilt each step
obs_node_map = {}


# ---------------------------------------------------------------------------
# Accessibility tree
# ---------------------------------------------------------------------------

def get_accessibility_tree(page) -> str:
    """Get a simplified accessibility tree from the page via CDP.

    Also populates obs_node_map so we can resolve tree IDs to DOM elements later.
    """
    global obs_node_map
    obs_node_map = {}

    cdp = page.context.new_cdp_session(page)
    try:
        result = cdp.send("Accessibility.getFullAXTree")
        nodes = result.get("nodes", [])
    finally:
        cdp.detach()

    if not nodes:
        return "(empty page)"

    # Deduplicate nodes by nodeId
    seen = set()
    unique_nodes = []
    for n in nodes:
        if n["nodeId"] not in seen:
            unique_nodes.append(n)
            seen.add(n["nodeId"])
    nodes = unique_nodes

    node_map = {n["nodeId"]: n for n in nodes}
    lines = []
    counter = [0]
    _walk_cdp_tree(nodes[0], node_map, lines, depth=0, counter=counter)
    if len(lines) > MAX_TREE_LINES:
        truncated = len(lines) - MAX_TREE_LINES
        lines = lines[:MAX_TREE_LINES]
        lines.append(f"... ({truncated} more elements truncated)")
    print(f"  [Tree: {len(lines)} lines, {len(obs_node_map)} interactive elements]")
    return "\n".join(lines)


def _walk_cdp_tree(node, node_map, lines, depth, counter):
    """Recursively walk the CDP accessibility tree and format it with IDs."""
    role = _get_ax_value(node, "role")
    name = _get_ax_value(node, "name")

    # Determine if this is a valid node worth showing.
    # InlineTextBox and StaticText are never interactive — they just duplicate
    # parent content and confuse the model into targeting non-interactive IDs.
    skip_roles = {
        "none", "generic", "Ignored", "ignored",
        "InlineTextBox", "StaticText",
    }
    valid = role not in skip_roles or False  # skip_roles are always skipped

    # For non-skipped roles, still skip empty generic-like structural nodes
    if valid and not name.strip() and role in (
        "img", "list", "strong", "paragraph",
        "banner", "navigation", "Section", "LabelText", "Legend", "listitem",
    ):
        valid = False

    if valid:
        obs_id = counter[0]
        counter[0] += 1
        indent = "\t" * depth

        # Build the node string matching TIGER-AI-Lab format
        node_str = f"[{obs_id}] {role} {repr(name)}"

        # Add properties (focused, required, etc.)
        props = []
        for prop in node.get("properties", []):
            pname = prop.get("name", "")
            if pname in ("level", "setsize", "posinset", "disabled",
                         "focused", "required", "checked", "selected"):
                pval = prop.get("value", {})
                val = pval.get("value", "") if isinstance(pval, dict) else pval
                props.append(f"{pname}: {val}")
        if props:
            node_str += " " + " ".join(props)

        lines.append(f"{indent}{node_str}")

        # Store mapping for action execution
        if "backendDOMNodeId" in node:
            obs_node_map[obs_id] = {
                "backend_id": node["backendDOMNodeId"],
                "role": role,
                "name": name,
            }

    for child_id in node.get("childIds", []):
        child = node_map.get(child_id)
        if child:
            child_depth = depth + 1 if valid else depth
            _walk_cdp_tree(child, node_map, lines, child_depth, counter)


def _get_ax_value(node, field):
    """Extract a string value from an accessibility tree node field."""
    obj = node.get(field, {})
    if isinstance(obj, dict):
        return obj.get("value", "")
    return str(obj)


# ---------------------------------------------------------------------------
# LLM communication
# ---------------------------------------------------------------------------

def extract_command(text: str) -> str:
    """Extract the last command from code fences in the model response."""
    blocks = re.findall(r"```\s*([^\s].*?[^\s])\s*```", text, re.DOTALL)
    if not blocks:
        return ""
    return blocks[-1].strip().replace("```", "").strip()


def extract_conclusion(text: str) -> str:
    """Extract conclusion content from the model response."""
    blocks = re.findall(r"<conclusion>\s*(.*?)\s*</conclusion>", text, re.DOTALL)
    if not blocks:
        return ""
    return blocks[-1].strip()


def send_prompt(objective: str, observation: str, history_action: str, history_info: str) -> str:
    """Send the formatted prompt to the vLLM model and return the response.

    Tries /v1/chat/completions first (chat models). If the server returns 404,
    falls back to /v1/completions (base/fine-tuned models).
    """
    # Truncate to fit within model context window (~4 chars/token).
    # Budget: system prompt + objective are fixed; split remaining space
    # between observation (70%) and history (30%).
    fixed = len(SYSTEM_PROMPT) + len(objective) + 500
    budget = max(MAX_CONTEXT_CHARS - fixed, 4000)
    obs_budget = int(budget * 0.7)
    hist_budget = budget - obs_budget

    if len(observation) > obs_budget:
        observation = observation[:obs_budget] + "\n... (truncated)"
        print(f"  [Truncated observation to ~{obs_budget} chars]")
    combined_hist = len(history_action) + len(history_info)
    if combined_hist > hist_budget:
        half = hist_budget // 2
        if len(history_action) > half:
            history_action = history_action[-half:]
        if len(history_info) > half:
            history_info = history_info[-half:]
        print(f"  [Truncated history to ~{hist_budget} chars]")

    user_content = USER_PROMPT_TEMPLATE.format(
        objective=objective,
        observation=observation,
        history_action=history_action,
        history_info=history_info,
    )
    prompt = SYSTEM_PROMPT + "\n\n" + user_content

    # Try chat completions first, retry with halved prompt on 400 (context overflow)
    for attempt in range(3):
        resp = requests.post(
            f"{API_BASE}/chat/completions",
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 1024,
            },
            timeout=120,
        )

        if resp.status_code == 404:
            # Fall back to text completions endpoint
            resp = requests.post(
                f"{API_BASE}/completions",
                json={
                    "model": MODEL_NAME,
                    "prompt": prompt,
                    "temperature": 0,
                    "max_tokens": 1024,
                },
                timeout=120,
            )

        if resp.status_code == 400 and attempt < 2:
            # Context overflow — aggressively truncate and retry
            prompt = prompt[:len(prompt) // 2]
            print(f"  [400 error — retrying with truncated prompt ({len(prompt)} chars)]")
            continue

        break

    resp.raise_for_status()
    data = resp.json()

    # Parse response from whichever endpoint worked
    if "message" in data["choices"][0]:
        return data["choices"][0]["message"]["content"]
    return data["choices"][0]["text"]


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def execute_action(page, command: str, collected_data: list, seen_snapshots: set) -> bool:
    """Parse and execute a BrowserAgent command. Returns False on stop."""
    if not command:
        print("  No command extracted.")
        return True

    print(f"  Action: {command}")

    if command.startswith("stop"):
        answer = re.match(r"stop\s*\[(.+)\]", command, re.DOTALL)
        if answer:
            print(f"\n  Agent answer: {answer.group(1)}")
        return False

    if command.startswith("extract"):
        match = re.match(r"extract\s+\[(.+)\]", command)
        label = match.group(1).strip() if match else None
        try_extract_data(page, collected_data, seen_snapshots, label=label)
        return True

    if command.startswith("click"):
        match = re.match(r"click\s+\[(\d+)\]", command)
        if match:
            old_url = page.url
            _click_by_tree_id(page, int(match.group(1)))
            # Wait for potential navigation after click
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            if page.url != old_url:
                page.wait_for_timeout(1500)  # Extra settle time after navigation

    elif command.startswith("type"):
        match = re.match(r"type\s+\[(\d+)\]\s+\[(.+?)\]\s*(?:\[(\d)\])?", command)
        if match:
            node_id = int(match.group(1))
            content = match.group(2)
            press_enter = match.group(3) != "0" if match.group(3) else True
            _type_by_tree_id(page, node_id, content, press_enter)

    elif command.startswith("scroll"):
        match = re.match(r"scroll\s+\[(down|up)\]", command)
        direction = match.group(1) if match else "down"
        delta = 500 if direction == "down" else -500
        page.evaluate(f"window.scrollBy(0, {delta})")

    elif command.startswith("goto"):
        match = re.match(r"goto\s+\[(.+)\]", command)
        if match:
            page.goto(match.group(1), wait_until="domcontentloaded")

    elif command.startswith("go_back"):
        page.go_back(wait_until="domcontentloaded")

    elif command.startswith("go_forward"):
        page.go_forward(wait_until="domcontentloaded")

    elif command.startswith("hover"):
        match = re.match(r"hover\s+\[(\d+)\]", command)
        if match:
            _hover_by_tree_id(page, int(match.group(1)))

    elif command.startswith("press"):
        match = re.match(r"press\s+\[(.+)\]", command)
        if match:
            page.keyboard.press(match.group(1))

    elif command.startswith("new_tab"):
        page.context.new_page()

    elif command.startswith("tab_focus"):
        match = re.match(r"tab_focus\s+\[(\d+)\]", command)
        if match:
            idx = int(match.group(1))
            pages = page.context.pages
            if 0 <= idx < len(pages):
                pages[idx].bring_to_front()

    elif command.startswith("close_tab"):
        page.close()

    else:
        print(f"  Unknown command: {command}")

    return True


# ---------------------------------------------------------------------------
# DOM interaction helpers
# ---------------------------------------------------------------------------

def _get_element_bounds(page, backend_node_id: int) -> dict | None:
    """Get bounding rect for an element via CDP, same method as TIGER-AI-Lab."""
    cdp = page.context.new_cdp_session(page)
    try:
        remote = cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = remote["object"]["objectId"]
        response = cdp.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": """function() {
                if (this.nodeType == 3) {
                    var range = document.createRange();
                    range.selectNode(this);
                    var rect = range.getBoundingClientRect().toJSON();
                    range.detach();
                    return rect;
                } else {
                    return this.getBoundingClientRect().toJSON();
                }
            }""",
            "returnByValue": True,
        })
        return response.get("result", {}).get("value")
    except Exception:
        return None
    finally:
        cdp.detach()


def _click_by_tree_id(page, node_id: int):
    info = obs_node_map.get(node_id)
    if not info:
        print(f"  Could not find element for tree ID {node_id}")
        return
    bounds = _get_element_bounds(page, info["backend_id"])
    if bounds and bounds.get("width", 0) > 0 and bounds.get("height", 0) > 0:
        x = bounds["x"] + bounds["width"] / 2
        y = bounds["y"] + bounds["height"] / 2
        try:
            page.mouse.click(x, y)
        except Exception as e:
            print(f"  Click failed on tree ID {node_id}: {e}")
    else:
        # Fallback: try role + name locator
        try:
            page.get_by_role(info["role"], name=info["name"], exact=False).first.click()
        except Exception as e:
            print(f"  Click failed on tree ID {node_id}: {e}")


def _type_by_tree_id(page, node_id: int, content: str, press_enter: bool):
    info = obs_node_map.get(node_id)
    if not info:
        print(f"  Could not find element for tree ID {node_id}")
        return
    bounds = _get_element_bounds(page, info["backend_id"])
    if bounds and bounds.get("width", 0) > 0 and bounds.get("height", 0) > 0:
        x = bounds["x"] + bounds["width"] / 2
        y = bounds["y"] + bounds["height"] / 2
        try:
            page.mouse.click(x, y)
            # Clear existing content then type
            page.keyboard.press("Control+a")
            page.keyboard.type(content)
            if press_enter:
                page.keyboard.press("Enter")
        except Exception as e:
            print(f"  Type failed on tree ID {node_id}: {e}")
    else:
        # Fallback: try role + name locator
        try:
            locator = page.get_by_role(info["role"], name=info["name"], exact=False).first
            locator.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(content)
            if press_enter:
                page.keyboard.press("Enter")
        except Exception as e:
            print(f"  Type failed on tree ID {node_id}: {e}")


def _hover_by_tree_id(page, node_id: int):
    info = obs_node_map.get(node_id)
    if not info:
        print(f"  Could not find element for tree ID {node_id}")
        return
    bounds = _get_element_bounds(page, info["backend_id"])
    if bounds and bounds.get("width", 0) > 0 and bounds.get("height", 0) > 0:
        x = bounds["x"] + bounds["width"] / 2
        y = bounds["y"] + bounds["height"] / 2
        try:
            page.mouse.move(x, y)
        except Exception as e:
            print(f"  Hover failed on tree ID {node_id}: {e}")
    else:
        print(f"  Could not find element for tree ID {node_id}")


# ---------------------------------------------------------------------------
# Generic page-aware data extraction
# ---------------------------------------------------------------------------

def detect_page_context(page) -> dict:
    """Return a context dict with url, title, and a short label for tagging data."""
    url = page.url
    title = page.title()

    # Build a short slug from the URL path and title
    parsed = urlparse(url)
    # Take the last meaningful path segments
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    # Use last 2 path segments at most
    path_slug = "-".join(path_parts[-2:]) if path_parts else parsed.hostname or "page"

    # Try to extract a short identifier from the title (first few words)
    title_words = re.split(r"[\s|—\-:]+", title)
    title_slug = "-".join(title_words[:3]) if title_words else ""

    # Combine: prefer "domain-path" style, append title hint
    domain = (parsed.hostname or "").replace("www.", "")
    domain_short = domain.split(".")[0] if domain else ""

    if title_slug and path_slug:
        label = f"{domain_short}-{path_slug}-{title_slug}"
    elif path_slug:
        label = f"{domain_short}-{path_slug}"
    else:
        label = domain_short or "page"

    # Sanitize: keep alphanumeric and hyphens, limit length
    label = re.sub(r"[^A-Za-z0-9\-]", "", label)[:60]

    return {"url": url, "title": title, "label": label}


def try_extract_data(page, collected_data: list[list[str]], seen_snapshots: set,
                     label: str | None = None):
    """Extract data from the current page: tables first, screenshot fallback.

    Tables are tagged with a page-context label (or an explicit label from the
    agent's ``extract`` command).  If no tables are found and the page content
    has changed, a screenshot is saved instead.
    """
    ctx = detect_page_context(page)
    page_label = label or ctx["label"]
    source_url = ctx["url"]

    tables = page.query_selector_all("table")
    extracted_any = False

    for table in tables:
        rows = table.query_selector_all("tr")
        raw_rows = []
        for row in rows:
            cells = row.query_selector_all("th, td")
            raw_rows.append([cell.inner_text().strip() for cell in cells])

        if len(raw_rows) < 2:
            continue

        # Deduplicate by hashing header + first + last data rows (label-independent)
        snapshot = f"{raw_rows[0]}:{raw_rows[1] if len(raw_rows) > 1 else ''}:{raw_rows[-1]}"
        if snapshot in seen_snapshots:
            continue
        seen_snapshots.add(snapshot)

        # Add header with Page / Source_URL columns on first collection
        if not collected_data:
            collected_data.append(["Page", "Source_URL"] + raw_rows[0])

        # Add data rows (skip header row) with page label + URL prefix
        for data_row in raw_rows[1:]:
            collected_data.append([page_label, source_url] + data_row)

        extracted_any = True
        print(f"  Extracted {len(raw_rows) - 1} rows [{page_label}]")

    if not extracted_any:
        # Screenshot fallback — only if page content looks new
        content_hash = hashlib.md5(page.content().encode()).hexdigest()
        if content_hash not in seen_snapshots:
            seen_snapshots.add(content_hash)
            os.makedirs("./output", exist_ok=True)
            safe_label = re.sub(r"[^A-Za-z0-9\-_]", "_", page_label)[:40]
            screenshot_path = f"./output/snapshot_{safe_label}_{content_hash[:8]}.png"
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"  No tables found — screenshot saved: {screenshot_path}")


def save_collected_data(collected_data: list[list[str]], output_dir: str = "./output") -> str | None:
    """Save all collected table data to a single CSV."""
    if not collected_data:
        return None
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, "collected_data.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(collected_data)
    print(f"  Saved {len(collected_data) - 1} total rows to {filename}")
    return filename


# ---------------------------------------------------------------------------
# Cookie consent
# ---------------------------------------------------------------------------

def dismiss_cookie_consent(page):
    """Try to dismiss common cookie consent dialogs before the agent starts."""
    consent_selectors = [
        # Yahoo / Oath consent
        "button[name='agree']",
        "[data-testid='consent-accept']",
        "button.consent-accept",
        # Generic consent buttons (common patterns)
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Tout accepter')",
        "button:has-text('Agree')",
        "button:has-text('I agree')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        # GDPR / CMP frameworks
        ".cmp-revoke-consent button",
        "#onetrust-accept-btn-handler",
        ".cookie-consent-accept",
        "[aria-label='Accept cookies']",
        "[aria-label='Cookies akzeptieren']",
    ]

    page.wait_for_timeout(2000)  # Let consent dialogs load

    for selector in consent_selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=500):
                btn.click()
                print(f"  Dismissed cookie consent via: {selector}")
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue

    # Try iframe-based consent (some sites put it in an iframe)
    for frame in page.frames:
        for selector in consent_selectors:
            try:
                btn = frame.locator(selector).first
                if btn.is_visible(timeout=300):
                    btn.click()
                    print(f"  Dismissed cookie consent in iframe via: {selector}")
                    page.wait_for_timeout(1000)
                    return
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------

def detect_model_name() -> str:
    """Query the vLLM server to get the served model name."""
    try:
        resp = requests.get(f"{API_BASE}/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        if models:
            name = models[0]["id"]
            print(f"  Detected model: {name}")
            return name
    except Exception as e:
        print(f"  Could not detect model name: {e}")
    return "qwen2.5-7b"


# ---------------------------------------------------------------------------
# Auth setup functions — each returns (page, cleanup_fn)
# ---------------------------------------------------------------------------

def _ask(value, prompt, default=None):
    """Return *value* if already set (from CLI), otherwise prompt interactively."""
    if value is not None:
        return value
    raw = input(prompt).strip()
    return raw if raw else default


def setup_no_auth(pw, args):
    """Launch browser and navigate to a user-specified URL (no auth)."""
    start_url = _ask(
        args.url,
        "Enter the starting URL [default: https://www.google.com]: ",
        "https://www.google.com",
    )

    print(f"\nLaunching browser and navigating to {start_url} ...")
    browser = pw.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto(start_url, wait_until="domcontentloaded")
    dismiss_cookie_consent(page)

    return page, browser.close


def setup_credentials_auth(pw, args):
    """Launch browser, fill a login form with username/password, then hand off."""
    login_url = _ask(args.url, "Enter the login page URL:\n> ")
    if not login_url:
        print("No URL provided. Exiting.")
        return None, None

    username = _ask(args.username, "Enter username: ")
    password = _ask(args.password, "Enter password: ")

    username_sel = _ask(
        args.username_selector,
        "CSS selector for the username field [default: input[name='username']]: ",
        "input[name='username']",
    )
    password_sel = _ask(
        args.password_selector,
        "CSS selector for the password field [default: input[name='password']]: ",
        "input[name='password']",
    )
    submit_sel = _ask(
        args.submit_selector,
        "CSS selector for the submit button [default: button[type='submit']]: ",
        "button[type='submit']",
    )

    print(f"\nLaunching browser and navigating to {login_url} ...")
    browser = pw.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto(login_url, wait_until="domcontentloaded")
    dismiss_cookie_consent(page)

    # Fill login form
    print("Filling login credentials ...")
    page.fill(username_sel, username)
    page.fill(password_sel, password)
    page.click(submit_sel)
    page.wait_for_load_state("domcontentloaded")
    print("Login submitted. Waiting for page to settle ...")
    page.wait_for_timeout(2000)

    return page, browser.close


def setup_token_auth(pw, args):
    """Launch browser with a token injected as cookie or Authorization header."""
    target_url = _ask(args.url, "Enter the target URL:\n> ")
    if not target_url:
        print("No URL provided. Exiting.")
        return None, None

    token = _ask(args.token, "Enter the auth token: ")
    if not token:
        print("No token provided. Exiting.")
        return None, None

    token_type = args.token_type
    if token_type is None:
        print("\nHow should the token be injected?")
        print("  1) As a cookie (default)")
        print("  2) As an Authorization header")
        token_type = input("Choice [1/2]: ").strip() or "cookie"
    # Normalize: accept "1"/"cookie" and "2"/"header"
    if token_type in ("1", "cookie"):
        token_type = "cookie"
    else:
        token_type = "header"

    parsed = urlparse(target_url)
    domain = parsed.hostname

    print(f"\nLaunching browser and navigating to {target_url} ...")
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context()

    if token_type == "cookie":
        cookie_name = _ask(args.cookie_name, "Cookie name [default: session]: ", "session")
        context.add_cookies([{
            "name": cookie_name,
            "value": token,
            "domain": domain,
            "path": "/",
        }])
        print(f"Injected cookie '{cookie_name}' for domain {domain}")
    else:
        context.set_extra_http_headers({
            "Authorization": f"Bearer {token}",
        })
        print("Injected Authorization header")

    page = context.new_page()
    page.goto(target_url, wait_until="domcontentloaded")
    dismiss_cookie_consent(page)
    page.wait_for_timeout(1000)

    return page, browser.close


def setup_session_takeover(pw, args):
    """Launch a persistent browser for the user to log in manually."""
    user_data_dir = _ask(
        args.profile_dir,
        "Enter browser profile directory [default: ./browser_profile]: ",
        "./browser_profile",
    )

    print(f"\nLaunching browser with persistent profile at: {user_data_dir}")
    print("A browser window will open. Log in to your desired site.\n")

    context = pw.chromium.launch_persistent_context(
        user_data_dir,
        headless=False,
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()

    input(
        ">>> Log in to your desired site in the browser window.\n"
        ">>> When you are ready, press ENTER here to hand control to the agent..."
    )

    print(f"\nCurrent page: {page.url}")
    return page, context.close


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _read_multiline_task() -> str:
    """Read a multi-line task from stdin. Enter a blank line or Ctrl-D to finish."""
    print("\nEnter the task / instruction for the agent (blank line to finish):")
    lines = []
    while True:
        try:
            line = input("> " if not lines else "  ")
        except EOFError:
            break
        if line.strip() == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def run_agent(page, cleanup_fn, args):
    """Run the agent loop on an already-authenticated page."""
    task = args.task if args.task else _read_multiline_task()
    if not task:
        print("No task provided. Exiting.")
        if cleanup_fn:
            cleanup_fn()
        return

    history_action = "\n"
    history_info = "\n"
    collected_data = []
    seen_snapshots = set()
    last_command = ""
    repeat_count = 0

    for step in range(1, MAX_STEPS + 1):
        try:
            # Get accessibility tree as observation
            observation = get_accessibility_tree(page)

            # Loop detection: if the same command repeated 3+ times, inject a hint
            loop_hint = ""
            if repeat_count >= 3:
                loop_hint = (
                    f"\nWARNING: The action '{last_command}' has failed {repeat_count} "
                    f"times. Try a DIFFERENT element ID or approach. Look carefully at "
                    f"the accessibility tree for the correct interactive element "
                    f"(textbox, button, link) — NOT StaticText or InlineTextBox.\n"
                )
                print(f"  [Loop detected: '{last_command}' repeated {repeat_count}x, injecting hint]")

            print(f"\n--- Step {step} ---")
            response = send_prompt(
                task + loop_hint, observation, history_action, history_info
            )

            # Extract command and conclusion
            command = extract_command(response)
            conclusion = extract_conclusion(response)

            # Track repetitions
            if command == last_command:
                repeat_count += 1
            else:
                last_command = command
                repeat_count = 1

            # If stuck for 6+ repeats, bail on this action entirely
            if repeat_count >= 6:
                print(f"  [Aborting: same action repeated {repeat_count}x — skipping]")
                history_action += f"{command} (FAILED — repeated {repeat_count}x, skipping)\n"
                last_command = ""
                repeat_count = 0
                continue

            # Update history
            history_action += command + "\n"
            history_info += conclusion + "\n"

            if not execute_action(page, command, collected_data, seen_snapshots):
                break

        except Exception as e:
            print(f"  Error on step {step}: {e}")
            history_action += f"(error: {e})\n"
            # If we get repeated API errors, navigate back to break the cycle
            if "400 Client Error" in str(e):
                print("  [Context overflow — navigating back to simpler page]")
                try:
                    page.go_back(wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

        # Automatic per-step extraction as safety net
        try:
            try_extract_data(page, collected_data, seen_snapshots)
        except Exception:
            pass  # Page may have navigated; extraction is best-effort

        page.wait_for_timeout(1500)

    # Final extraction attempt on the last page
    try:
        try_extract_data(page, collected_data, seen_snapshots)
    except Exception:
        pass

    # Save all collected data
    print("\n--- Data Extraction ---")
    saved = save_collected_data(collected_data)
    if saved:
        print(f"\nAll data saved to {saved}")
    else:
        # Take a screenshot as final fallback
        screenshot_path = "./output/final_page.png"
        os.makedirs("./output", exist_ok=True)
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"No tables found. Screenshot saved to {screenshot_path}")

    if cleanup_fn:
        cleanup_fn()


# ---------------------------------------------------------------------------
# Main — auth mode selection + agent launch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Unified browser agent with selectable auth modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Fully non-interactive (no auth, public site)
  python run.py --auth 1 --url https://finance.yahoo.com --port 5001 \\
    --task "Search for RTX, go to Historical Data, extract the table"

  # Credential auth with all fields
  python run.py --auth 2 --url https://example.com/login \\
    --username admin --password secret --task "Download the report"

  # Interactive — just run with no args
  python run.py
""",
    )
    # Global
    p.add_argument("--port", default=None, help="Model server port (default: 5001)")
    p.add_argument("--auth", default=None, choices=["1", "2", "3", "4"],
                   help="Auth mode: 1=none, 2=credentials, 3=token, 4=session")
    p.add_argument("--url", default=None, help="Starting / login / target URL")
    p.add_argument("--task", default=None, help="Task instruction for the agent")

    # Credentials auth (mode 2)
    p.add_argument("--username", default=None, help="Login username (mode 2)")
    p.add_argument("--password", default=None, help="Login password (mode 2)")
    p.add_argument("--username-selector", default=None,
                   help="CSS selector for username field (mode 2)")
    p.add_argument("--password-selector", default=None,
                   help="CSS selector for password field (mode 2)")
    p.add_argument("--submit-selector", default=None,
                   help="CSS selector for submit button (mode 2)")

    # Token auth (mode 3)
    p.add_argument("--token", default=None, help="Auth token value (mode 3)")
    p.add_argument("--token-type", default=None, choices=["cookie", "header"],
                   help="Token injection method (mode 3)")
    p.add_argument("--cookie-name", default=None,
                   help="Cookie name when token-type=cookie (mode 3)")

    # Session takeover (mode 4)
    p.add_argument("--profile-dir", default=None,
                   help="Browser profile directory (mode 4)")

    return p


def main():
    global API_BASE, MODEL_NAME
    args = build_parser().parse_args()

    port = _ask(args.port, "Enter the model server port [default: 5001]: ", "5001")
    API_BASE = f"http://localhost:{port}/v1"

    print("Connecting to model server ...")
    MODEL_NAME = detect_model_name()

    choice = args.auth
    if choice is None:
        print("\nChoose authentication mode:")
        print("  1) No authentication       — public sites")
        print("  2) Username/password auth   — login form")
        print("  3) Token auth               — cookie or Bearer token")
        print("  4) Session takeover         — log in manually, agent takes over")
        choice = input("Choice [1-4]: ").strip()

    setup_fns = {
        "1": setup_no_auth,
        "2": setup_credentials_auth,
        "3": setup_token_auth,
        "4": setup_session_takeover,
    }

    setup_fn = setup_fns.get(choice)
    if not setup_fn:
        print("Invalid choice. Exiting.")
        return

    with sync_playwright() as pw:
        page, cleanup_fn = setup_fn(pw, args)
        if page is None:
            return
        run_agent(page, cleanup_fn, args)


if __name__ == "__main__":
    main()
