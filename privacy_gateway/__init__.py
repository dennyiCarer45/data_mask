"""Local privacy enforcement. No provider SDK calls belong in this package."""

import os

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
