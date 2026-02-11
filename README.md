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

# Terminal 2: run an agent script
source ./venv/bin/activate    # Activate the environment
python run_no_auth.py         # Run an agent script
```

> **Note:** `start_model.py` blocks the terminal while the server runs (so you can see logs). If you want a single-terminal experience, use `./main.sh` instead — it backgrounds the server automatically.

## Repository Structure

| File | Description |
|------|-------------|
| `main.sh` | All-in-one launcher — setup, model server, run mode menu |
| `setup.sh` | Environment setup — venv, GPU check, pip deps, Playwright |
| `start_model.py` | Launch vLLM OpenAI-compatible API server |
| `run_no_auth.py` | Run agent on public sites (no login) |
| `run_auth_credentials.py` | Run agent with username/password login |
| `run_auth_token.py` | Run agent with token auth (cookie or header) |
| `run_session_hijack.py` | Run agent by taking over a manually-authenticated session |
| `requirements.txt` | Python dependencies |

## Run Modes

### No Authentication

For public websites that don't require login.

```bash
python run_no_auth.py
```

You'll be prompted for a task and an optional starting URL.

### Username / Password Authentication

For sites with a standard login form.

```bash
python run_auth_credentials.py
```

You'll be prompted for the login URL, credentials, and CSS selectors for the form fields.

### Token Authentication

For sites that use session cookies or Bearer tokens.

```bash
python run_auth_token.py
```

You'll be prompted for the target URL, your token, and how to inject it (cookie or Authorization header).

### Session Takeover

For sites with complex login flows (MFA, CAPTCHA, SSO). A visible browser opens so you can log in manually, then the agent takes over.

```bash
python run_session_hijack.py
```

A Playwright browser with a persistent profile launches. Log in yourself, press Enter in the terminal, and the agent takes control of the authenticated session.

## How It Works

1. **`start_model.py`** serves the Tiger Browser-Agent model via vLLM's OpenAI-compatible API
2. Each **run script** launches a Playwright Chromium browser, handles authentication, then enters an agent loop:
   - Capture the current page HTML
   - Send the task + page content to the model
   - Parse the model's JSON response into a browser action (click, fill, navigate, scroll, etc.)
   - Execute the action and repeat
3. The agent stops when the model returns a `"done"` action or after 20 steps

## License

MIT
