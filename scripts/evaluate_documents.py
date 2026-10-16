"""Masking accuracy per input type: the same synthetic records as raw text, text PDF, scanned
PDF, clean image, and phone-style photo. NOT a safety certification.

Run inside the provided Docker image (Tesseract, PyMuPDF, Pillow, and optionally GLiNER):
    python scripts/evaluate_documents.py --mode gliner --output docs/evaluation-documents-gliner.json
"""

import argparse
import difflib
import io
import json
import re
import statistics
import time
from pathlib import Path

from evaluate import CASES

from privacy_gateway.detection import DetectionEngine, GLiNERDetector, RuleDetector
from privacy_gateway.documents import DocumentProcessor
from privacy_gateway.errors import PrivacyError
from privacy_gateway.ocr import TesseractOCR
from privacy_gateway.pipeline import Pipeline
from privacy_gateway.policy import Policies
from privacy_gateway.redaction import render_masked

# Synthetic Indian ID-card layouts. Names on real cards are often printed without a label.
ID_CASES = [
    {
        "text": "Government of India\nMira Vale\nDOB: 14/04/1993\nFemale\n2345 6789 0123\n"
        "Aadhaar - proof of identity",
        "sensitive": [
            ("Mira Vale", "PERSON"),
            ("14/04/1993", "DATE_OF_BIRTH"),
            ("2345 6789 0123", "GOVERNMENT_ID"),
        ],
        "retain": ["proof of identity"],
        "category": "general",
    },
    {
        "text": "Permanent Account Number Card\nABCDE1234F\nName\nRavi Kamath\nFather's Name\n"
        "Suresh Kamath\nDate of Birth\n02/11/1988\nSignature",
        "sensitive": [
            ("ABCDE1234F", "TAX_ID"),
            ("Ravi Kamath", "PERSON"),
            ("Suresh Kamath", "PERSON"),
            ("02/11/1988", "DATE_OF_BIRTH"),
        ],
        "retain": ["Permanent Account Number Card"],
        "category": "general",
    },
    {
        "text": "Election Commission of India\nElector Name: Asha Rao\nEPIC No: ABC1234567\n"
        "Address: 73 Wren Avenue, Pune 411001\nIdentity card",
        "sensitive": [
            ("Asha Rao", "PERSON"),
            ("ABC1234567", "GOVERNMENT_ID"),
            ("73 Wren Avenue, Pune 411001", "ADDRESS"),
        ],
        "retain": ["Identity card"],
        "category": "general",
    },
    {
        "text": "Driving Licence\nDL No: MH12 20110012345\nName: Owen Pratley\nS/O Peter Pratley\n"
        "Valid for motor cycle and light motor vehicle",
        "sensitive": [
            ("MH12 20110012345", "GOVERNMENT_ID"),
            ("Owen Pratley", "PERSON"),
            ("Peter Pratley", "PERSON"),
        ],
        "retain": ["light motor vehicle"],
        "category": "general",
    },
    {
        "text": "Republic of India Passport\nPassport No. K1234567\nSurname: Pratley\n"
        "Given Name: Owen\nDate of Birth: 21/07/1985\nPlace of issue: Mumbai",
        "sensitive": [
            ("K1234567", "GOVERNMENT_ID"),
            ("Pratley", "PERSON"),
            ("Owen", "PERSON"),
            ("21/07/1985", "DATE_OF_BIRTH"),
        ],
        "retain": ["Passport"],
        "category": "general",
    },
]
RENDERINGS = ["text", "pdf", "scanned_pdf", "image", "photo"]


