import json

import pytest

from privacy_gateway.detection import DetectionEngine, RuleDetector
from privacy_gateway.errors import PrivacyError
from privacy_gateway.masking import MaskContext, mask, propagate
from privacy_gateway.pipeline import Pipeline
from privacy_gateway.schemas import Entity
from privacy_gateway.validators import FailResult

SYNTHETIC = "Patient: John Smith\nPatient ID: MRN-45821\nPhone: 9876543210\nDiagnosis: Diabetes"


def test_healthcare_example(client):
    result = client.post("/mask/text", json={"text": SYNTHETIC, "category": "healthcare"})
    assert result.status_code == 200, result.text
    assert result.json()["sanitized_text"] == (
        "Patient: [PERSON_001]\nPatient ID: [MRN_001]\nPhone: [PHONE_001]\nDiagnosis: [HEALTH_CONDITION]"
    )
    assert "John Smith" not in result.text and "mapping" not in result.text


def test_reference_propagation_keeps_detector_confidence(policies):
    text = "Phone: 9876543210"
    detections = [
        [
            Entity(start=7, end=17, entity_type="ADDRESS", confidence=0.63, source="gliner"),
            Entity(start=7, end=17, entity_type="PHONE", confidence=1.0, source="context"),
        ]
    ]
    propagate([text], detections)
    masked, _ = mask(text, detections[0], "healthcare", policies, MaskContext())
    assert masked == "Phone: [PHONE_001]"


def test_detect_returns_offsets_not_values(client):
    response = client.post("/detect/text", json={"text": "Reach alice@example.test"})
    assert response.status_code == 200
    assert any(e["entity_type"] == "EMAIL" and e["start"] == 6 for e in response.json()["entities"])
    assert "alice@example.test" not in response.text


def test_cross_message_consistency_and_history(client, spy):
    response = client.post(
        "/llm/chat",
        json={
            "messages": [
                {"role": "user", "content": "Patient: John Smith"},
                {"role": "assistant", "content": "John Smith needs a follow-up."},
                {"role": "user", "content": "Please summarize John Smith's notes."},
            ]
        },
    )
    assert response.status_code == 200, response.text
    serialized = json.dumps(spy.calls)
    assert "John Smith" not in serialized
    assert serialized.count("[PERSON_001]") == 3
    assert "follow-up" in response.text


@pytest.mark.parametrize(
    "text",
    [
        "Password: synthetic-password",
        "Protected terms: 12% rebate",
        "Internal strategy: unreleased expansion",
        "confidential roadmap",
    ],
)
def test_block_prevents_transport(client, spy, text):
    response = client.post("/llm/chat", json={"messages": [{"role": "user", "content": text}]})
    assert response.status_code == 422
    assert not spy.calls
    assert text not in response.text


@pytest.mark.parametrize(
    "extra",
    [
        {"stream": True},
        {"model": "bypass"},
        {"tools": []},
        {"api_base": "https://invalid.test"},
        {"metadata": {}},
        {"extra_body": {"messages": ["raw"]}},
    ],
)
def test_bypass_fields_rejected(client, spy, extra):
    payload = {"messages": [{"role": "user", "content": SYNTHETIC}], **extra}
    response = client.post("/llm/chat", json=payload)
    assert response.status_code == 422 and not spy.calls
    assert "John Smith" not in response.text


def test_unauthorized_never_echoes(client, spy):
    response = client.post(
        "/llm/chat",
        headers={"Authorization": "Bearer wrong"},
        json={"messages": [{"role": "user", "content": SYNTHETIC}]},
    )
    assert response.status_code == 401 and not spy.calls
    assert "John Smith" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": ""},
        {"text": SYNTHETIC, "category": "unsafe"},
        {"text": SYNTHETIC, "data": {}},
        {"text": 9876543210},
    ],
)
def test_validation_errors_do_not_echo(client, payload):
    result = client.post("/mask/text", json=payload)
    assert result.status_code == 422
    assert result.json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    "output", ["Contact alice@example.test", "Patient: New Person", "John Smith", "[PERSON_999]"]
)
def test_output_blocked(client, spy, output):
    spy.response = output
    result = client.post("/llm/chat", json={"messages": [{"role": "user", "content": SYNTHETIC}]})
    assert len(spy.calls) == 1
    assert result.status_code == 422
    assert output not in result.text


