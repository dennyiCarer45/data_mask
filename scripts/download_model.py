"""Provision public model artifacts once. Never run this in the request handler."""

import hashlib
import json
import os
from pathlib import Path

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from huggingface_hub import snapshot_download  # noqa: E402

MODEL = "urchade/gliner_small-v2.1"
REVISION = "4e091416cf7c3481db542c2a3d26156916f3a47f"
BACKBONE = "microsoft/deberta-v3-small"
BACKBONE_REVISION = "a36c739020e01763fe789b4b85e2df55d6180012"


def main():
    root = Path("models/gliner")
    snapshot_download(
        MODEL,
        revision=REVISION,
        local_dir=root,
        allow_patterns=["gliner_config.json", "pytorch_model.bin", "README.md"],
    )
    snapshot_download(
        BACKBONE,
        revision=BACKBONE_REVISION,
        local_dir=root / "backbone",
        allow_patterns=["config.json", "tokenizer_config.json", "spm.model", "README.md"],
    )
    # Embed backbone configuration and tokenizer so runtime is completely offline and relocatable.
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        str(root / "backbone"), local_files_only=True, model_max_length=512, fix_mistral_regex=False
    )
    tokenizer.save_pretrained(root)
    config = json.loads((root / "gliner_config.json").read_text())
    config["encoder_config"] = json.loads((root / "backbone/config.json").read_text())
    (root / "gliner_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    hashes = {}
    for path in root.rglob("*"):
        if path.is_file() and ".cache" not in path.parts and path.name != "manifest.json":
            with path.open("rb") as stream:
                hashes[str(path.relative_to(root))] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "model": MODEL,
                "revision": REVISION,
                "license": "Apache-2.0",
                "backbone": BACKBONE,
                "backbone_revision": BACKBONE_REVISION,
                "sha256": hashes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Pinned model and local tokenizer provisioned in models/gliner")


if __name__ == "__main__":
    main()