def render_image(text, photo=False):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    lines = text.splitlines()
    font = ImageFont.load_default(size=38)
    image = Image.new("RGB", (1400, 80 + 64 * len(lines)), (236, 236, 230) if photo else "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((50, 40 + 64 * index), line, fill=(25, 25, 40), font=font)
    output = io.BytesIO()
    if photo:
        # A hand-held phone picture: slight tilt, soft focus, heavy JPEG compression.
        image = image.rotate(1.5, expand=True, fillcolor=(200, 200, 196))
        image = image.filter(ImageFilter.GaussianBlur(1.1))
        image.save(output, "JPEG", quality=55)
    else:
        image.save(output, "PNG")
    return output.getvalue()


def render(text, kind):
    import pymupdf

    if kind == "text":
        return text.encode(), "case.txt"
    if kind in ("image", "photo"):
        return render_image(text, kind == "photo"), "case.jpg" if kind == "photo" else "case.png"
    doc = pymupdf.open()
    page = doc.new_page()
    if kind == "pdf":
        page.insert_text((60, 72), text, fontsize=11)
    else:
        page.insert_image(pymupdf.Rect(30, 40, 580, 400), stream=render_image(text))
    content = doc.tobytes()
    doc.close()
    return content, "case.pdf"


def near_leak(value, output):
    """A sensitive value, or an OCR misreading of it (>= 80% similar), survived masking."""
    squash = lambda s: re.sub(r"\s+", "", s.casefold())  # noqa: E731
    value, output = squash(value), squash(output)
    if value in output:
        return True
    width = len(value)
    for size in range(max(1, width - 2), width + 3):
        for start in range(0, max(1, len(output) - size + 1)):
            window = output[start : start + size]
            if difflib.SequenceMatcher(None, value, window).ratio() >= 0.8:
                return True
    return False


def evaluate(mode):
    policies = Policies(Path("config/policies.yaml"))
    model = GLiNERDetector(Path("models/gliner"), 0.35) if mode == "gliner" else None
    engine = DetectionEngine(RuleDetector(policies.config.custom_terms), model)
    pipeline = Pipeline(engine, policies)
    ocr = TesseractOCR()
    documents = DocumentProcessor(ocr=ocr)
    results = {}
    for kind in RENDERINGS:
        stats = dict.fromkeys(
            [
                "records",
                "sensitive_values",
                "values_read_exactly",
                "literal_leaks",
                "near_leaks",
                "refused_records",
                "blocked_records",
                "unverified_words",
                "unlocated_in_review_copy",
                "retained_phrases",
                "retain_total",
            ],
            0,
        )
        times, leaked = [], []
        for case in CASES + ID_CASES:
            stats["records"] += 1
            stats["sensitive_values"] += len(case["sensitive"])
            stats["retain_total"] += len(case["retain"])
            content, name = render(case["text"], kind)
            started = time.perf_counter()
            try:
                extracted = documents.extract_document(content, name)
                spans = []
                sanitized, _, context = pipeline.sanitize(
                    [extracted.text], case["category"], [extracted.unverified], spans
                )
                context.clear()
                output = sanitized[0]
                if any(span["action"] == "block" for span in spans[0]):
                    stats["blocked_records"] += 1
                _, unlocated = render_masked(extracted, spans[0])
                stats["unlocated_in_review_copy"] += unlocated
                stats["unverified_words"] += len(extracted.unverified)
                read = extracted.text.casefold()
            except PrivacyError:
                stats["refused_records"] += 1
                output, read = "", ""
            times.append((time.perf_counter() - started) * 1000)
            for value, entity_type in case["sensitive"]:
                stats["values_read_exactly"] += value.casefold() in read
                literal = value.casefold() in output.casefold()
                near = near_leak(value, output)
                stats["literal_leaks"] += literal
                stats["near_leaks"] += near
                if near:
                    leaked.append({"type": entity_type, "literal": literal})
            stats["retained_phrases"] += sum(
                p.casefold() in output.casefold() for p in case["retain"]
            )
        stats["leaked_types"] = leaked
        stats["leak_rate_including_near"] = stats["near_leaks"] / stats["sensitive_values"]
        stats["context_phrase_preservation"] = stats["retained_phrases"] / stats["retain_total"]
        stats["latency_ms_mean"] = round(statistics.mean(times), 1)
        results[kind] = stats
    return {
        "detector": mode,
        "ocr": f"tesseract {ocr.version} ({ocr.languages}), min confidence {ocr.min_confidence}",
        "records_per_rendering": len(CASES) + len(ID_CASES),
        "results": results,
        "limitations": "13 English synthetic records, one generated font, no real scans or "
        "handwriting, no confidence interval. Blocked records still count leaks only if a value "
        "survives masking. Near leak = at least 80% character similarity, whitespace ignored. "
        "No zero-leakage claim.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["rules", "gliner"], default="rules")
    parser.add_argument("--output", default="docs/evaluation-documents-rules.json")
    args = parser.parse_args()
    result = evaluate(args.mode)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