def test_fail_closed_validator(client, spy, monkeypatch):
    validator = client.app.state.pipeline.input_validator
    monkeypatch.setattr(validator, "validate", lambda *_: FailResult(error_message="synthetic"))
    response = client.post("/llm/chat", json={"messages": [{"role": "user", "content": SYNTHETIC}]})
    assert response.status_code == 422 and not spy.calls


def test_detector_failure_no_outbound_or_trace(client, spy, monkeypatch, caplog):
    def broken(_):
        raise RuntimeError("John Smith alice@example.test")

    monkeypatch.setattr(client.app.state.pipeline.engine.rules, "detect", broken)
    response = client.post("/llm/chat", json={"messages": [{"role": "user", "content": SYNTHETIC}]})
    assert response.status_code == 503 and not spy.calls
    assert "John Smith" not in response.text + caplog.text


def test_provider_error_no_echo_or_log(client, spy, caplog):
    spy.error = RuntimeError("John Smith alice@example.test")
    response = client.post("/llm/chat", json={"messages": [{"role": "user", "content": SYNTHETIC}]})
    assert response.status_code == 503
    assert "John Smith" not in response.text + caplog.text


def test_guardrails_local_no_metrics(pipeline):
    assert pipeline.input_validator.use_local is True
    assert pipeline.input_validator._disable_telemetry is True


def test_overlap_union_does_not_leave_suffix(policies):
    text = "ABCDEFGHIJK"
    entities = [
        Entity(start=0, end=6, entity_type="PERSON", confidence=0.9, source="test"),
        Entity(start=4, end=11, entity_type="ADDRESS", confidence=0.8, source="test"),
    ]
    context = MaskContext()
    output, count = mask(text, entities, "general", policies, context)
    assert output == "[PERSON_001]" and count == 1
    assert "GHIJK" not in output


def test_block_wins_over_overlap(policies):
    entities = [
        Entity(start=0, end=5, entity_type="PERSON", confidence=1.0, source="test"),
        Entity(start=3, end=7, entity_type="SECRET", confidence=0.1, source="test"),
    ]
    with pytest.raises(PrivacyError, match="policy_blocked"):
        mask("ABCDEFG", entities, "general", policies, MaskContext())


def test_explicit_allow_policy_preserves_diagnosis(pipeline, policies):
    policies.config.categories["healthcare"]["DIAGNOSIS"] = "allow"
    masked, _, context = pipeline.sanitize([SYNTHETIC], "healthcare")
    assert "Diagnosis: Diabetes" in masked[0]
    context.clear()


def test_request_local_mapping(pipeline):
    a, _, ca = pipeline.sanitize(["Patient: John Smith"], "general")
    b, _, cb = pipeline.sanitize(["Patient: Mira Vale"], "general")
    assert a == b
    assert ca.originals != cb.originals
    ca.clear()
    assert not ca.originals and not ca.replacements and cb.originals


def test_reserved_placeholder_injection(client):
    response = client.post("/mask/text", json={"text": "Patient: [PERSON_001]"})
    assert response.status_code == 422


def test_sensitive_json_keys_and_numbers(client):
    response = client.post(
        "/mask/text",
        json={
            "data": {"patient_name": "Mira Vale", "phone": 9876543210, "alice@example.test": "note"}
        },
    )
    assert response.status_code == 200, response.text
    for value in ["Mira Vale", "9876543210", "alice@example.test"]:
        assert value not in response.text


def test_duplicate_json_fields_rejected(client):
    result = client.post(
        "/mask/text",
        content='{"text":"first","text":"Patient: John Smith"}',
        headers={"Content-Type": "application/json"},
    )
    assert result.status_code == 422
    assert "John Smith" not in result.text


