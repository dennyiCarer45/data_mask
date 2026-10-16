"""Review (preview) flow, reviewed-hash sending, ID-card rules, and OCR. Synthetic data only."""

import base64
import hashlib
import io
import shutil

import pytest
from conftest import KEY
from fastapi.testclient import TestClient

from privacy_gateway.api import create_app
from privacy_gateway.detection import RuleDetector
from privacy_gateway.documents import DocumentProcessor
from privacy_gateway.ocr import TesseractOCR, Word, layout

NOTE = "Patient: Mira Vale\nPhone: 9876543210\nFollow-up scheduled."
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="no tesseract")


def test_preview_text_pairs_every_span(client):
    response = client.post("/preview/text", json={"text": NOTE, "category": "healthcare"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["original_text"] == NOTE
    assert "Mira Vale" not in body["sanitized_text"]
    assert "9876543210" not in body["sanitized_text"]
    for span in body["spans"]:
        assert body["sanitized_text"][span["masked_start"] : span["masked_end"]] == span["token"]
    originals = {NOTE[x["start"] : x["end"]] for x in body["spans"]}
    assert {"Mira Vale", "9876543210"} <= originals
    assert body["sendable"] is True
    expected = hashlib.sha256(body["sanitized_text"].encode()).hexdigest()
    assert body["preview_sha256"] == expected
    assert body["masked_document"] is None


def test_preview_shows_blocked_values_without_sending(client, spy):
    text = "Confidential rebate: 22 percent volume rebate for Corvane Retail."
    response = client.post("/preview/text", json={"text": text, "category": "agreements"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sendable"] is False and body["preview_sha256"] is None
    assert "COMMERCIAL_TERM" in body["summary"]["blocked_types"]
    assert "22 percent" not in body["sanitized_text"]
    assert spy.calls == []


def test_preview_json_data(client):
    response = client.post("/preview/text", json={"data": {"patient_name": "Mira Vale"}})
    assert response.status_code == 200
    assert response.json()["input_type"] == "json"
    assert "Mira Vale" not in response.json()["sanitized_text"]


def test_chat_sends_only_the_reviewed_masking(client, spy):
    preview = client.post("/preview/text", json={"text": NOTE}).json()
    messages = [{"role": "user", "content": NOTE}, {"role": "user", "content": "Summarise."}]
    response = client.post(
        "/llm/chat",
        json={"messages": messages, "approved_preview_sha256": preview["preview_sha256"]},
    )
    assert response.status_code == 200, response.text
    assert spy.calls[0][0]["content"] == preview["sanitized_text"]
    assert response.json()["sent_messages"] == spy.calls[0]


def test_chat_refuses_text_that_differs_from_review(client, spy):
    preview = client.post("/preview/text", json={"text": NOTE}).json()
    changed = NOTE.replace("Follow-up", "Contact zorina@example.test for follow-up")
    response = client.post(
        "/llm/chat",
        json={
            "messages": [{"role": "user", "content": changed}],
            "approved_preview_sha256": preview["preview_sha256"],
        },
    )
    assert response.json() == {"error": "preview_mismatch"} and response.status_code == 409
    assert spy.calls == []


def test_document_chat_with_question_and_hash(client, spy):
    files = {"file": ("note.txt", NOTE.encode())}
    preview = client.post("/preview/document", files=files).json()
    response = client.post(
        "/llm/document",
        files=files,
        data={"question": "Summarise.", "approved_preview_sha256": preview["preview_sha256"]},
    )
    assert response.status_code == 200, response.text
    assert [m["content"] for m in spy.calls[0]] == [preview["sanitized_text"], "Summarise."]


def test_review_page_is_public_static_and_api_is_not(settings, engine, spy):
    with TestClient(create_app(settings, engine, spy)) as anonymous:
        page = anonymous.get("/review")
        assert page.status_code == 200 and "Masking review" in page.text
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert anonymous.post("/preview/text", json={"text": NOTE}).status_code == 401


@pytest.mark.parametrize(
    "value,kind",
    [
        ("2345 6789 0123", "GOVERNMENT_ID"),  # Aadhaar shape (synthetic)
        ("9123 4567 8901 2345", "GOVERNMENT_ID"),  # VID shape
        ("XXXX XXXX 4321", "GOVERNMENT_ID"),  # masked Aadhaar
        ("ABC1234567", "GOVERNMENT_ID"),  # voter ID (EPIC)
        ("K1234567", "GOVERNMENT_ID"),  # passport
        ("MH12 20110012345", "GOVERNMENT_ID"),  # driving licence
        ("ABCDE1234F", "TAX_ID"),  # PAN
    ],
)
def test_indian_id_numbers(value, kind):
    text = f"Card number {value} issued."
    found = {(text[e.start : e.end], e.entity_type) for e in RuleDetector().detect(text)}
    assert (value, kind) in found


@pytest.mark.parametrize(
    "text,value",
    [
        ("S/O Ravi Kamath, 12 Lake Road", "Ravi Kamath"),
        ("D/O: Asha Kamath", "Asha Kamath"),
        ("Father's Name: Ravi Kamath", "Ravi Kamath"),
        ("Year of Birth: 1990", "1990"),
    ],
)
def test_id_card_labels(text, value, pipeline):
    sanitized, _, context = pipeline.sanitize([text], "general")
    context.clear()
    assert value not in sanitized[0]


class FakeOCR:
    """Returns fixed words; exercises the unverified-word policy without Tesseract."""

    version = "fake"
    min_confidence = 60

    def __init__(self, words):
        self._words = words

    def words(self, image):
        return self._words

    def unverified(self, word):
        return TesseractOCR.unverified(self, word)


def png_bytes(size=(400, 120)):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, "PNG")
    return output.getvalue()


def test_unreadable_and_non_latin_ocr_words_are_masked(pipeline):
    pytest.importorskip("PIL")
    words = [
        Word("Name:", 95, (0, 0, 50, 10), (1, 1, 1)),
        Word("Mira", 93, (55, 0, 90, 10), (1, 1, 1)),
        Word("Va1e", 41, (95, 0, 130, 10), (1, 1, 1)),  # low confidence
        Word("मीरा", 91, (0, 20, 40, 30), (1, 1, 2)),  # Devanagari: not verifiable
        Word("issued", 96, (0, 40, 40, 50), (1, 2, 1)),
    ]
    extracted = DocumentProcessor(ocr=FakeOCR(words)).extract_document(png_bytes(), "id.png")
    assert extracted.text == "Name: Mira Va1e\nमीरा\nissued"
    assert [extracted.text[s:e] for s, e in extracted.unverified] == ["Va1e", "मीरा"]
    sanitized, _, context = pipeline.sanitize([extracted.text], "general", [extracted.unverified])
    context.clear()
    assert "Va1e" not in sanitized[0] and "मीरा" not in sanitized[0]
    assert "[UNVERIFIED_TEXT_" in sanitized[0] and sanitized[0].endswith("issued")
    with pytest.raises(Exception, match="ocr_unverified_text"):
        DocumentProcessor(ocr=FakeOCR(words)).extract(png_bytes(), "id.png")


def test_layout_offsets_match_words():
    words = [Word("a", 90, (0, 0, 1, 1), (1, 1, 1)), Word("bc", 90, (2, 0, 3, 1), (1, 1, 2))]
    result = layout(words, FakeOCR(words))
    assert result.text == "a\nbc"
    assert [result.text[s:e] for s, e, _ in result.words] == ["a", "bc"]


def test_images_refused_without_ocr(settings, engine, spy):
    pytest.importorskip("PIL")
    off = settings.model_copy(update={"ocr": "off"})
    with TestClient(create_app(off, engine, spy)) as anonymous:
        anonymous.headers["Authorization"] = "Bearer " + KEY
        response = anonymous.post("/preview/document", files={"file": ("id.png", png_bytes())})
    assert response.status_code == 503 and response.json() == {"error": "ocr_unavailable"}


# --- Real Tesseract (provided Docker image) -------------------------------------------------

CARD = [
    "GOVERNMENT IDENTITY CARD",
    "Name: Mira Vale",
    "DOB: 14/04/1993",
    "Phone: 9876543210",
    "Number: 2345 6789 0123",
]


def card_image(lines=CARD, file_format="PNG"):
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=42)
    image = Image.new("RGB", (1300, 90 + 80 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 50 + 80 * index), line, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, file_format)
    return output.getvalue()


SECRETS = ["Mira Vale", "14/04/1993", "9876543210", "2345 6789 0123"]


@needs_tesseract
@pytest.mark.parametrize("name,file_format", [("card.png", "PNG"), ("card.jpg", "JPEG")])
def test_id_card_image_is_read_and_masked(client, name, file_format):
    files = {"file": (name, card_image(file_format=file_format))}
    response = client.post("/preview/document", files=files)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "Mira" in body["original_text"] and "9876543210" in body["original_text"]
    for secret in SECRETS:
        assert secret not in body["sanitized_text"]
    assert body["input_type"] == "image" and body["ocr"]["words"] > 0
    document = body["masked_document"]
    assert document["media_type"] == "image/png" and document["unlocated_spans"] == 0
    assert base64.b64decode(document["base64"]).startswith(b"\x89PNG")


def pdf_with_image(text_layer=True):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if text_layer:
        page.insert_text((72, 60), "Visit summary for Mira Vale")
    page.insert_image(pymupdf.Rect(40, 100, 560, 400), stream=card_image())
    content = doc.tobytes()
    doc.close()
    return content


@needs_tesseract
@pytest.mark.parametrize("text_layer", [True, False], ids=["mixed", "scanned"])
def test_pdf_image_regions_are_read_and_masked(client, text_layer):
    response = client.post(
        "/preview/document", files={"file": ("scan.pdf", pdf_with_image(text_layer))}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "9876543210" in body["original_text"]
    for secret in SECRETS:
        assert secret not in body["sanitized_text"]
    assert body["ocr"]["words"] > 0
    import pymupdf

    masked = base64.b64decode(body["masked_document"]["base64"])
    with pymupdf.open(stream=masked, filetype="pdf") as doc:
        assert "Mira Vale" not in doc[0].get_text()


@needs_tesseract
def test_id_card_send_after_review(client, spy):
    files = {"file": ("card.png", card_image())}
    preview = client.post("/preview/document", files=files).json()
    response = client.post(
        "/llm/document", files=files, data={"approved_preview_sha256": preview["preview_sha256"]}
    )
    assert response.status_code == 200, response.text
    sent = spy.calls[0][0]["content"]
    assert sent == preview["sanitized_text"]
    assert not any(secret in sent for secret in SECRETS)


class SecondPassModel:
    """Model stand-in: flags "Velmont" as a person only once text contains a placeholder,
    like GLiNER changing its mind on the masked text."""

    mode = "fake"

    def detect(self, text):
        from privacy_gateway.schemas import Entity

        found = []
        if "[PERSON_001]" in text and "Velmont" in text:
            start = text.index("Velmont")
            found.append(
                Entity(
                    start=start,
                    end=start + 7,
                    entity_type="PERSON",
                    confidence=0.55,
                    source="gliner_fake",
                )
            )
        if "Sector 62" in text:
            start = text.index("Sector 62")
            found.append(
                Entity(
                    start=start,
                    end=text.index("122102") + 6,
                    entity_type="ADDRESS",
                    confidence=0.9,
                    source="gliner",
                )
            )
        return found


class AdjacentModel:
    """Model stand-in: on masked text, flags a placeholder together with the word after it."""

    mode = "fake"

    def detect(self, text):
        from privacy_gateway.schemas import Entity

        if "[PERSON_001] Velmont" not in text:
            return []
        start = text.index("[PERSON_001] Velmont")
        return [
            Entity(
                start=start, end=start + 20, entity_type="PERSON", confidence=0.55, source="gliner"
            )
        ]


def test_second_look_masks_values_found_only_in_masked_text(settings, spy, policies):
    from privacy_gateway.detection import DetectionEngine

    engine = DetectionEngine(RuleDetector(policies.config.custom_terms), SecondPassModel())
    text = "Name: Mira Vale\nVelmont signed. Velmont agreed."
    with TestClient(create_app(settings, engine, spy)) as review:
        review.headers["Authorization"] = "Bearer " + KEY
        body = review.post("/preview/text", json={"text": text}).json()
        assert body["validation"]["passed"] is True and body["sendable"] is True
        assert "Velmont" not in body["sanitized_text"]
        assert any("second_look" in span["sources"] for span in body["spans"])


def test_preview_lists_values_still_detected_after_masking(settings, spy, policies):
    from privacy_gateway.detection import DetectionEngine

    # A second-look hit that touches a placeholder is a partial leak: never re-masked, refused.
    engine = DetectionEngine(RuleDetector(policies.config.custom_terms), AdjacentModel())
    text = "Name: Mira Vale\nMira Vale Velmont"
    with TestClient(create_app(settings, engine, spy)) as review:
        review.headers["Authorization"] = "Bearer " + KEY
        body = review.post("/preview/text", json={"text": text}).json()
        assert body["validation"]["passed"] is False
        assert body["sendable"] is False and body["preview_sha256"] is None
        (item,) = body["validation"]["findings"]
        found = body["sanitized_text"][item["masked_start"] : item["masked_end"]]
        assert found == "[PERSON_001] Velmont"
        assert item["reason"] == "residual_sensitive_entity" and item["entity_type"] == "PERSON"
        sent = review.post("/llm/chat", json={"messages": [{"role": "user", "content": text}]})
        assert sent.json() == {"error": "input_validation_failed"}
    assert spy.calls == []


def test_address_extends_back_to_house_number(policies):
    from privacy_gateway.detection import DetectionEngine

    engine = DetectionEngine(RuleDetector(), SecondPassModel())
    text = 'between Northstar ("Client"), 28 Orchid Business Park, Sector 62, Gurugram 122102, and'
    spans = {text[e.start : e.end] for e in engine.detect(text) if e.entity_type == "ADDRESS"}
    assert "28 Orchid Business Park, Sector 62, Gurugram 122102" in spans


def test_gender_and_role_words_are_not_names():
    from privacy_gateway.detection import is_generic

    assert is_generic("Female") and is_generic("Delivery Manager")
    assert is_generic("Authorized Representative\n\n For the Provider")
    assert not is_generic("Mira Vale")


def test_masked_pdf_labels_fit_narrow_table_cells():
    import pymupdf

    from privacy_gateway.documents import Extracted
    from privacy_gateway.redaction import render_masked

    doc = pymupdf.open()
    page = doc.new_page()
    # Invoice row: narrow Rate cell, then Rate and Amount masked together as one value.
    page.insert_text((400, 100), "$800", fontsize=8)
    page.insert_text((400, 130), "$1,050", fontsize=8)
    page.insert_text((470, 130), "$2,100.00", fontsize=8)
    page.insert_text((400, 160), "$510.00", fontsize=8)
    content = doc.tobytes()
    doc.close()
    with pymupdf.open(stream=content, filetype="pdf") as source:
        text = source[0].get_text("text", sort=True)
    spans = [
        {"start": text.index("$800"), "end": text.index("$800") + 4, "token": "[AMOUNT_001]"},
        {
            "start": text.index("$1,050"),
            "end": text.index("$2,100.00") + 9,
            "token": "[AMOUNT_002]",
        },
        {"start": text.index("$510.00"), "end": text.index("$510.00") + 7, "token": "[AMOUNT_003]"},
    ]
    extracted = Extracted(text, "pdf", content, layers=[(0, len(text), 0)])
    document, unlocated = render_masked(extracted, spans)
    assert unlocated == 0
    with pymupdf.open(stream=document["content"], filetype="pdf") as masked:
        shown = masked[0].get_text()
    assert "$" not in shown
    # Too narrow for the full label: its number is shown instead of a blank box.
    assert "001" in shown and "[AMOUNT_001]" not in shown
    # The label goes on the wider Amount cell, so it fits.
    assert "[AMOUNT_002]" in shown
    # Every box of a value is labelled, so the merged Rate cell is not left blank.
    assert shown.count("002") == 2
    # Medium cells fall back to the number rather than a label too small to be drawn.
    assert "003" in shown
