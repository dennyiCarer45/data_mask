"""Local OCR through the Tesseract command-line program. No network, files, or shell."""

import csv
import io
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from .errors import PrivacyError

# Latin letters (with accents), digits, and punctuation are what the English detectors were
# evaluated on. Any other letter (Devanagari on Aadhaar cards, for example) cannot be verified.
LATIN_ONLY = re.compile(r"[\x00-\x7f -ɏ -⁯₠-⃏]*")
LONGEST_SIDE = 2400


@dataclass(frozen=True)
class Word:
    text: str
    confidence: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in the caller's image pixels
    line: tuple[int, int, int]  # block, paragraph, line


@dataclass(frozen=True)
class OCRText:
    text: str
    # (start, end, box) for every word, offsets into text.
    words: list[tuple[int, int, tuple[float, float, float, float]]]
    # Offsets of words OCR could not read confidently or in a script the detectors do not cover.
    unverified: list[tuple[int, int]]


class TesseractOCR:
    def __init__(self, command="tesseract", languages="eng+hin", min_confidence=60, timeout=60):
        self.command = shutil.which(command)
        if self.command is None:
            raise PrivacyError("ocr_unavailable", 503)
        self.languages, self.min_confidence, self.timeout = languages, min_confidence, timeout
        installed = self._run(["--list-langs"], b"").decode("utf-8", "replace").split()
        if not set(languages.split("+")) <= set(installed):
            raise PrivacyError("ocr_unavailable", 503)
        self.version = self._run(["--version"], b"").decode("utf-8", "replace").split()[1]

    def _run(self, arguments, stdin):
        environment = {"PATH": os.environ.get("PATH", ""), "OMP_THREAD_LIMIT": "1"}
        try:
            result = subprocess.run(
                [self.command, *arguments],
                input=stdin,
                capture_output=True,
                timeout=self.timeout,
                env=environment,
                check=True,
            )
        except Exception:
            raise PrivacyError("ocr_failed", 503) from None
        return result.stdout

    def words(self, image) -> list[Word]:
        """OCR a Pillow image. Small images are enlarged; boxes are in the input's pixels."""
        from PIL import Image

        gray = image.convert("L")
        scale = min(3.0, max(1.0, LONGEST_SIDE / max(gray.size)))
        if scale > 1:
            size = (round(gray.width * scale), round(gray.height * scale))
            gray = gray.resize(size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        gray.save(buffer, "PNG")
        tsv = self._run(
            ["stdin", "stdout", "-l", self.languages, "--psm", "3", "tsv"], buffer.getvalue()
        )
        words = []
        rows = csv.DictReader(
            io.StringIO(tsv.decode("utf-8", "strict")), delimiter="\t", quoting=csv.QUOTE_NONE
        )
        for row in rows:
            text = (row.get("text") or "").strip()
            if row["level"] != "5" or not text:
                continue
            left, top = int(row["left"]) / scale, int(row["top"]) / scale
            right = left + int(row["width"]) / scale
            bottom = top + int(row["height"]) / scale
            words.append(
                Word(
                    text,
                    float(row["conf"]),
                    (left, top, right, bottom),
                    (int(row["block_num"]), int(row["par_num"]), int(row["line_num"])),
                )
            )
        return words

    def unverified(self, word: Word) -> bool:
        if not re.search(r"\w", word.text):
            return False
        return word.confidence < self.min_confidence or not LATIN_ONLY.fullmatch(word.text)


def layout(words: list[Word], ocr: TesseractOCR) -> OCRText:
    """Join words into lines in Tesseract's reading order, keeping each word's offsets."""
    text, placed, unverified = [], [], []
    size, previous = 0, None
    for word in words:
        if previous is not None:
            separator = " " if word.line == previous else "\n"
            text.append(separator)
            size += 1
        start = size
        text.append(word.text)
        size += len(word.text)
        placed.append((start, size, word.box))
        if ocr.unverified(word):
            unverified.append((start, size))
        previous = word.line
    return OCRText("".join(text), placed, unverified)
