"""Draw a masked copy of an uploaded PDF or image for local review. Never sent to a provider."""

import io
import re

from .documents import open_image
from .layout import union

NOTE = (
    "Review copy. Masked text is covered; photos, QR codes, signatures, and other graphics "
    "are not removed. Only the masked text is ever sent to the AI."
)


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def render_masked(extracted, spans):
    """Return ({"media_type", "content", "note"}, unlocated) or (None, 0) for text inputs.

    unlocated counts masked spans the review copy does not cover: no printed position, or (PDF)
    text still readable in the copy after redaction. Their text is still masked in the sanitized
    output, but the review copy shows them uncovered.
    """
    if extracted.kind == "pdf":
        return _pdf(extracted, spans)
    if extracted.kind == "image":
        return _image(extracted, spans)
    return None, 0


def line_rects(boxes):
    """Merge character or word boxes on one line into one rectangle per line and page."""
    merged = []
    for page, box in sorted(boxes, key=lambda item: (item[0], item[1][1], item[1][0])):
        if merged:
            last_page, last = merged[-1]
            height = min(last[3] - last[1], box[3] - box[1])
            same_line = min(last[3], box[3]) - max(last[1], box[1]) >= 0.5 * height
            if last_page == page and same_line and box[0] - last[2] <= max(height, 3):
                merged[-1] = (page, union([last, box]))
                continue
        merged.append((page, box))
    return merged


def _pdf(extracted, spans):
    import pymupdf

    from .masking import literal

    with pymupdf.open(stream=extracted.content, filetype="pdf") as doc:
        found = []
        for span in spans:
            boxes = [
                (page, box)
                for start, end, page, box in extracted.boxes
                if overlaps(start, end, span["start"], span["end"])
            ]
            found.append([(page, pymupdf.Rect(box)) for page, box in line_rects(boxes)])
        everything = [item for rects in found for item in rects]
        labelled = set()
        for span, rects in zip(spans, found, strict=True):
            for page, rect in rects:
                # Label every box once. A box inside a larger masked box is left to that
                # box's label so labels do not pile up.
                key = (page, tuple(rect))
                inside = any(
                    p == page and other != rect and other.contains(rect) for p, other in everything
                )
                _redact(doc[page], rect, "" if inside or key in labelled else span["token"])
                labelled.add(key)
        for page in doc:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE
            )
        # Drop metadata, attachments, hidden text, scripts, and links from the review copy.
        doc.scrub()
        content = doc.tobytes(garbage=3, deflate=True)
    # Check the copy as a reader would: any masked value still readable is reported uncovered.
    with pymupdf.open(stream=content, filetype="pdf") as copy:
        remaining = " ".join(" ".join(page.get_text().split()) for page in copy).casefold()
    unlocated = 0
    for span, rects in zip(spans, found, strict=True):
        value = " ".join(extracted.text[span["start"] : span["end"]].split()).casefold()
        leftover = len(value) >= 3 and re.search(literal(value), remaining)
        unlocated += not rects or bool(leftover)
    return {"media_type": "application/pdf", "content": content, "note": NOTE}, unlocated


def _redact(page, rect, label):
    import pymupdf

    size = max(4.0, min(9.0, rect.height * 0.7))
    if label:
        label, size = _fit(pymupdf, label, rect.width - 1, size)
    page.add_redact_annot(
        rect, text=label or None, fontsize=size, fill=(0, 0, 0), text_color=(1, 1, 1)
    )


def _fit(pymupdf, label, width, size):
    """A label wider than its box is silently not drawn, leaving a plain black box. Shrink it,
    and in very narrow boxes ("$800") show only the placeholder number ("[AMOUNT_003]" -> "003")."""
    if width <= 0:
        return "", size
    number = "".join(re.findall(r"\d", label))
    for text in filter(None, (label, number)):
        length = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
        fitted = size * min(1.0, width / length)
        # PyMuPDF silently skips redaction text below about 4 pt (measured with 1.28.2).
        if fitted >= 4.1:
            return text, fitted
    return text, max(fitted, 1.0)


def _image(extracted, spans):
    from PIL import ImageDraw, ImageFont

    image = open_image(extracted.content, extracted.image_format).convert("RGB")
    draw = ImageDraw.Draw(image)
    unlocated = 0
    for span in spans:
        boxes = [
            box
            for start, end, _, box in extracted.boxes
            if overlaps(start, end, span["start"], span["end"])
        ]
        unlocated += not boxes
        for index, (x0, y0, x1, y1) in enumerate(boxes):
            draw.rectangle((x0 - 2, y0 - 2, x1 + 2, y1 + 2), fill="black")
            if index == 0:
                font = ImageFont.load_default(size=max(10, int((y1 - y0) * 0.6)))
                draw.text((x0, y0), span["token"], fill="white", font=font)
    output = io.BytesIO()
    image.save(output, "PNG")
    return {"media_type": "image/png", "content": output.getvalue(), "note": NOTE}, unlocated
