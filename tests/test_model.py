import os
from pathlib import Path

import pytest

from privacy_gateway.detection import DetectionEngine, GLiNERDetector, RuleDetector
from privacy_gateway.pipeline import Pipeline

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(
        os.environ.get("RUN_MODEL_TESTS") != "1", reason="set RUN_MODEL_TESTS=1 after provisioning"
    ),
]


def test_real_local_gliner_and_guardrails(policies):
    model = GLiNERDetector(Path("models/gliner"), 0.35)
    engine = DetectionEngine(RuleDetector(policies.config.custom_terms), model)
    entities = engine.detect("Mira Vale emailed mira@example.test about a follow-up.")
    assert any(e.source == "gliner" and e.entity_type == "PERSON" for e in entities)
    pipeline = Pipeline(engine, policies)
    result, _, context = pipeline.sanitize(["Patient: Mira Vale\nPhone: 9876543210"], "healthcare")
    assert "Mira Vale" not in result[0] and "9876543210" not in result[0]
    pipeline.validate(result[0], "healthcare", context, output=True)
    context.clear()
