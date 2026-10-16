import os
from pathlib import Path

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["OTEL_SDK_DISABLED"] = "true"

import pytest
from fastapi.testclient import TestClient

from privacy_gateway.api import create_app
from privacy_gateway.config import Settings
from privacy_gateway.detection import DetectionEngine, RuleDetector
from privacy_gateway.pipeline import Pipeline
from privacy_gateway.policy import Policies

KEY = "synthetic-test-key-" + "x" * 32


class SpyTransport:
    def __init__(self):
        self.calls = []
        self.response = None
        self.error = None

    async def chat(self, messages, max_tokens):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.response or "\n".join(m["content"] for m in messages)


@pytest.fixture
def policies():
    return Policies(Path("config/policies.yaml"))


@pytest.fixture
def engine(policies):
    return DetectionEngine(RuleDetector(policies.config.custom_terms))


@pytest.fixture
def pipeline(engine, policies):
    return Pipeline(engine, policies)


@pytest.fixture
def spy():
    return SpyTransport()


@pytest.fixture
def settings():
    return Settings(api_key=KEY, signing_key=KEY, proxy_key=KEY, detector="rules")


@pytest.fixture
def client(settings, engine, spy):
    with TestClient(create_app(settings, engine, spy), raise_server_exceptions=False) as client:
        client.headers["Authorization"] = "Bearer " + KEY
        yield client
