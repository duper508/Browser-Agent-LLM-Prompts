#!/usr/bin/env python3
"""Start a vLLM OpenAI-compatible API server for the Tiger Browser-Agent model."""

import argparse
import os
import re
import subprocess
import sys


def to_wsl_path(path: str) -> str:
    """Convert a Windows path (e.g. C:\\Users\\foo) to a WSL path (/mnt/c/Users/foo)."""
    match = re.match(r"^([A-Za-z]):[\\\/](.*)", path)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def resolve_model_path(path: str) -> str:
    """If the path looks like a local path, ensure it exists. Convert Windows paths if needed."""
    # If it contains a slash or backslash, treat it as a local path
    if "\\" in path or (os.sep == "/" and path.startswith("/")):
        if not os.path.exists(path):
            wsl_path = to_wsl_path(path)
            if wsl_path != path and os.path.exists(wsl_path):
                print(f"Windows path detected. Converting to WSL path: {wsl_path}")
                return wsl_path
            print(f"WARNING: Path not found: {path}")
        return path
    # Otherwise it's likely a HuggingFace model ID
    return path


def main():
    parser = argparse.ArgumentParser(description="Start vLLM model server")
    parser.add_argument("--model", help="Local model path or HuggingFace model ID")
    parser.add_argument("--port", help="Port to serve on")
    args = parser.parse_args()

    model_path = args.model or input(
        "Enter the local model path or HuggingFace model ID\n"
        "(e.g. TIGER-Lab/BrowserAgent-RFT): "
    ).strip()
    if not model_path:
        print("No model path provided. Exiting.")
        sys.exit(1)

    model_path = resolve_model_path(model_path)
    port = args.port or input("Enter the port to serve on [default: 5001]: ").strip() or "5001"

    print(f"\nStarting vLLM server on port {port} with model: {model_path}")
    print(f"API endpoint will be: http://localhost:{port}/v1\n")

    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", model_path,
        "--host", "localhost",
        "--port", port,
        "--served-model-name", "qwen2.5-7b",
        "--max-model-len", "32768",
        "--gpu-memory-utilization", "0.9",
        "--trust-remote-code",
        "--disable-log-requests",
    ]

    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except subprocess.CalledProcessError as e:
        print(f"Server exited with error code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
