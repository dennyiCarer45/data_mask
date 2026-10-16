"""Real proxy ordering test. Only synthetic text and local loopback servers."""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from privacy_gateway.approval import sign

pytestmark = pytest.mark.integration


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_server(url, children):
    with httpx.Client(trust_env=False) as client:
        for _ in range(120):
            if any(child.poll() is not None for child in children):
                raise AssertionError("local service exited; inspect synthetic integration log")
            try:
                client.get(url, timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.5)
    raise AssertionError("local service did not become ready")


@pytest.mark.skipif(os.environ.get("RUN_PROXY_TESTS") != "1", reason="set RUN_PROXY_TESTS=1")
@pytest.mark.parametrize(
    "detector",
    [
        "rules",
        pytest.param(
            "gliner",
            marks=pytest.mark.skipif(
                os.environ.get("RUN_MODEL_TESTS") != "1", reason="set RUN_MODEL_TESTS=1"
            ),
        ),
    ],
)
def test_real_litellm_ordering_and_bypass(tmp_path, detector):
    mock_port, proxy_port, gateway_port = free_port(), free_port(), free_port()
    key = "sk-synthetic-integration-" + "x" * 40
    capture = tmp_path / "provider.jsonl"
    mock = tmp_path / "capture_mock.py"
    mock.write_text(
        """import json, os
from fastapi import FastAPI, Request
app = FastAPI()
@app.post("/v1/chat/completions")
async def chat(request: Request):
    data = await request.json()
    with open(os.environ["CAPTURE_FILE"], "a") as f:
        f.write(json.dumps(data) + "\\n")
    return {"id":"mock", "object":"chat.completion", "created":0, "model":"local-mock",
            "choices":[{"index":0,"message":{"role":"assistant",
            "content":data["messages"][-1]["content"]},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PRIVACY_API_KEY": key,
        "PRIVACY_PROXY_KEY": key,
        "PRIVACY_SIGNING_KEY": key,
        "PRIVACY_DETECTOR": detector,
        "PRIVACY_PROXY_URL": f"http://127.0.0.1:{proxy_port}",
        "MOCK_API_BASE": f"http://127.0.0.1:{mock_port}/v1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OTEL_SDK_DISABLED": "true",
        "LITELLM_TELEMETRY": "False",
        "CAPTURE_FILE": str(capture),
    }
    commands = [
        [
            "-m",
            "uvicorn",
            "capture_mock:app",
            "--app-dir",
            str(tmp_path),
            "--port",
            str(mock_port),
            "--no-access-log",
        ],
        [
            "-c",
            "from litellm import run_server; run_server()",
            "--config",
            "config/litellm.yaml",
            "--host",
            "127.0.0.1",
            "--port",
            str(proxy_port),
        ],
        [
            "-m",
            "uvicorn",
            "privacy_gateway.api:create_app",
            "--factory",
            "--port",
            str(gateway_port),
            "--no-access-log",
        ],
    ]
    children = []
    log = tmp_path / "services.log"
    try:
        with log.open("w", encoding="utf-8") as output:
            for command in commands:
                children.append(
                    subprocess.Popen(
                        [sys.executable, *command],
                        env=env,
                        stdout=output,
                        stderr=output,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                )
            wait_for_server(f"http://127.0.0.1:{proxy_port}/health/liveliness", children)
            wait_for_server(f"http://127.0.0.1:{gateway_port}/health", children)
            headers = {"Authorization": "Bearer " + key}
            with httpx.Client(trust_env=False, timeout=30) as client:
                url = f"http://127.0.0.1:{gateway_port}/llm/chat"
                result = client.post(
                    url,
                    headers=headers,
                    json={
                        "messages": [
                            {"role": "user", "content": "Patient: Mira Vale\nPhone: 9876543210"}
                        ]
                    },
                )
                assert result.status_code == 200, result.text + "\n" + log.read_text()
                seen = capture.read_text()
                assert "Mira Vale" not in seen and "9876543210" not in seen
                assert "[PERSON_001]" in seen and "privacy_approval" not in seen
                count = len(seen.splitlines())
                blocked = client.post(
                    url,
                    headers=headers,
                    json={
                        "messages": [
                            {"role": "user", "content": "Password: synthetic-private-secret"}
                        ]
                    },
                )
                assert blocked.status_code == 422
                proxy_url = f"http://127.0.0.1:{proxy_port}/v1/chat/completions"
                unsigned = {
                    "model": "privacy-mock",
                    "messages": [{"role": "user", "content": "synthetic unsigned probe"}],
                    "max_tokens": 64,
                    "stream": False,
                }
                rejected = client.post(proxy_url, headers=headers, json=unsigned)
                assert rejected.status_code >= 400
                ticket = sign(unsigned, key)
                tampered = {
                    **unsigned,
                    "metadata": {"privacy_approval": ticket},
                    "extra_body": {"messages": [{"role": "user", "content": "probe"}]},
                }
                assert client.post(proxy_url, headers=headers, json=tampered).status_code >= 400
                assert len(capture.read_text().splitlines()) == count
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
    logs = log.read_text(encoding="utf-8")
    assert "Mira Vale" not in logs and "9876543210" not in logs
    assert "synthetic-private-secret" not in logs
