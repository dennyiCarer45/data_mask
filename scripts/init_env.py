"""Create project-local secrets; never print them or overwrite an existing .env."""

import secrets
from pathlib import Path

path = Path(".env")
with path.open("x", encoding="utf-8") as stream:
    for name in ("PRIVACY_API_KEY", "PRIVACY_PROXY_KEY", "PRIVACY_SIGNING_KEY"):
        stream.write(f"{name}=sk-{secrets.token_urlsafe(36)}\n")
    stream.write("PRIVACY_DETECTOR=gliner\nPRIVACY_EXTERNAL_ENABLED=false\n")
try:
    path.chmod(0o600)
except OSError:
    pass
print("Created .env with independent local secrets. Keep it private.")
