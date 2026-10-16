import io
import zipfile

import pytest
from docx import Document

from privacy_gateway.documents import DocumentProcessor
from privacy_gateway.errors import PrivacyError


def pdf_bytes(blank=False, image=False):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if not blank:
        page.insert_text((72, 72), "Patient: Mira Vale\nPhone: 9876543210")
    if image:
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 8, 8))
        pix.clear_with(255)
        page.insert_image(pymupdf.Rect(20, 20, 40, 40), pixmap=pix)
    result = doc.tobytes()
    doc.close()
    return result


def docx_bytes():
    doc = Document()
    doc.add_paragraph("Patient: Mira Vale")
    table = doc.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Phone: 9876543210"
    doc.sections[0].header.paragraphs[0].text = "Email: header@example.test"
    doc.sections[0].footer.paragraphs[0].text = "MRN-45821"
    result = io.BytesIO()
    doc.save(result)
    return result.getvalue()


@pytest.mark.parametrize(
    "filename,content",
    [
        ("synthetic.txt", b"Patient: Mira Vale\nPhone: 9876543210"),
        ("synthetic.json", b'{"patient_name":"Mira Vale","phone":9876543210}'),
    ],
)
def test_text_documents(client, filename, content):
    response = client.post("/mask/document", files={"file": (filename, content)})
    assert response.status_code == 200, response.text
    assert "Mira Vale" not in response.text and "9876543210" not in response.text


def test_searchable_pdf(client):
    response = client.post("/mask/document", files={"file": ("test.pdf", pdf_bytes())})
    assert response.status_code == 200, response.text
    assert "Mira Vale" not in response.text and "9876543210" not in response.text


def test_docx_table_header_footer(client):
    response = client.post("/mask/document", files={"file": ("test.docx", docx_bytes())})
    assert response.status_code == 200, response.text
    assert all(
        value not in response.text
        for value in ["Mira Vale", "9876543210", "header@example.test", "MRN-45821"]
    )


@pytest.mark.parametrize(
    "filename,content",
    [
        ("bad.txt", b"\xff\xfe"),
        ("bad.pdf", b"invalid"),
        ("bad.docx", b"invalid"),
        ("bad.exe", b"data"),
        ("empty.txt", b""),
        ("nul.txt", b"abc\x00secret"),
        ("bad.json", b'{"a":"first","a":"lost"}'),
        ("bad.json", b'{"a":NaN}'),
    ],
)
def test_unreadable_documents_rejected(client, filename, content):
    response = client.post("/mask/document", files={"file": (filename, content)})
    assert response.status_code in {422, 415}


def test_blank_pdf_page_rejected():
    with pytest.raises(PrivacyError):
        DocumentProcessor().extract(pdf_bytes(blank=True), "scan.pdf")


def test_mixed_image_pdf_rejected():
    with pytest.raises(PrivacyError, match="partial_extraction_risk"):
        DocumentProcessor().extract(pdf_bytes(image=True), "mixed.pdf")


def test_partial_multipage_pdf_rejected():
    import pymupdf

    with pymupdf.open(stream=pdf_bytes(), filetype="pdf") as doc:
        doc.new_page()
        content = doc.tobytes()
    with pytest.raises(PrivacyError):
        DocumentProcessor().extract(content, "partial.pdf")


def test_docx_unsupported_part_rejected():
    output = io.BytesIO(docx_bytes())
    with zipfile.ZipFile(output, "a") as archive:
        archive.writestr("word/embeddings/secret.bin", "synthetic")
    with pytest.raises(PrivacyError, match="partial_extraction_risk"):
        DocumentProcessor().extract(output.getvalue(), "embedded.docx")


def pdf_with_attachments(*attachments):
    """attachments: (name, c2pa_markers) pairs."""
    import pymupdf

    with pymupdf.open(stream=pdf_bytes(), filetype="pdf") as doc:
        for name, c2pa in attachments:
            if not c2pa:
                doc.embfile_add(name, b"synthetic attachment", filename=name)
                continue
            # Indirect file spec and typed stream, as C2PA-signing tools write them.
            stream = doc.get_new_xref()
            doc.update_object(stream, "<</Type/EmbeddedFile/Subtype/application#2Fc2pa>>")
            doc.update_stream(stream, b"synthetic manifest")
            spec = doc.get_new_xref()
            doc.update_object(
                spec,
                f"<</Type/Filespec/AFRelationship/C2PA_Manifest/F({name})/UF({name})"
                f"/EF<</F {stream} 0 R>>>>",
            )
            doc.xref_set_key(
                doc.pdf_catalog(), "Names/EmbeddedFiles/Names", f"[({name}) {spec} 0 R]"
            )
        return doc.tobytes()


def test_pdf_content_credentials_manifest_is_ignored():
    content = pdf_with_attachments(("Content Credentials", True))
    assert "Mira Vale" in DocumentProcessor().extract(content, "signed.pdf")


@pytest.mark.parametrize(
    "attachments",
    [
        [("Content Credentials", False)],
        [("Content Credentials", True), ("notes.txt", False)],
    ],
)
def test_pdf_other_attachments_rejected(attachments):
    with pytest.raises(PrivacyError, match="unsupported_pdf"):
        DocumentProcessor().extract(pdf_with_attachments(*attachments), "attached.pdf")