def test_request_size_limit(client):
    response = client.post("/mask/text", content=b"x" * 6_000_001)
    assert response.status_code == 413


def test_no_hidden_fallback():
    class BrokenModel:
        def detect(self, text):
            raise ValueError("model failure")

    with pytest.raises(PrivacyError, match="detector_unavailable"):
        DetectionEngine(RuleDetector(), BrokenModel()).detect("some text")


def test_model_field_label_exemption_is_narrow():
    class LabelModel:
        def detect(self, text):
            return [
                Entity(start=0, end=7, entity_type="PERSON", confidence=0.9, source="gliner"),
                Entity(start=9, end=16, entity_type="PERSON", confidence=0.9, source="gliner"),
            ]

    result = DetectionEngine(RuleDetector(), LabelModel()).detect("Patient: Pattern")
    assert not any(e.start == 0 for e in result)
    assert any(e.start == 9 and e.source == "gliner" for e in result)
    # A value spelled like a generic word is dropped from model hits but still masked by rules.
    rules_only = DetectionEngine(RuleDetector(), LabelModel()).detect("Patient: Patient")
    assert any(e.start == 9 and e.source == "context" for e in rules_only)


class SpanModel:
    """Synthetic stand-in for GLiNER that tags fixed substrings."""

    def __init__(self, *spans):
        self.spans = spans

    def detect(self, text):
        found = []
        for piece, kind in self.spans:
            start = text.find(piece)
            if start >= 0:
                found.append(
                    Entity(
                        start=start,
                        end=start + len(piece),
                        entity_type=kind,
                        confidence=0.6,
                        source="gliner",
                    )
                )
        return found


def test_generic_model_words_are_not_entities():
    model = SpanModel(("her", "PERSON"), ("All staff", "PERSON"), ("Ravi Menon", "PERSON"))
    text = "All staff should thank her and Ravi Menon."
    result = DetectionEngine(RuleDetector(), model).detect(text)
    assert [text[e.start : e.end] for e in result] == ["Ravi Menon"]


def test_generic_filter_never_skips_rules():
    model = SpanModel(("customer", "PERSON"))
    result = DetectionEngine(RuleDetector(), model).detect("Customer name: Payment Team")
    assert any(e.source == "context" and e.entity_type == "PERSON" for e in result)


def test_validator_ignores_labels_beside_placeholders(policies):
    model = SpanModel(("Email", "EMAIL"), ("Our [PERSON_001]", "ORGANIZATION"))
    pipeline = Pipeline(DetectionEngine(RuleDetector(), model), policies)
    context = MaskContext(tokens={"[PERSON_001]", "[EMAIL_001]"})
    pipeline.validate("Email: [EMAIL_001]\nOur [PERSON_001] agreed.", "general", context)


def test_validator_catches_partial_leak_beside_placeholder(policies):
    model = SpanModel(("[PERSON_001] Okafor", "PERSON"))
    pipeline = Pipeline(DetectionEngine(RuleDetector(), model), policies)
    context = MaskContext(tokens={"[PERSON_001]"})
    with pytest.raises(PrivacyError, match="input_validation_failed"):
        pipeline.validate("[PERSON_001] Okafor will attend.", "general", context)


def test_document_numbers_are_not_phones_or_accounts():
    model = SpanModel(("INV-2026-00417", "BANK_ACCOUNT"), ("PO-44718", "ORGANIZATION"))
    text = "Invoice INV-2026-00417, PO-44718. Call 9876501234."
    result = DetectionEngine(RuleDetector(), model).detect(text)
    assert [text[e.start : e.end] for e in result] == ["9876501234"]


def test_document_number_exemption_is_narrow():
    model = SpanModel(("Invoice INV-30418", "PERSON"))
    text = "Invoice INV-30418\nAccount: INV-77\nRef INV-123-45-6789"
    found = {
        (text[e.start : e.end], e.entity_type)
        for e in DetectionEngine(RuleDetector(), model).detect(text)
    }
    # Model spans reaching outside the number, context labels, and ID shapes still count.
    assert ("Invoice INV-30418", "PERSON") in found
    assert ("INV-77", "BANK_ACCOUNT") in found
    assert ("123-45-6789", "GOVERNMENT_ID") in found


