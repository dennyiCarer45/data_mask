"""Send one input to the running gateway from inside the private Compose network.

    docker run --rm --network llm-privacy-gateway_private --env-file .env \
        -v "$PWD:/app" -w /app local-privacy-gateway:dev \
        python scripts/try_input.py samples/demo-run/input/hospital-invoice.pdf --category invoices

Pass a file path (PDF, image, DOCX, TXT, JSON) or --text "...". Synthetic data only.
"""

import argparse
import json
import os
from pathlib import Path

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?")
    parser.add_argument("--text")
    parser.add_argument("--category", default="general")
    args = parser.parse_args()
    if (args.file is None) == (args.text is None):
        parser.error("give a file or --text")

    headers = {"Authorization": f"Bearer {os.environ['PRIVACY_API_KEY']}"}
    with httpx.Client(base_url=GATEWAY, headers=headers, timeout=120) as client:
        if args.text is not None:
            body = {"category": args.category, "text": args.text}
            response = client.post("/mask/text", json=body)
        else:
            path = Path(args.file)
            response = client.post(
                "/mask/document",
                data={"category": args.category},
                files={"file": (path.name, path.read_bytes())},
            )
    result = response.json()
    if "sanitized_text" in result:
        print(result["sanitized_text"])
        print(f"\n--- {result['masked_spans']} values masked, detector {result['detector']}")
    else:
        print(response.status_code, json.dumps(result))


if __name__ == "__main__":
    main()
