#!/usr/bin/env python3
"""Run the browser agent by taking over an existing browser session.

Launches a visible Playwright browser with a persistent context so the user
can log in manually. Once the user confirms, the agent takes control.

Uses the TIGER-AI-Lab BrowserAgent prompt format with accessibility tree
observations. After the agent finishes navigating, any tables on the page
are automatically extracted and saved to CSV.
"""

import csv
import os
import re
import requests
from playwright.sync_api import sync_playwright

API_BASE = None  # Set in main()
MODEL_NAME = None  # Auto-detected from server
MAX_STEPS = 30
MAX_CONTEXT_CHARS = 100000  # ~25K tokens; keeps prompt under 32K context with room for completion

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


# Global mapping from tree observation ID → backendDOMNodeId, rebuilt each step
obs_node_map = {}


def get_accessibility_tree(page) -> str:
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
    return "\n".join(lines)


def _walk_cdp_tree(node, node_map, lines, depth, counter):
    role = _get_ax_value(node, "role")
    name = _get_ax_value(node, "name")

    skip_roles = {"none", "generic", "Ignored", "ignored", "InlineTextBox", ""}
    valid = role not in skip_roles or name.strip()

    if not name.strip() and role in (
        "generic", "img", "list", "strong", "paragraph",
        "banner", "navigation", "Section", "LabelText", "Legend", "listitem",
    ):
        valid = False

    if valid:
        obs_id = counter[0]
        counter[0] += 1
        indent = "\t" * depth
        node_str = f"[{obs_id}] {role} {repr(name)}"

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
    obj = node.get(field, {})
    if isinstance(obj, dict):
        return obj.get("value", "")
    return str(obj)


def extract_command(text: str) -> str:
    blocks = re.findall(r"```\s*([^\s].*?[^\s])\s*```", text, re.DOTALL)
    if not blocks:
        return ""
    return blocks[-1].strip().replace("```", "").strip()


def extract_conclusion(text: str) -> str:
    blocks = re.findall(r"<conclusion>\s*(.*?)\s*</conclusion>", text, re.DOTALL)
    if not blocks:
        return ""
    return blocks[-1].strip()


def send_prompt(objective: str, observation: str, history_action: str, history_info: str) -> str:
    # Truncate to fit within model context window (~4 chars/token).
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

    resp.raise_for_status()
    data = resp.json()

    if "message" in data["choices"][0]:
        return data["choices"][0]["message"]["content"]
    return data["choices"][0]["text"]


def execute_action(page, command: str) -> bool:
    if not command:
        print("  No command extracted.")
        return True

    print(f"  Action: {command}")

    if command.startswith("stop"):
        answer = re.match(r"stop\s*\[(.+)\]", command, re.DOTALL)
        if answer:
            print(f"\n  Agent answer: {answer.group(1)}")
        return False

    if command.startswith("click"):
        match = re.match(r"click\s+\[(\d+)\]", command)
        if match:
            _click_by_tree_id(page, int(match.group(1)))

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
        page.evaluate(f"window.scrollBy(0, {500 if direction == 'down' else -500})")

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


def _get_element_bounds(page, backend_node_id: int) -> dict | None:
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
            page.keyboard.press("Control+a")
            page.keyboard.type(content)
            if press_enter:
                page.keyboard.press("Enter")
        except Exception as e:
            print(f"  Type failed on tree ID {node_id}: {e}")
    else:
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


def extract_tables(page, output_dir: str = "./output") -> list[str]:
    tables = page.query_selector_all("table")
    if not tables:
        print("\nNo tables found on the page.")
        return []

    os.makedirs(output_dir, exist_ok=True)
    saved = []

    for i, table in enumerate(tables):
        rows = table.query_selector_all("tr")
        table_data = []
        for row in rows:
            cells = row.query_selector_all("th, td")
            table_data.append([cell.inner_text().strip() for cell in cells])

        if not table_data:
            continue

        filename = os.path.join(output_dir, f"table_{i + 1}.csv")
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(table_data)

        saved.append(filename)
        print(f"  Saved table ({len(table_data)} rows) to {filename}")

    return saved


def dismiss_cookie_consent(page):
    consent_selectors = [
        "button[name='agree']",
        "[data-testid='consent-accept']",
        "button.consent-accept",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Tout accepter')",
        "button:has-text('Agree')",
        "button:has-text('I agree')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        ".cmp-revoke-consent button",
        "#onetrust-accept-btn-handler",
        ".cookie-consent-accept",
        "[aria-label='Accept cookies']",
        "[aria-label='Cookies akzeptieren']",
    ]

    page.wait_for_timeout(2000)

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


def detect_model_name() -> str:
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


def main():
    global API_BASE, MODEL_NAME
    port = input("Enter the model server port [default: 5001]: ").strip() or "5001"
    API_BASE = f"http://localhost:{port}/v1"

    print("Connecting to model server ...")
    MODEL_NAME = detect_model_name()

    user_data_dir = input(
        "Enter browser profile directory [default: ./browser_profile]: "
    ).strip() or "./browser_profile"

    print(f"\nLaunching browser with persistent profile at: {user_data_dir}")
    print("A browser window will open. Log in to your desired site.\n")

    with sync_playwright() as pw:
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

        task = input("\nEnter the task / instruction for the agent:\n> ").strip()
        if not task:
            print("No task provided. Exiting.")
            context.close()
            return

        history_action = "\n"
        history_info = "\n"

        for step in range(1, MAX_STEPS + 1):
            try:
                observation = get_accessibility_tree(page)

                print(f"\n--- Step {step} ---")
                response = send_prompt(task, observation, history_action, history_info)

                command = extract_command(response)
                conclusion = extract_conclusion(response)

                history_action += command + "\n"
                history_info += conclusion + "\n"

                if not execute_action(page, command):
                    break

            except Exception as e:
                print(f"  Error on step {step}: {e}")
                history_action += f"(error: {e})\n"

            page.wait_for_timeout(1500)

        # Extract tables after agent finishes
        print("\n--- Data Extraction ---")
        saved_files = extract_tables(page)
        if saved_files:
            print(f"\nExtracted {len(saved_files)} table(s) to ./output/")
        else:
            screenshot_path = "./output/final_page.png"
            os.makedirs("./output", exist_ok=True)
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"No tables found. Screenshot saved to {screenshot_path}")

        context.close()


if __name__ == "__main__":
    main()