def test_qualified_name_labels_use_their_own_type():
    text = "Project name: Orion Vault\nCompany name: Larkspur Mills\nFull name: Ines Calloway"
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert found == {
        ("Orion Vault", "PROJECT"),
        ("Larkspur Mills", "ORGANIZATION"),
        ("Ines Calloway", "PERSON"),
    }


def test_invoice_line_items_and_total_labels_are_not_entities():
    model = SpanModel(("Consulting services", "ORGANIZATION"), ("Total due", "AMOUNT"))
    text = "Consulting services - 12 hours\nTotal due: EUR 1,250.00"
    result = DetectionEngine(RuleDetector(), model).detect(text)
    assert {(text[e.start : e.end], e.source) for e in result} == {
        ("EUR 1,250.00", "regex"),
        ("EUR 1,250.00", "context"),
    }


def test_output_check_accepts_placeholder_closing_a_labeled_value(policies):
    pipeline = Pipeline(DetectionEngine(RuleDetector()), policies)
    context = MaskContext(tokens={"[MRN_001]", "[PERSON_001]"})
    pipeline.validate("Patient ID: [MRN_001]) seen on 15 Oct.", "healthcare", context, True)
    with pytest.raises(PrivacyError, match="output_validation_failed"):
        pipeline.validate("Name: [PERSON_001] Okafor", "general", context, True)


def test_output_check_accepts_model_text_from_approved_input(policies):
    # The model misses "CBC" in the input but flags it when the provider repeats it.
    _, _, context = Pipeline(DetectionEngine(RuleDetector()), policies).sanitize(
        ["Tests: complete blood count (CBC)."], "invoices"
    )
    pipeline = Pipeline(DetectionEngine(RuleDetector(), SpanModel(("CBC", "DIAGNOSIS"))), policies)
    pipeline.validate("The CBC was billed.", "invoices", context, True)
    fresh = MaskContext()
    with pytest.raises(PrivacyError, match="output_validation_failed"):
        pipeline.validate("The CBC was billed.", "invoices", fresh, True)


@pytest.mark.parametrize(
    "text, value",
    [
        ("Date of birth 14 Apr 1993 Visit date 14 Oct 2026", "14 Apr 1993"),
        ("DOB: 03/11/1987", "03/11/1987"),
        ("Birth date = 1990-02-28", "1990-02-28"),
        ("She was born on March 5, 1979 in a small town.", "March 5, 1979"),
    ],
)
def test_date_of_birth_is_detected(text, value):
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert (value, "DATE_OF_BIRTH") in found
    assert not any(kind == "DATE_OF_BIRTH" and "Oct 2026" in v for v, kind in found)


def test_address_extends_over_locality_and_postcode():
    model = SpanModel(("42 Demo Avenue", "ADDRESS"))
    text = "42 Demo Avenue, Sample District, New Delhi – 110000\nCall us."
    found = {
        text[e.start : e.end]
        for e in DetectionEngine(RuleDetector(), model).detect(text)
        if e.entity_type == "ADDRESS"
    }
    assert "42 Demo Avenue, Sample District, New Delhi – 110000" in found


def test_bare_money_amounts_but_not_small_decimals():
    text = "Consultation 1 800.00\nMedicines 1 1,200.00\nVersion 2.25 of 3.14"
    found = {
        text[e.start : e.end] for e in RuleDetector().detect(text) if e.entity_type == "AMOUNT"
    }
    assert found == {"800.00", "1,200.00"}


@pytest.mark.parametrize(
    "text, term",
    [
        ("Negotiated discount: 17%.", "17%."),
        ("Confidential budget\nceiling: INR 5,20,000", "INR 5,20,000"),
        ("Corvane Retail receives a 22 percent volume rebate.", "22 percent volume rebate"),
        ("We agreed a discount of 12.5% for 2027.", "discount of 12.5%"),
    ],
)
def test_commercial_terms_are_detected_by_rules(text, term):
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert (term, "COMMERCIAL_TERM") in found


