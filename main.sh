#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# Step 1: Run setup if venv is missing or broken
if [ ! -f "$VENV_DIR/bin/activate" ] || [ ! -f "$VENV_DIR/bin/pip" ]; then
    echo "Virtual environment not found or incomplete. Running setup ..."
    echo
    rm -rf "$VENV_DIR"
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
    read -rp "Enter the local model path or HuggingFace model ID
(e.g. TIGER-Lab/BrowserAgent-RFT): " model_path
    if [ -z "$model_path" ]; then
        echo "No model path provided. Exiting."
        exit 1
    fi
    read -rp "Enter the port to serve on [default: 5001]: " model_port
    model_port="${model_port:-5001}"

    echo
    echo "Starting model server in a new shell ..."

    # Launch the model server in a separate terminal session
    setsid bash -c "source '$VENV_DIR/bin/activate' && python '$SCRIPT_DIR/start_model.py' --model '$model_path' --port '$model_port'" &
    MODEL_PID=$!
    echo "Model server PID: $MODEL_PID"
    echo "API endpoint: http://localhost:${model_port}/v1"

    # Wait for the server to be listening on the port
    echo "Waiting for model server to be ready on port ${model_port} ..."
    echo "  Sleeping 30s before polling (model takes ~2min to load) ..."
    sleep 30
    MAX_WAIT=300
    ELAPSED=30
    while ! curl -sf "http://localhost:${model_port}/v1/models" >/dev/null 2>&1; do
        if ! kill -0 "$MODEL_PID" 2>/dev/null; then
            echo "ERROR: Model server process died. Check logs above."
            exit 1
        fi
        if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
            echo "ERROR: Timed out after ${MAX_WAIT}s waiting for model server."
            exit 1
        fi
        sleep 2
        ELAPSED=$((ELAPSED + 2))
        printf "\r  %ds elapsed ..." "$ELAPSED"
    done
    printf "\r"
    echo "Model server is ready! (took ${ELAPSED}s)"
else
    read -rp "Enter the model server port [default: 5001]: " model_port
    model_port="${model_port:-5001}"
fi

echo

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
