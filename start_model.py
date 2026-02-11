#!/usr/bin/env python3
"""Start a vLLM OpenAI-compatible API server for the Tiger Browser-Agent model."""

import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Start vLLM model server")
    parser.add_argument("--model", help="Local model path or HuggingFace model ID")
    parser.add_argument("--port", help="Port to serve on")
    args = parser.parse_args()

    model_path = args.model or input(
        "Enter the local model path or HuggingFace model ID\n"
        "(e.g. tiger-research/BrowserAgent-RFT): "
    ).strip()
    if not model_path:
        print("No model path provided. Exiting.")
        sys.exit(1)

    port = args.port or input("Enter the port to serve on [default: 8000]: ").strip() or "8000"

    print(f"\nStarting vLLM server on port {port} with model: {model_path}")
    print(f"API endpoint will be: http://localhost:{port}/v1\n")

    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", model_path,
        "--port", port,
        "--trust-remote-code",
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