def test_describing_confidential_categories_is_not_a_term():
    model = SpanModel(
        ("pricing", "COMMERCIAL_TERM"),
        ("contract terms", "COMMERCIAL_TERM"),
        ("credentials", "INTERNAL_INFORMATION"),
        ("patient-related information", "INTERNAL_INFORMATION"),
    )
    text = "Protect patient-related information, pricing, contract terms, and credentials."
    assert DetectionEngine(RuleDetector(), model).detect(text) == []
    party = "Confidential party: Halvorsen Group"
    kinds = {e.entity_type for e in RuleDetector().detect(party)}
    assert kinds == {"ORGANIZATION"}


def test_table_layout_labels_capture_next_line():
    text = (
        "Patient name\nMeera Kapoor\nDate of birth\n09 March 1984\n"
        "Insurance member ID\nPLAN-DEMO-552018\nFacility address\n82 Silver Oak Road\n"
        "Please send the updated address\nby Friday."
    )
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert ("Meera Kapoor", "PERSON") in found
    assert ("09 March 1984", "DATE_OF_BIRTH") in found
    assert ("PLAN-DEMO-552018", "MRN") in found
    assert ("82 Silver Oak Road", "ADDRESS") in found
    # A prose line that merely ends with a label word is not a table label.
    assert not any(value == "by Friday." for value, _ in found)


def test_table_label_exemption_covers_label_only():
    model = SpanModel(("Owen Pratley contact", "PERSON"), ("Facility address", "ADDRESS"))
    text = "Owen Pratley contact\nsee below\nFacility address\n82 Silver Oak Road"
    found = {text[e.start : e.end] for e in DetectionEngine(RuleDetector(), model).detect(text)}
    assert "Owen Pratley contact" in found
    assert "Facility address" not in found


def test_placeholder_examples_in_documents_are_not_entities():
    model = SpanModel(("PERSON_001", "PERSON"), ("Ravi Menon", "PERSON"))
    text = "After: <PERSON_001> called. Before: Ravi Menon called."
    found = {text[e.start : e.end] for e in DetectionEngine(RuleDetector(), model).detect(text)}
    assert found == {"Ravi Menon"}


def test_output_check_accepts_rewording_of_sent_words_only(policies):
    _, _, context = Pipeline(DetectionEngine(RuleDetector()), policies).sanitize(
        ["Restricted: medical record number and internal escalation paths."], "guidelines"
    )
    model = SpanModel(("record numbers", "MRN"), ("internal paths", "INTERNAL_INFORMATION"))
    pipeline = Pipeline(DetectionEngine(RuleDetector(), model), policies)
    pipeline.validate("Protect record numbers and internal paths.", "guidelines", context, True)
    leak = Pipeline(
        DetectionEngine(RuleDetector(), SpanModel(("Meera Kapoor", "PERSON"))), policies
    )
    with pytest.raises(PrivacyError, match="output_validation_failed"):
        leak.validate("Ask Meera Kapoor about record numbers.", "guidelines", context, True)


# Long, unlabelled conversation summaries (samples/conversation-demo). Synthetic values only.


def test_bank_account_stops_before_prose():
    text = "He read out GB82 WEST 1234 5698 7654 32 and asked us to confirm it."
    found = [text[e.start : e.end] for e in RuleDetector().detect(text)]
    assert "GB82 WEST 1234 5698 7654 32" in found
    assert not any("asked" in value for value in found)


def test_amount_excludes_sentence_punctuation():
    text = "A refund of $312.40. The largest was EUR 18,900.00, then INR 48,250."
    found = {
        text[e.start : e.end] for e in RuleDetector().detect(text) if e.entity_type == "AMOUNT"
    }
    assert found == {"$312.40", "EUR 18,900.00", "INR 48,250"}


