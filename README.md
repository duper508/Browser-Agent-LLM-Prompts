# Browser-Agent LLM Prompts

End-to-end toolkit for running the **Tiger Browser-Agent RFT** model as an autonomous browser automation agent. Provides environment setup, model serving via vLLM, and multiple run modes with different authentication strategies.

## Prerequisites

- Python 3.10+
- NVIDIA GPU with CUDA drivers (required by vLLM)
- Linux (tested on Ubuntu / WSL2)

## Quick Start

The easiest way to get started is with `main.sh`, which handles setup, model serving, and run mode selection in one flow:

```bash
git clone https://github.com/<your-username>/Browser-Agent-LLM-Prompts.git
cd Browser-Agent-LLM-Prompts
chmod +x main.sh setup.sh
./main.sh
```

Or run each step manually (requires two terminals):

```bash
# Terminal 1: setup and start the model server
./setup.sh                    # Create venv, install deps, Playwright browsers
source ./venv/bin/activate    # Activate the environment
python start_model.py         # Start the model server (blocks this terminal)

# Terminal 2: run the agent
source ./venv/bin/activate    # Activate the environment
python run.py                 # Interactive prompts for auth mode, URL, task, etc.
```

> **Note:** `start_model.py` blocks the terminal while the server runs (so you can see logs). If you want a single-terminal experience, use `./main.sh` instead — it backgrounds the server automatically.

## Repository Structure

| File | Description |
|------|-------------|
| `main.sh` | All-in-one launcher — setup, model server, agent |
| `setup.sh` | Environment setup — venv, GPU check, pip deps, Playwright |
| `start_model.py` | Launch vLLM OpenAI-compatible API server |
| `run.py` | Unified agent runner — all auth modes, CLI args or interactive |
| `requirements.txt` | Python dependencies |

## Usage

`run.py` supports both fully interactive mode (just run it with no flags) and fully parameterized mode via CLI arguments. Run `python run.py --help` to see all options.

### CLI Arguments

| Flag | Auth Modes | Description |
|------|------------|-------------|
| `--port PORT` | all | Model server port (default: 5001) |
| `--auth {1,2,3,4}` | all | Auth mode: 1=none, 2=credentials, 3=token, 4=session |
| `--url URL` | all | Starting / login / target URL |
| `--task TASK` | all | Task instruction for the agent |
| `--username USER` | 2 | Login username |
| `--password PASS` | 2 | Login password |
| `--username-selector SEL` | 2 | CSS selector for username field |
| `--password-selector SEL` | 2 | CSS selector for password field |
| `--submit-selector SEL` | 2 | CSS selector for submit button |
| `--token TOKEN` | 3 | Auth token value |
| `--token-type {cookie,header}` | 3 | Token injection method |
| `--cookie-name NAME` | 3 | Cookie name (when token-type=cookie) |
| `--profile-dir DIR` | 4 | Browser profile directory |

Any flag you omit will be asked interactively at runtime.

### Examples

```bash
# Public site, no auth, fully scripted
python run.py --auth 1 --url https://finance.yahoo.com --port 5001 \
  --task "For each stock (RTX, GOOG, MSFT): search the ticker, go to Historical Data, set 1 Year daily, use extract [TICKER historical] to capture the table, then go back"

# Credential auth
python run.py --auth 2 --url https://example.com/login \
  --username admin --password secret \
  --task "Navigate to the reports page and extract the quarterly summary"

# Token auth (cookie)
python run.py --auth 3 --url https://api.example.com/dashboard \
  --token "abc123" --token-type cookie --cookie-name session \
  --task "Download the latest metrics"

# Session takeover (interactive login, scripted task)
python run.py --auth 4 --task "Go to billing and extract the invoice table"

# Fully interactive — just run it
python run.py
```

## Auth Modes

| Mode | Flag | Description |
|------|------|-------------|
| **1 — No auth** | `--auth 1` | Public sites, no login needed |
| **2 — Credentials** | `--auth 2` | Fill a username/password login form automatically |
| **3 — Token** | `--auth 3` | Inject a session cookie or Bearer token |
| **4 — Session takeover** | `--auth 4` | Log in manually in the browser, then hand control to the agent |

## How It Works

1. **`start_model.py`** serves the Tiger Browser-Agent model via vLLM's OpenAI-compatible API
2. **`run.py`** launches a Playwright Chromium browser, handles authentication, then enters an agent loop:
   - Capture the page's accessibility tree
   - Send the task + observation to the model
   - Parse the model's response into a browser action (click, type, navigate, scroll, extract, etc.)
   - Execute the action and repeat
3. The agent stops when the model issues a `stop` action or after 30 steps
4. Extracted table data is saved to `./output/collected_data.csv` with `Page` and `Source_URL` columns; screenshots are saved as fallback when no tables are found

## License

MIT
