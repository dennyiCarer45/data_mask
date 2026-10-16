"""Run three long, unlabelled conversation summaries (TXT, JSON, PDF) through the gateway.

    docker run --rm --network llm-privacy-gateway_private --env-file .env \
        -e GATEWAY_URL=http://gateway:8000 -e PYTHONPATH=/app:/app/scripts \
        -v "$PWD:/app" -w /app local-privacy-gateway:dev python scripts/conversation_demo.py

Writes samples/conversation-demo/input/* and output/<name>/*. Synthetic data only: every name,
number, and organization below is fictional.
"""

import json
import os
from pathlib import Path

import demo_run
import httpx
import pymupdf

ROOT = Path("samples/conversation-demo")

CALL_SUMMARY = """\
Summary of the follow-up call held on Tuesday afternoon.

The call opened with Asha Rao confirming that she had received the discharge papers from \
Lakeview Memorial Hospital. She explained that her husband, Vikram Rao, had been admitted last \
week after a fall at home and that the ward doctors had since diagnosed him with type 2 diabetes \
and early stage chronic kidney disease. Asha said the family had found the first few days \
difficult because Vikram was not used to managing his own medication.

Asha asked whether the home-care nurse could visit them at 14 Hillcrest Lane, Koramangala, \
Bengaluru 560034 rather than at her sister's flat, since Vikram would be staying at home for \
the next month. She also mentioned that their daughter, Priya, would be the main contact on \
weekdays and could be reached on 98450 12345 or at priya.rao@example.test.

Later in the call Mrs. Rao raised a billing concern. The hospital had charged INR 48,250.00 for \
the admission, but the insurer, Bluepeak Health Insurance, had so far only approved part of it. \
She read out the policy member number MRN-778213 and asked us to chase the claim.

Before closing, Dr. Kavya Menon from the care coordination team joined briefly and agreed to \
review Vikram's insulin plan. Asha thanked everyone and asked that all future letters be sent \
to the Hillcrest Lane address. Next steps: Kavya to call Priya on Friday; the billing team to \
follow up with Bluepeak; Asha to share the latest blood test results by email.
"""

CHAT_RECORD = {
    "ticket": "CS-20431",
    "channel": "phone",
    "summary": (
        "The customer, Daniel Okafor, called because a refund had not reached his account. He "
        "said he had cancelled his Northwind Telecom contract in August and was promised a "
        "refund of $312.40. Daniel read out his bank account GB82 WEST 1234 5698 7654 32 and "
        "asked us to confirm it was the one on file. He also gave a new mobile number, "
        "+44 7700 900123, and asked that we stop using his old email daniel.o@example.test. "
        "During the call Mr. Okafor mentioned he had moved to 7 Ferry Road, Leith, Edinburgh "
        "EH6 4AE. His partner, Grace Bello, is authorised to speak on the account. Daniel was "
        "polite but frustrated, and asked for a supervisor callback within two working days."
    ),
    "turns": [
        {"speaker": "agent", "text": "Thanks for calling, how can I help today?"},
        {
            "speaker": "customer",
            "text": "Hi, this is Daniel. I am still waiting for my refund from August.",
        },
        {"speaker": "agent", "text": "I am sorry about that. Let me check the account."},
        {
            "speaker": "customer",
            "text": "Grace already called last week and was told it was processed.",
        },
    ],
    "resolution": "escalated",
}

MEETING_SUMMARY = """\
Meeting summary - partner review

Attendees: our account team and two representatives from Halden Logistics.

Tomas Lindqvist opened by saying Halden Logistics was happy with delivery times but wanted \
clearer reporting. His colleague, Mei Tanaka, explained that their finance lead had queried \
three invoices from the last quarter, the largest being EUR 18,900.00. Tomas asked whether \
Project Cedar could include a shared dashboard so both sides could see shipment status.

Mei said she could be contacted directly at mei.tanaka@halden.example.test or on \
+46 70 123 45 67, and that invoices should now go to their new office at 22 Sveavagen, \
Stockholm 113 50. She also noted that Tomas would be on leave for most of December, so \
she would approve any changes in his absence.

Action items: our team to send a draft dashboard to Mei; Tomas to confirm the reporting fields \
before he leaves; both sides to meet again in January.
"""

INPUTS = {
    "raw-call-summary.txt": "healthcare",
    "raw-chat-record.json": "general",
    "meeting-summary.pdf": "general",
}


def make_inputs(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "raw-call-summary.txt").write_text(CALL_SUMMARY, encoding="utf-8")
    (folder / "raw-chat-record.json").write_text(
        json.dumps(CHAT_RECORD, indent=2), encoding="utf-8"
    )
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox(page.rect + (60, 60, -60, -60), MEETING_SUMMARY, fontsize=11)
        (folder / "meeting-summary.pdf").write_bytes(doc.tobytes())


def main():
    headers = {"Authorization": "Bearer " + os.environ["PRIVACY_API_KEY"]}
    url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
    # demo_run.save reads ROOT/input and sends raw-* files through the JSON text API.
    demo_run.ROOT = ROOT
    make_inputs(ROOT / "input")
    results = []
    with httpx.Client(base_url=url, timeout=600, trust_env=False) as client:
        print("gateway:", client.get("/health").json())
        for name, category in INPUTS.items():
            results.append(demo_run.save(ROOT / "output", name, category, client, headers))
            print(json.dumps(results[-1]))
    (ROOT / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