def test_custom_terms_match_across_line_breaks():
    text = "Tomas asked whether Project\nCedar could include a dashboard."
    found = RuleDetector({"PROJECT": ["Project Cedar"]}).detect(text)
    assert [(text[e.start : e.end], e.entity_type) for e in found] == [
        ("Project\nCedar", "PROJECT")
    ]


def test_ticket_numbers_are_not_phones():
    model = SpanModel(("CS-20431", "PHONE"), ("Daniel Okafor", "PERSON"))
    text = "ticket: CS-20431\nsummary: Daniel Okafor called about a refund."
    found = [text[e.start : e.end] for e in DetectionEngine(RuleDetector(), model).detect(text)]
    assert found == ["Daniel Okafor"]


def test_conversation_roles_are_generic():
    roles = ("speaker", "agent", "hospital", "insurer", "ward doctors", "care coordination team")
    model = SpanModel(*((role, "PERSON") for role in roles), ("Asha Rao", "PERSON"))
    text = "turns[0].speaker: agent\nAsha Rao told the hospital, insurer, " + ", ".join(roles[-2:])
    found = [text[e.start : e.end] for e in DetectionEngine(RuleDetector(), model).detect(text)]
    assert found == ["Asha Rao"]


def test_rule_type_wins_over_model_label(policies):
    text = "Contact mei.tanaka@halden.example.test today."
    model = SpanModel(("mei.tanaka@halden.example.test", "PERSON"))
    found = DetectionEngine(RuleDetector(), model).detect(text)
    masked, _ = mask(text, found, "general", policies, MaskContext())
    assert masked == "Contact [EMAIL_001] today."


def test_gliner_windows_break_at_whitespace():
    from privacy_gateway.detection import word_end, word_start

    text = "Kavya joined briefly and agreed to review the plan."
    start = text.index("briefly") + 2
    assert word_end(text, 0, start) == text.index(" briefly")
    assert word_start(text, 0, start) == text.index("briefly")
    # Unbroken text cannot be split at whitespace and keeps the hard cut.
    assert word_end("x" * 100, 0, 40) == 40
    assert word_start("x" * 100, 0, 40) == 40


# Two-column legal invoice (samples/demo-run/input/invoice1_modern.pdf). Synthetic values only.


def test_us_city_state_zip_is_an_address():
    text = (
        "350 Park Avenue, Suite 2400 | New York, NY 10022\n"
        "100 Commerce Drive, Suite 500     Billing Period: March 2025\n"
        "    San Francisco, CA 94105-1234\n"
        "Standard rate, NY 2025 market; Economy class, CA 12 seats"
    )
    found = {
        text[e.start : e.end]
        for e in RuleDetector().detect(text)
        if e.entity_type == "ADDRESS" and e.source == "regex"
    }
    assert found == {"New York, NY 10022", "San Francisco, CA 94105-1234"}


def test_labelled_reference_numbers_are_not_phones():
    model = SpanModel(("2025-10477", "PHONE"))
    text = "Invoice No: MB-2025-10477\nMatter No: VCG-LIT-2025-003\nCall 212 555 8800 9."
    found = [text[e.start : e.end] for e in DetectionEngine(RuleDetector(), model).detect(text)]
    assert found == ["212 555 8800 9"]


def test_phone_keeps_area_code_bracket():
    text = "Tel: (212) 555-8800 | billing"
    found = {text[e.start : e.end] for e in RuleDetector().detect(text) if e.source == "regex"}
    assert "(212) 555-8800" in found


def test_job_titles_and_table_headings_are_generic():
    titles = ("Partner / Member", "Senior Associate", "Of Counsel / Special Counsel", "Analyst")
    noise = (
        "Market    Amount",
        "eDiscovery",
        "Third-party eDiscovery vendor",
        "Tel",
        "VP",
        "generative AI platform",
        "cite-checking authorities",
        "firm overhead",
        "rent",
        "standard technology",
    )
    model = SpanModel(*((t, "PERSON") for t in titles + noise), ("K. Singh", "PERSON"))
    # Blank lines as in the PDF: a value directly under "Amount" is a labelled amount.
    text = "\n\n".join(titles + noise) + "\n\nK. Singh"
    found = [text[e.start : e.end] for e in DetectionEngine(RuleDetector(), model).detect(text)]
    assert found == ["K. Singh"]


