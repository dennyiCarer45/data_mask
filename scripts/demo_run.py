"""Run sample inputs through the running gateway and save every input and output as files.

    docker run --rm --network llm-privacy-gateway_private --env-file .env \
        -e GATEWAY_URL=http://gateway:8000 -e PYTHONPATH=/app:/app/scripts \
        -v "$PWD:/app" -w /app local-privacy-gateway:dev python scripts/demo_run.py

Writes samples/demo-run/input/* and samples/demo-run/output/<name>/*. To run one file you put in
samples/demo-run/input yourself, append its name and category, e.g.
`python scripts/demo_run.py invoice1_modern.pdf --category invoices`. Synthetic data only.
"""

import argparse
import base64
import csv
import json
import os
from pathlib import Path

import httpx
import pymupdf
from evaluate_documents import render_image

ROOT = Path("samples/demo-run")
QUESTION = "Summarise this document in three bullet points."


def make_inputs(folder):
    folder.mkdir(parents=True, exist_ok=True)
    inputs = {}
    # PDFs from the repository samples (fictional), plus a scanned (image-only) copy of one.
    for name in ["hospital-invoice.pdf", "Sample_Agreement.pdf"]:
        (folder / name).write_bytes(Path("samples", name).read_bytes())
    with pymupdf.open("samples/hospital-invoice.pdf") as source, pymupdf.open() as scan:
        for page in source:
            pix = page.get_pixmap(dpi=150)
            target = scan.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
        (folder / "hospital-invoice-scanned.pdf").write_bytes(scan.tobytes())
    # Image: phone photo of a synthetic ID card.
    card = (
        "Government of India\nMira Vale\nDOB: 14/04/1993\nS/O Ravi Vale\nFemale\n"
        "2345 6789 0123\nAadhaar - proof of identity"
    )
    (folder / "aadhaar-card-photo.jpg").write_bytes(render_image(card, photo=True))
    # Raw data: plain text and JSON.
    (folder / "raw-note.txt").write_text(
        "Patient: Mira Vale\nMRN: MRN-45821\nPhone: +91 98765 43210\n"
        "Diagnosis: Type 2 diabetes\nEmail mira.vale@example.test with the follow-up plan.\n",
        encoding="utf-8",
    )
    (folder / "raw-record.json").write_text(
        json.dumps(
            {
                "customer_name": "Owen Pratley",
                "account": "GB29 NWBK 6016 1331 9268 19",
                "amount": "USD 420.00",
                "status": "paid",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    inputs = {
        "hospital-invoice.pdf": "invoices",
        "hospital-invoice-scanned.pdf": "invoices",
        "Sample_Agreement.pdf": "agreements",
        "aadhaar-card-photo.jpg": "general",
        "raw-note.txt": "healthcare",
        "raw-record.json": "invoices",
    }
    return inputs


def save(folder, name, category, client, headers):
    out = folder / Path(name).stem
    out.mkdir(parents=True, exist_ok=True)
    # Files from an earlier run (e.g. old findings) would otherwise look current.
    for old in out.iterdir():
        old.unlink()
    content = (ROOT / "input" / name).read_bytes()
    files = {"file": (name, content)}
    if name.startswith("raw-"):
        # Raw data goes through the JSON API, as an application would send it.
        body = {"category": category}
        if name.endswith(".json"):
            body["data"] = json.loads(content)
        else:
            body["text"] = content.decode("utf-8")
        response = client.post("/preview/text", headers=headers, json=body)
    else:
        response = client.post(
            "/preview/document", headers=headers, files=files, data={"category": category}
        )
    if response.status_code != 200:
        (out / "error.json").write_text(response.text, encoding="utf-8")
        return {"input": name, "category": category, "result": response.json()}
    preview = response.json()
    (out / "1-extracted-original.txt").write_text(preview["original_text"], encoding="utf-8")
    (out / "2-masked-sent-to-ai.txt").write_text(preview["sanitized_text"], encoding="utf-8")
    with open(out / "3-replacements.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["original", "placeholder", "type", "action", "found_by", "confidence"])
        for span in preview["spans"]:
            writer.writerow(
                [
                    preview["original_text"][span["start"] : span["end"]],
                    span["token"],
                    span["entity_type"],
                    span["action"],
                    " ".join(span["sources"]),
                    span["confidence"],
                ]
            )
    findings = preview["validation"]["findings"]
    if findings:
        with open(
            out / "3b-still-detected-after-masking.csv", "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["masked_text_at_spot", "type", "reason", "found_by", "confidence"])
            for item in findings:
                snippet = preview["sanitized_text"][item["masked_start"] : item["masked_end"]]
                writer.writerow(
                    [
                        snippet,
                        item["entity_type"],
                        item["reason"],
                        item["source"],
                        item["confidence"],
                    ]
                )
    document = preview["masked_document"]
    if document:
        suffix = ".pdf" if document["media_type"] == "application/pdf" else ".png"
        masked = base64.b64decode(document["base64"])
        (out / f"4-masked-copy{suffix}").write_bytes(masked)
        if suffix == ".pdf":
            # PNG of each page for quick viewing.
            with pymupdf.open(stream=masked, filetype="pdf") as doc:
                for number, page in enumerate(doc, 1):
                    page.get_pixmap(dpi=110).save(out / f"4-masked-copy-page{number}.png")
            with pymupdf.open(stream=content, filetype="pdf") as doc:
                for number, page in enumerate(doc, 1):
                    page.get_pixmap(dpi=110).save(out / f"0-original-page{number}.png")
    summary = {
        "input": name,
        "category": category,
        "masked_values": preview["summary"]["masked_spans"],
        "by_type": preview["summary"]["by_type"],
        "blocked_types": preview["summary"]["blocked_types"],
        "sendable": preview["sendable"],
        "still_detected_after_masking": len(findings),
        "ocr": preview["ocr"],
        "unlocated_in_masked_copy": document["unlocated_spans"] if document else None,
    }
    if preview["sendable"]:
        if name.startswith("raw-"):
            messages = [
                {"role": "user", "content": preview["original_text"]},
                {"role": "user", "content": QUESTION},
            ]
            sent = client.post(
                "/llm/chat",
                headers=headers,
                json={
                    "category": category,
                    "messages": messages,
                    "approved_preview_sha256": preview["preview_sha256"],
                },
            )
        else:
            sent = client.post(
                "/llm/document",
                headers=headers,
                files=files,
                data={
                    "category": category,
                    "question": QUESTION,
                    "approved_preview_sha256": preview["preview_sha256"],
                },
            )
        (out / "5-ai-request-and-reply.json").write_text(
            json.dumps(sent.json(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        summary["ai_call"] = sent.status_code
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", help="one file already in samples/demo-run/input")
    parser.add_argument("--category", default="general")
    args = parser.parse_args()
    headers = {"Authorization": "Bearer " + os.environ["PRIVACY_API_KEY"]}
    url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
    if args.file:
        # Only this file; the generated inputs and the combined summary are left as they are.
        name = Path(args.file).name
        if not (ROOT / "input" / name).is_file():
            parser.error(f"put the file in {ROOT / 'input'} first")
        inputs = {name: args.category}
    else:
        inputs = make_inputs(ROOT / "input")
    results = []
    with httpx.Client(base_url=url, timeout=600, trust_env=False) as client:
        print("gateway:", client.get("/health").json())
        for name, category in inputs.items():
            results.append(save(ROOT / "output", name, category, client, headers))
            print(json.dumps(results[-1]))
    if not args.file:
        (ROOT / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
