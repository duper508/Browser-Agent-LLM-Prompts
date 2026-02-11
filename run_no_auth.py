#!/usr/bin/env python3
"""Run the browser agent without authentication — for public sites.

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
MODEL_NAME = "qwen2.5-7b"
MAX_STEPS = 30

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


def get_accessibility_tree(page) -> str:
    """Get a simplified accessibility tree from the page via Playwright."""
    tree = page.accessibility.snapshot()
    if not tree:
        return "(empty page)"
    lines = []
    _walk_tree(tree, lines, depth=0, counter=[0])
    return "\n".join(lines)


def _walk_tree(node, lines, depth, counter):
    """Recursively walk the accessibility tree and format it with IDs."""
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    # Skip generic/empty nodes but still walk children
    if role not in ("none", "generic", "") or name:
        node_id = counter[0]
        counter[0] += 1
        indent = "  " * depth
        parts = [f"{indent}[{node_id}] {role}"]
        if name:
            parts.append(f"'{name}'")
        if value:
            parts.append(f"value='{value}'")
        lines.append(" ".join(parts))

    for child in node.get("children", []):
        _walk_tree(child, lines, depth + 1, counter)


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
    user_content = USER_PROMPT_TEMPLATE.format(
        objective=objective,
        observation=observation,
        history_action=history_action,
        history_info=history_info,
    )
    prompt = SYSTEM_PROMPT + "\n\n" + user_content

    # Try chat completions first
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

    resp.raise_for_status()
    data = resp.json()

    # Parse response from whichever endpoint worked
    if "message" in data["choices"][0]:
        return data["choices"][0]["message"]["content"]
    return data["choices"][0]["text"]


def execute_action(page, command: str) -> bool:
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

    if command.startswith("click"):
        match = re.match(r"click\s+\[(\d+)\]", command)
        if match:
            node_id = int(match.group(1))
            _click_by_tree_id(page, node_id)

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
            node_id = int(match.group(1))
            _hover_by_tree_id(page, node_id)

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


def _find_element_by_tree_id(page, target_id: int):
    """Find a page element by walking the accessibility tree to match the target ID."""
    tree = page.accessibility.snapshot()
    if not tree:
        return None
    path = []
    _find_path(tree, target_id, [0], path)
    if not path:
        return None
    node = path[-1]
    name = node.get("name", "")
    role = node.get("role", "")

    # Try to locate via role and name
    role_to_selector = {
        "link": "a",
        "button": "button",
        "textbox": "input, textarea",
        "searchbox": "input[type='search'], input",
        "combobox": "select, input",
        "checkbox": "input[type='checkbox']",
        "radio": "input[type='radio']",
        "tab": "[role='tab']",
        "menuitem": "[role='menuitem']",
    }

    selector = role_to_selector.get(role)
    if selector and name:
        try:
            locator = page.get_by_role(role, name=name, exact=False).first
            if locator.is_visible():
                return locator
        except Exception:
            pass

    # Fallback: try text matching
    if name:
        try:
            locator = page.get_by_text(name, exact=False).first
            if locator.is_visible():
                return locator
        except Exception:
            pass

    return None


def _find_path(node, target_id, counter, path):
    """DFS to find the node matching the target accessibility tree ID."""
    role = node.get("role", "")
    name = node.get("name", "")
    if role not in ("none", "generic", "") or name:
        if counter[0] == target_id:
            path.append(node)
            return True
        counter[0] += 1

    for child in node.get("children", []):
        if _find_path(child, target_id, counter, path):
            path.append(node)
            return True
    return False


def _click_by_tree_id(page, node_id: int):
    el = _find_element_by_tree_id(page, node_id)
    if el:
        el.click()
    else:
        print(f"  Could not find element for tree ID {node_id}")


def _type_by_tree_id(page, node_id: int, content: str, press_enter: bool):
    el = _find_element_by_tree_id(page, node_id)
    if el:
        el.fill(content)
        if press_enter:
            el.press("Enter")
    else:
        print(f"  Could not find element for tree ID {node_id}")


def _hover_by_tree_id(page, node_id: int):
    el = _find_element_by_tree_id(page, node_id)
    if el:
        el.hover()
    else:
        print(f"  Could not find element for tree ID {node_id}")


def extract_tables(page, output_dir: str = "./output") -> list[str]:
    """Extract all HTML tables from the current page and save each as a CSV."""
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


def main():
    global API_BASE
    port = input("Enter the model server port [default: 5001]: ").strip() or "5001"
    API_BASE = f"http://localhost:{port}/v1"

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

        history_action = "\n"
        history_info = "\n"

        for step in range(1, MAX_STEPS + 1):
            # Get accessibility tree as observation
            observation = get_accessibility_tree(page)

            print(f"\n--- Step {step} ---")
            response = send_prompt(task, observation, history_action, history_info)

            # Extract command and conclusion
            command = extract_command(response)
            conclusion = extract_conclusion(response)

            # Update history
            history_action += command + "\n"
            history_info += conclusion + "\n"

            if not execute_action(page, command):
                break

            page.wait_for_timeout(1500)

        # After agent finishes, extract any tables on the page
        print("\n--- Data Extraction ---")
        saved_files = extract_tables(page)
        if saved_files:
            print(f"\nExtracted {len(saved_files)} table(s) to ./output/")
        else:
            # Take a screenshot as fallback
            screenshot_path = "./output/final_page.png"
            os.makedirs("./output", exist_ok=True)
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"No tables found. Screenshot saved to {screenshot_path}")

        browser.close()


if __name__ == "__main__":
    main()