def test_registered_company_first_word_is_masked_everywhere(policies):
    text = (
        "Billed to Volta Commerce Group, LLC.\nReview of Volta document production.\n"
        "Platform Technology Defense filed. Platform fees apply. Revolta is unrelated."
    )
    found = [
        Entity(
            start=text.index(name),
            end=text.index(name) + len(name),
            entity_type="ORGANIZATION",
            confidence=0.9,
            source="gliner",
        )
        for name in ("Volta Commerce Group, LLC", "Platform Technology Defense")
    ]
    propagate([text], [found])
    masked, _ = mask(text, found, "general", policies, MaskContext())
    assert "Volta" not in masked and "Revolta is unrelated" in masked
    # No legal suffix, so the common first word "Platform" is not treated as a name.
    assert "Platform fees apply" in masked


def test_recurrences_match_whole_words_only(policies):
    pipeline = Pipeline(
        DetectionEngine(RuleDetector(), SpanModel(("IRS", "ORGANIZATION"))), policies
    )
    sanitized, _, context = pipeline.sanitize(["At the IRS rate. No first-year time."], "general")
    assert sanitized == ["At the [ORGANIZATION_001] rate. No first-year time."]
    context.clear()


def test_matter_label_is_a_project():
    text = "Matter: Platform Technology Litigation Defense\nMatter No: VCG-LIT-2025-003"
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert found == {("Platform Technology Litigation Defense", "PROJECT")}


def test_model_amount_span_does_not_merge_table_cells(policies):
    text = "2.0     $1,050   $2,100.00\nHotel 1 night.     Standard      $78.50"
    model = SpanModel(("$1,050   $2,100.00", "AMOUNT"), ("Standard      $78.50", "AMOUNT"))
    found = DetectionEngine(RuleDetector(), model).detect(text)
    masked, _ = mask(text, found, "invoices", policies, MaskContext())
    assert masked == (
        "2.0     [AMOUNT_001]   [AMOUNT_002]\nHotel 1 night.     Standard      [AMOUNT_003]"
    )


# Phone photos of fictional Indian-style ID cards (scripts/id_card_demo.py). Synthetic values only.


def test_indian_city_pin_line_is_an_address():
    text = (
        "Address: Flat 7B, Lake View Apts, Salt Lake,\nKolkata - 700091\n"
        "Bengaluru, Karnataka - 560038\nNew Delhi 110 001\nValid Till: 01/02/2039"
    )
    found = {
        text[e.start : e.end]
        for e in RuleDetector().detect(text)
        if e.entity_type == "ADDRESS" and e.source == "regex"
    }
    assert found == {"Kolkata - 700091", "Bengaluru, Karnataka - 560038", "New Delhi 110 001"}


def test_blood_group_is_health_data():
    text = "DOB: 02/02/1992\nBlood Group: B+\nAddress: 18 Church Street"
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert ("B+", "DIAGNOSIS") in found


def test_id_card_words_are_generic():
    words = ("Aadhaar", "Elector", "SPECIMEN", "DRIVING LICENCE\nTransport Department")
    model = SpanModel(*((w, "ORGANIZATION") for w in words), ("Farida Sheikh", "PERSON"))
    # As on the card: the licence title shares its line with "UNION OF INDIA", so it is not a
    # bare "Driving licence" label whose next line would be read as the licence number.
    text = (
        "Aadhaar\n\nElector\n\nSPECIMEN\n\nUNION OF INDIA - DRIVING LICENCE\nTransport Department"
    )
    text += "\n\nFarida Sheikh"
    found = [
        text[e.start : e.end]
        for e in DetectionEngine(RuleDetector(), model).detect(text)
        if e.source == "gliner"
    ]
    assert found == ["Farida Sheikh"]
