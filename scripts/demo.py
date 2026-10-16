"""Try the running gateway from inside its container. Reads text or a document from stdin.

docker compose exec -T gateway python scripts/demo.py mask healthcare < note.txt
docker compose exec -T gateway python scripts/demo.py document invoices --name bill.pdf < bill.pdf
docker compose exec -T gateway python scripts/demo.py chat general < question.txt
docker compose exec -T gateway python scripts/demo.py chat invoices --question "Summarise it." < bill.txt
docker compose exec -T gateway python scripts/demo.py chat invoices --name bill.pdf --question "..." < bill.pdf

For chat, PDF/DOCX/JSON are extracted here with the gateway's own local extractor; the extracted
original text then goes through /llm/chat, which performs all masking and validation.
"""

import argparse
import json
import os
import sys

import httpx

from privacy_gateway.documents import DocumentProcessor
from privacy_gateway.errors import PrivacyError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["detect", "mask", "document", "chat"])
    parser.add_argument(
        "category", choices=["general", "healthcare", "invoices", "agreements", "guidelines"]
    )
    parser.add_argument("--name", default="input.txt", help="file name for document uploads")
    parser.add_argument("--question", help="chat: send this as a separate message after stdin")
    parser.add_argument("--max-tokens", type=int, default=512, help="chat: reply length limit")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read()
    headers = {"Authorization": "Bearer " + os.environ["PRIVACY_API_KEY"]}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=300, trust_env=False) as client:
        if args.action == "document":
            response = client.post(
                "/mask/document",
                headers=headers,
                files={"file": (args.name, raw)},
                data={"category": args.category},
            )
        elif args.action == "chat":
            try:
                content = DocumentProcessor().extract(raw, args.name)
            except PrivacyError as error:
                print(error.status)
                print(json.dumps({"error": error.code}, indent=2))
                return
            messages = [{"role": "user", "content": content}]
            if args.question:
                messages.append({"role": "user", "content": args.question})
            body = {
                "category": args.category,
                "messages": messages,
                "max_tokens": args.max_tokens,
            }
            response = client.post("/llm/chat", headers=headers, json=body)
        else:
            body = {"category": args.category, "text": raw.decode("utf-8")}
            response = client.post(f"/{args.action}/text", headers=headers, json=body)
    print(response.status_code)
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
