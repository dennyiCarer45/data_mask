# Local Privacy Gateway

Standalone Python backend; all confidential-data detection is local. Read README.md and
docs/security.md before changing privacy boundaries. Only use synthetic data in tests.

## Invariants

- Complete extraction, GLiNER/rule detection, masking, and real Guardrails validation before
  the sole outbound call in transport.py. Never use a concurrent moderation hook for privacy.
- Fail closed on model, extraction, policy, validator, transport, and output failures.
- Never send original documents, request-local mappings, keys, or raw histories to providers.
- No body/exception logging or telemetry. Errors use static public codes; never echo Pydantic input.
- No Presidio, paid detection services, remote model inference, or request-time downloads.
- Keep external models disabled by default. Rules mode is for mock demonstrations only.
- Proxy stays private, requires its own key plus signed approval, and accepts no caller-selected model,
  URLs, extra body, tools, attachments, or streaming. Keep output buffered until validated.
- Do not automatically allow diagnoses, amounts, commercial terms, or organizational content.
- Do not claim zero leakage. Report exact tested versions, fixtures, skips, and limitations.

## Verification

Python >=3.11,<3.14 (3.13 verified). `pip install -e '.[dev,proxy]'`.
`python -m pytest -q`; set RUN_PROXY_TESTS=1 for a real local proxy subprocess test.
Provision `python scripts/download_model.py`, then set RUN_MODEL_TESTS=1 for model integration.
`python scripts/evaluate.py --mode rules` and `--mode gliner --output docs/evaluation-gliner.json`.
`ruff check privacy_gateway scripts tests`; `ruff format --check privacy_gateway scripts tests`.

This Windows host allows signed Python 3.13 but application control blocks PyTorch/PyMuPDF
native binaries. Use the provided Docker image for full verification; do not disable host controls.
Dependencies, model files, and runtime caches belong inside this project. Never commit .env/models.
The generated freeze files document tested environments; revalidate updates and model revisions.
Update docs/evaluation.md and docs/decisions.md when changing detection, versions, or measurements.

