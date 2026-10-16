"""Small reproducible synthetic benchmark, NOT a safety certification."""

import argparse
import json
import statistics
import time
from pathlib import Path

from privacy_gateway.detection import DetectionEngine, GLiNERDetector, RuleDetector
from privacy_gateway.errors import PrivacyError
from privacy_gateway.pipeline import Pipeline
from privacy_gateway.policy import Policies

CASES = [
    {
        "text": "Patient: Mira Vale\nMRN: MRN-45821\nPhone: 9876543210\nFollow-up scheduled.",
        "sensitive": [("Mira Vale", "PERSON"), ("MRN-45821", "MRN"), ("9876543210", "PHONE")],
        "retain": ["Follow-up scheduled."],
        "category": "healthcare",
    },
    {
        "text": "Contact mila@example.test for the public schedule.",
        "sensitive": [("mila@example.test", "EMAIL")],
        "retain": ["public schedule"],
        "category": "general",
    },
    {
        "text": "Account: 123456789012\nAmount: USD 420.00\nInvoice issued.",
        "sensitive": [("123456789012", "BANK_ACCOUNT"), ("USD 420.00", "AMOUNT")],
        "retain": ["Invoice issued."],
        "category": "invoices",
    },
    {
        "text": "Patient: Mira Vale\nDiagnosis: Diabetes\nFollow-up recommended.",
        "sensitive": [("Mira Vale", "PERSON"), ("Diabetes", "DIAGNOSIS")],
        "retain": ["Follow-up recommended."],
        "category": "healthcare",
    },
    {
        "text": "Project Cedar documentation is ready.",
        "sensitive": [("Project Cedar", "PROJECT")],
        "retain": ["documentation is ready."],
        "category": "guidelines",
    },
    {
        "text": "The publicly available handbook explains the review process.",
        "sensitive": [],
        "retain": ["review process"],
        "category": "general",
    },
    # Unlabeled unusual names expose the rule-only baseline's recall limitation.
    {
        "text": "Zorina Quell requested a follow-up.",
        "sensitive": [("Zorina Quell", "PERSON")],
        "retain": ["requested a follow-up."],
        "category": "general",
    },
    {
        "text": "Ship to 73 Wren Avenue tomorrow.",
        "sensitive": [("73 Wren Avenue", "ADDRESS")],
        "retain": ["tomorrow."],
        "category": "general",
    },
]


def evaluate(mode):
    policies = Policies(Path("config/policies.yaml"))
    model = GLiNERDetector(Path("models/gliner"), 0.35) if mode == "gliner" else None
    engine = DetectionEngine(RuleDetector(policies.config.custom_terms), model)
    pipeline = Pipeline(engine, policies)
    tp = fp = fn = leaks = total = retained = retention_total = approved = blocked = 0
    times = []
    approved_sensitive_values = 0
    for case in CASES:
        text = case["text"]
        gold = {
            (text.index(value), text.index(value) + len(value), kind)
            for value, kind in case["sensitive"]
        }
        predicted = {(e.start, e.end, e.entity_type) for e in engine.detect(text)}
        tp += len(gold & predicted)
        fp += len(predicted - gold)
        fn += len(gold - predicted)
        total += len(gold)
        started = time.perf_counter()
        try:
            sanitized, _, context = pipeline.sanitize([text], case["category"])
            output = sanitized[0]
            approved += 1
            approved_sensitive_values += len(gold)
            context.clear()
        except PrivacyError:
            output = ""
            blocked += 1
        times.append((time.perf_counter() - started) * 1000)
        leaks += sum(value.casefold() in output.casefold() for value, _ in case["sensitive"])
        retained += sum(value in output for value in case["retain"])
        retention_total += len(case["retain"])
    return {
        "detector": mode,
        "synthetic_records": len(CASES),
        "true_positives": tp,
        "false_positives": fp,
        "missed_identifiers": fn,
        "precision_exact_span_and_type": tp / max(tp + fp, 1),
        "recall_exact_span_and_type": tp / max(tp + fn, 1),
        "sensitive_values": total,
        "leaked_values": leaks,
        "leakage_rate": leaks / max(total, 1),
        "sensitive_value_removal_rate_including_blocks": 1 - leaks / max(total, 1),
        "approved_sensitive_values": approved_sensitive_values,
        "masking_accuracy_approved_values": 1 - leaks / max(approved_sensitive_values, 1),
        "approved_records": approved,
        "blocked_records": blocked,
        "context_phrase_preservation": retained / max(retention_total, 1),
        "masking_pipeline_latency_ms_mean": statistics.mean(times),
        "masking_pipeline_latency_ms_max": max(times),
        "limitations": "Eight English synthetic cases, no confidence interval; correlated validators; "
        "blocked records count as removal, not successful masking; no zero-leakage claim.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["rules", "gliner"], default="rules")
    parser.add_argument("--output", default="docs/evaluation-rules.json")
    args = parser.parse_args()
    result = evaluate(args.mode)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
