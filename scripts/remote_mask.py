"""Send a document to a running gateway's LAN relay and print what the AI would receive.

Any OS with Python 3.9+; standard library only. Synthetic data only.

  python scripts/remote_mask.py invoice.pdf --category invoices
  python scripts/remote_mask.py invoice.pdf --host 192.168.1.17 --user ds

The API key comes from PRIVACY_API_KEY, or is read once over SSH from the server's .env (never
printed or saved). The server's certificate is fetched over SSH on first use and kept in
~/gateway-cert.pem. Output shows placeholders and types only, never the original values.
"""

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CATEGORIES = ["general", "healthcare", "invoices", "agreements", "guidelines"]


def ssh(target: str, command: str) -> str:
    try:
        result = subprocess.run(
            ["ssh", target, command], capture_output=True, text=True, check=True
        )
    except FileNotFoundError:
        sys.exit("ssh not found: set PRIVACY_API_KEY and pass --cert instead.")
    except subprocess.CalledProcessError:
        sys.exit(f"ssh to {target} failed: check the address, user, and project path.")
    return result.stdout


def multipart(fields: dict, file_path: Path):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="PDF, DOCX, TXT, JSON, PNG, JPG, or WEBP")
    parser.add_argument("--category", default="general", choices=CATEGORIES)
    parser.add_argument("--host", default="192.168.1.17", help="server address")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--user", default="ds", help="SSH user on the server")
    parser.add_argument(
        "--project", default="~/projects/datasmasking", help="project path on the server"
    )
    parser.add_argument("--cert", type=Path, default=Path.home() / "gateway-cert.pem")
    parser.add_argument("--save-copy", action="store_true", help="save the masked PDF/PNG copy")
    args = parser.parse_args()

    if not args.file.is_file():
        sys.exit(f"File not found: {args.file}")
    target = f"{args.user}@{args.host}"
    if not args.cert.is_file():
        pem = ssh(target, f"cat {args.project}/.runtime/lan-tls/cert.pem")
        if "BEGIN CERTIFICATE" not in pem:
            sys.exit("Could not read the server certificate.")
        args.cert.write_text(pem, encoding="ascii")
    key = (
        os.environ.get("PRIVACY_API_KEY")
        or ssh(target, f"grep '^PRIVACY_API_KEY=' {args.project}/.env | cut -d= -f2-").strip()
    )
    if len(key) < 32:
        sys.exit("No API key: set PRIVACY_API_KEY.")

    body, content_type = multipart({"category": args.category}, args.file)
    request = urllib.request.Request(
        f"https://{args.host}:{args.port}/preview/document",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
    )
    context = ssl.create_default_context(cafile=str(args.cert))
    try:
        with urllib.request.urlopen(request, context=context, timeout=600) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get("error", error.code)
        except Exception:
            code = error.code
        sys.exit(f"Refused: {code}")
    except urllib.error.URLError as error:
        sys.exit(f"Cannot reach https://{args.host}:{args.port}: {error.reason}")

    print("===== MASKED TEXT (what the AI receives) =====")
    print(result["sanitized_text"])
    print("\n===== REPLACEMENTS =====")
    for span in result["spans"]:
        print(f"{span['token']:<28} {span['entity_type']:<22} {span['action']}")
    summary = result["summary"]
    blocked = ", ".join(summary["blocked_types"]) or "-"
    print(
        f"\nMasked values: {summary['masked_spans']}   Blocked: {blocked}   Sendable: {result['sendable']}"
    )
    document = result.get("masked_document")
    if args.save_copy and document:
        suffix = ".pdf" if document["media_type"] == "application/pdf" else ".png"
        path = args.file.with_name(args.file.stem + "-masked" + suffix)
        path.write_bytes(base64.b64decode(document["base64"]))
        print(f"Masked copy saved: {path}")


if __name__ == "__main__":
    main()
