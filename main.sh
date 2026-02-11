#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# Step 1: Run setup if venv doesn't exist
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Virtual environment not found. Running setup first ..."
    echo
    bash "$SCRIPT_DIR/setup.sh"
fi

# Activate the venv
source "$VENV_DIR/bin/activate"
echo "Using Python: $(which python)"
echo

# Step 2: Start the model server
echo "=== Step 1: Start Model Server ==="
echo "Make sure the model server is running before continuing."
echo
echo "  1) Start the model server now"
echo "  2) Skip (server is already running)"
echo
read -rp "Choice [1/2]: " model_choice

if [ "$model_choice" = "1" ]; then
    echo
    echo "Starting model server in the background ..."
    python "$SCRIPT_DIR/start_model.py" &
    MODEL_PID=$!
    echo "Model server PID: $MODEL_PID"
    echo "Waiting 10 seconds for server to start ..."
    sleep 10
fi

# Step 3: Choose run mode
while true; do
    echo
    echo "=== Step 2: Choose Run Mode ==="
    echo
    echo "  1) No authentication       — public sites"
    echo "  2) Username/password auth   — login form"
    echo "  3) Token auth               — cookie or Bearer token"
    echo "  4) Session takeover         — log in manually, agent takes over"
    echo "  5) Quit"
    echo
    read -rp "Choice [1-5]: " run_choice

    case "$run_choice" in
        1) python "$SCRIPT_DIR/run_no_auth.py" ;;
        2) python "$SCRIPT_DIR/run_auth_credentials.py" ;;
        3) python "$SCRIPT_DIR/run_auth_token.py" ;;
        4) python "$SCRIPT_DIR/run_session_hijack.py" ;;
        5) break ;;
        *) echo "Invalid choice." ;;
    esac
done

# Cleanup
if [ -n "$MODEL_PID" ] && kill -0 "$MODEL_PID" 2>/dev/null; then
    echo "Stopping model server (PID $MODEL_PID) ..."
    kill "$MODEL_PID"
fi

echo "Done."
