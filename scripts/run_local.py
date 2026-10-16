"""Start loopback mock, real LiteLLM Proxy, and gateway; Ctrl+C stops all children."""

import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values


def main():
    if not Path(".env").is_file():
        raise SystemExit("Run python scripts/init_env.py first")
    environment = dict(os.environ)
    environment.update(
        {key: value for key, value in dotenv_values(".env").items() if value is not None}
    )
    environment.update(
        {
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "OTEL_SDK_DISABLED": "true",
            "LITELLM_TELEMETRY": "False",
            "MOCK_API_BASE": "http://127.0.0.1:8001/v1",
        }
    )
    commands = [
        ["-m", "uvicorn", "privacy_gateway.mock_llm:app", "--port", "8001", "--no-access-log"],
        [
            "-c",
            "from litellm import run_server; run_server()",
            "--config",
            "config/litellm.yaml",
            "--host",
            "127.0.0.1",
            "--port",
            "4000",
        ],
        [
            "-m",
            "uvicorn",
            "privacy_gateway.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--no-access-log",
        ],
    ]
    children = []
    try:
        for command in commands:
            children.append(
                subprocess.Popen(
                    [sys.executable, *command],
                    env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            )
        print(
            "Starting local stack; check http://127.0.0.1:8000/health. Ctrl+C stops all services."
        )
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
