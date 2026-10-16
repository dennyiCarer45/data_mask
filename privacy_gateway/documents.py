import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import PrivacyError
from .ocr import layout

IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MAX_IMAGE_PIXELS = 40_000_000
# Render PDF image regions for OCR at up to 300 dpi, capped in total pixels per region.
OCR_DPI, MAX_REGION_PIXELS = 300, 25_000_000


def check_text(text: str, limit: int) -> str:
    if not text.strip():
        raise PrivacyError("empty_document")
    if len(text) > limit:
        raise PrivacyError("text_too_large", 413)
    if "\ufffd" in text or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise PrivacyError("unreadable_document")
    return text


def json_text(data, limit: int = 32000) -> str:
    """Render every key/value locally. JSON type/layout preservation is not promised."""
    lines = []
    size = 0

    def walk(value, path: str, depth: int):
        nonlocal size
        if depth > 20:
            raise PrivacyError("json_too_deep")
        if isinstance(value, dict):
            for key, item in value.items():
                # Include ancestor keys; user-controlled keys must also be scanned.
                walk(item, f"{path}.{key}" if path else str(key), depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", depth + 1)
        else:
            rendered = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
            line = f"{path}: {rendered}" if path else rendered
            size += len(line) + 1
            if size > limit:
                raise PrivacyError("text_too_large", 413)
            lines.append(line)
        # Empty containers can still have sensitive keys.
        if isinstance(value, (dict, list)) and not value and path:
            lines.append(path + ": []")

    walk(data, "", 0)
    return check_text("\n".join(lines), limit)


def unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise PrivacyError("duplicate_json_key")
        output[key] = value
    return output


def invalid_constant(_):
    raise PrivacyError("invalid_json_number")


@dataclass(repr=False)
class Extracted:
    """Extracted text plus what is needed to draw a masked copy of the same document locally."""

    text: str
    kind: str
    content: bytes = b""
    # OCR words that must be masked: low confidence or a script the detectors do not cover.
    unverified: list[tuple[int, int]] = field(default_factory=list)
    # OCR word boxes as (start, end, page, (x0, y0, x1, y1)); page is None for image files.
    boxes: list = field(default_factory=list)
    # PDF text-layer ranges as (start, end, page).
    layers: list = field(default_factory=list)
    ocr_words: int = 0
    image_format: str = ""


class DocumentProcessor:
    def __init__(self, max_bytes=5_000_000, max_chars=32000, ocr=None):
        self.max_bytes, self.max_chars, self.ocr = max_bytes, max_chars, ocr

    def extract(self, content: bytes, filename: str) -> str:
        """Text only. Callers that cannot carry unverified OCR words are refused them."""
        extracted = self.extract_document(content, filename)
        if extracted.unverified:
            raise PrivacyError("ocr_unverified_text")
        return extracted.text

    def extract_document(self, content: bytes, filename: str) -> Extracted:
        if len(content) > self.max_bytes:
            raise PrivacyError("document_too_large", 413)
        try:
            suffix = Path(filename).suffix.lower()
            if suffix == ".txt":
                result = Extracted(content.decode("utf-8-sig", errors="strict"), "text")
            elif suffix == ".json":
                value = json.loads(
                    content.decode("utf-8-sig"),
                    object_pairs_hook=unique_object,
                    parse_constant=invalid_constant,
                )
                result = Extracted(json_text(value, self.max_chars), "json")
            elif suffix == ".pdf":
                result = self._pdf(content)
            elif suffix == ".docx":
                result = Extracted(self._docx(content), "docx")
            elif suffix in IMAGE_FORMATS:
                result = self._image(content, IMAGE_FORMATS[suffix])
            else:
                raise PrivacyError("unsupported_document_type", 415)
            check_text(result.text, self.max_chars)
            return result
        except PrivacyError:
            raise
        except Exception:
            raise PrivacyError("document_extraction_failed") from None

    def _image(self, content: bytes, image_format: str) -> Extracted:
        if self.ocr is None:
            raise PrivacyError("ocr_unavailable", 503)
        words = self.ocr.words(open_image(content, image_format))
        region = layout(words, self.ocr)
        boxes = [(start, end, None, box) for start, end, box in region.words]
        return Extracted(
            region.text, "image", content, region.unverified, boxes, [], len(words), image_format
        )

    def _pdf(self, content: bytes) -> Extracted:
        import pymupdf

        if not content.startswith(b"%PDF-"):
            raise PrivacyError("invalid_pdf")
        result = Extracted("", "pdf", content)
        with pymupdf.open(stream=content, filetype="pdf") as doc:
            if doc.needs_pass or doc.is_repaired or not 0 < len(doc) <= 100:
                raise PrivacyError("unsupported_pdf")
            if doc.embfile_count() and not self._only_content_credentials(doc):
                raise PrivacyError("unsupported_pdf")
            pages, offset = [], 0
            for number, page in enumerate(doc):
                if list(page.annots() or []) or list(page.widgets() or []):
                    raise PrivacyError("partial_extraction_risk")
                # Includes inline images, which page.get_images() does not list.
                images = [pymupdf.Rect(i["bbox"]) & page.rect for i in page.get_image_info()]
                # Without OCR an image could hold identifiers that are never extracted.
                if images and self.ocr is None:
                    raise PrivacyError("partial_extraction_risk")
                layer = page.get_text("text", sort=True)
                parts = []
                if layer.strip():
                    parts.append(layer)
                    result.layers.append((offset, offset + len(layer), number))
                for rect in images:
                    if rect.is_empty or rect.width < 2 or rect.height < 2:
                        continue
                    region = self._ocr_region(page, rect)
                    if not region.text:
                        continue
                    base = offset + sum(map(len, parts)) + len(parts)
                    parts.append(region.text)
                    result.boxes.extend((base + s, base + e, number, b) for s, e, b in region.words)
                    result.unverified.extend((base + s, base + e) for s, e in region.unverified)
                    result.ocr_words += len(region.words)
                # A blank page, or an image without readable text, may be an unread scan.
                text = check_text("\n".join(parts), self.max_chars)
                pages.append(text)
                offset += len(text) + 1
                if offset > self.max_chars:
                    raise PrivacyError("text_too_large", 413)
            result.text = "\n".join(pages)
            return result

    def _ocr_region(self, page, rect):
        """OCR an image region as rendered on the page; skip words the text layer already has."""
        import pymupdf
        from PIL import Image

        zoom = min(OCR_DPI / 72, math.sqrt(MAX_REGION_PIXELS / (rect.width * rect.height)))
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(zoom, zoom), clip=rect, colorspace=pymupdf.csGRAY, alpha=False
        )
        image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        layer_words = [
            (pymupdf.Rect(w[:4]) + (-3, -3, 3, 3), w[4].casefold()) for w in page.get_text("words")
        ]
        kept = []
        for word in self.ocr.words(image):
            x0, y0, x1, y1 = word.box
            box = (
                rect.x0 + x0 / zoom,
                rect.y0 + y0 / zoom,
                rect.x0 + x1 / zoom,
                rect.y0 + y1 / zoom,
            )
            center = pymupdf.Point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            # Text printed over an image is already extracted from the text layer.
            if any(text == word.text.casefold() and center in r for r, text in layer_words):
                continue
            kept.append(replace(word, box=box))
        return layout(kept, self.ocr)

    @staticmethod
    def _only_content_credentials(doc) -> bool:
        """True if every embedded file is solely a C2PA provenance manifest ("Content Credentials").

        Many tools attach one automatically. It is never extracted or forwarded. Any other
        attachment, a renamed file without both C2PA markers, or extra streams still fail.
        """
        manifests = 0
        for xref in range(1, doc.xref_length()):
            if doc.xref_get_key(xref, "Type")[1] != "/Filespec":
                continue
            kind, files = doc.xref_get_key(xref, "EF")
            if kind == "null":
                continue
            stream = re.fullmatch(r"<</F (\d+) 0 R>>", files.strip()) if kind == "dict" else None
            if (
                stream is None
                or doc.xref_get_key(xref, "AFRelationship")[1] != "/C2PA_Manifest"
                or doc.xref_get_key(int(stream[1]), "Subtype")[1]
                not in ("/application/c2pa", "/application#2Fc2pa")
            ):
                return False
            manifests += 1
        return manifests == doc.embfile_count()

    def _docx(self, content: bytes) -> str:
        from docx import Document
        from lxml import etree

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > 500 or sum(i.file_size for i in infos) > 20_000_000:
                raise PrivacyError("document_expansion_limit", 413)
            if any(i.flag_bits & 1 or i.file_size / max(i.compress_size, 1) > 200 for i in infos):
                raise PrivacyError("unsupported_docx")
            if any(
                i.filename.startswith(("word/media/", "word/embeddings/", "word/charts/"))
                or "vbaProject" in i.filename
                for i in infos
            ):
                raise PrivacyError("partial_extraction_risk")
            # Verify package is a Word document through python-docx, then walk all supported parts.
            Document(io.BytesIO(content))
            chunks = []
            for info in infos:
                if not info.filename.startswith("word/") or not info.filename.endswith(".xml"):
                    continue
                raw = archive.read(info)
                if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                    raise PrivacyError("unsupported_docx")
                root = etree.fromstring(
                    raw, etree.XMLParser(resolve_entities=False, no_network=True)
                )
                for node in root.iter():
                    tag = etree.QName(node).localname
                    if tag in {
                        "drawing",
                        "pict",
                        "object",
                        "altChunk",
                        "del",
                        "ins",
                        "fldChar",
                        "instrText",
                        "oMath",
                        "oMathPara",
                        "AlternateContent",
                    }:
                        raise PrivacyError("partial_extraction_risk")
                supported = ("document.xml", "footnotes.xml", "endnotes.xml", "comments.xml")
                part = Path(info.filename).name
                if part in supported or part.startswith(("header", "footer")):
                    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                    for paragraph in root.iter(ns + "p"):
                        # Includes nested tables and headers/footers in their package order.
                        chunks.append("".join(n.text or "" for n in paragraph.iter(ns + "t")))
            return "\n".join(chunks)


def open_image(content: bytes, image_format: str):
    """Decode one still image as it is displayed (EXIF rotation applied), with a pixel limit."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(content), formats=[image_format]) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise PrivacyError("image_too_large", 413)
        if getattr(image, "n_frames", 1) != 1:
            raise PrivacyError("unsupported_image")
        image.load()
        return ImageOps.exif_transpose(image)
