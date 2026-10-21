import math
import os
import re
import threading
from pathlib import Path

from .errors import PrivacyError
from .policy import LABELS
from .schemas import Entity

# Deliberately broad patterns: false positives are preferable to silently ignoring identifiers.
PATTERNS = {
    "EMAIL": r"\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    # An opening area-code bracket belongs to the number ("(212) 555-8800").
    "PHONE": r"(?<!\w)(?:\+?\(?\d[\d ()\-.]{7,}\d)(?!\w)",
    "MRN": r"\bMRN[- ]?\d{3,}\b",
    # SSN; Aadhaar (12 digits) and its VID (16 digits), including the "XXXX XXXX 1234" masked
    # form; voter ID (EPIC); Indian passport; driving licence (state, RTO, year, number).
    "GOVERNMENT_ID": r"\b(?:\d{3}-\d{2}-\d{4}|\d{4}[ -]?\d{4}[ -]?\d{4}(?:[ -]?\d{4})?"
    r"|[X*]{4}[ -]?[X*]{4}[ -]?\d{4}|[A-Z]{3}\d{7}|[A-Z][1-9]\d{6}"
    r"|[A-Z]{2}[ -]?\d{2}[ -]?(?:19|20)\d{2}[ -]?\d{7})\b",
    # Case-sensitive: IBANs print in capitals, and under re.I the tail ran on into prose words.
    "BANK_ACCOUNT": r"(?-i:\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b)",
    "TAX_ID": r"\b(?:[A-Z]{5}\d{4}[A-Z]|\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b",
    # US "City, ST 12345" line. GLiNER masks the street but often misses this part when it sits
    # after a "|" or on the next line of a two-column layout ("New York, NY 10022").
    "ADDRESS": r"(?-i:\b[A-Z][a-z]+(?:[ .'-]+[A-Z][a-z]+){0,3},[ \t]+(?:"
    r"A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|PR|RI"
    r"|S[CD]|T[NX]|UT|V[AT]|W[AIVY])[ \t]+\d{5}(?:-\d{4})?\b)"
    # Indian "City - PIN" line, often the second address line on ID cards ("Kolkata - 700091",
    # "Bengaluru, Karnataka - 560038"), which the address tail does not reach across a line break.
    # The PIN must end the line or a clause, so phone digits ("Call 212 555 8800") do not match.
    r"|(?-i:\b[A-Z][a-z]+(?:,?[ \t]+[A-Z][a-z]+){0,3}[ \t]*(?:[-–,][ \t]*)?[1-9]\d{2}[ \t]?\d{3}"
    r"(?=[ \t]*(?:[\r\n,.;)]|\Z)))",
    # Currency-marked values, plus bare money-shaped numbers in invoice tables ("1,200.00").
    # Values end on a digit so a sentence-final "." or "," is not swallowed.
    "AMOUNT": r"(?:[$€£₹]\s?\d(?:[\d,.]*\d)?|\b(?:USD|INR|EUR|GBP)\s+\d(?:[\d,.]*\d)?"
    r"|(?<![\w.,])(?:\d{1,3}(?:,\d{3})+|\d{3,})\.\d{2}(?![\w.,]))",
    "SECRET": r"\b(?:sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b",
    # Percentage concessions in prose ("a 22 percent volume rebate", "discount of 17%").
    "COMMERCIAL_TERM": r"\b\d+(?:\.\d+)?\s?(?:%|percent)\s+(?:[a-z]+\s+)?"
    r"(?:discount|rebate|commission|markup|margin)\b"
    r"|\b(?:discount|rebate|commission|markup|margin)\s+(?:of\s+|at\s+)?\d+(?:\.\d+)?\s?(?:%|percent)",
}
CONTEXT = {
    # Bare "name" skips qualifiers that have their own label below ("Project name:").
    "PERSON": r"patient(?:[ _]name)?|(?<!project[ _])(?<!company[ _])(?<!organization[ _])name"
    r"|customer(?:[ _]name)?|contact(?:[ _]name)?|surname|elector(?:'s)?(?:[ _]name)?"
    r"|(?:father|mother|husband|spouse|guardian)(?:'s)?(?:[ _]name)?",
    "MRN": r"patient[ _]id|mrn|medical[ _]record(?:[ _]number)?"
    r"|(?:insurance(?:[ _]member)?|member)[ _](?:id|number)",
    "PHONE": r"phone|mobile|telephone",
    "ADDRESS": r"(?:street[ _]|billing[ _]|shipping[ _])?address",
    "GOVERNMENT_ID": r"ssn|government[ _]id|(?:aadhaar|passport|uid|vid|epic|voter[ _]id"
    r"|enrol?ment|(?:driving[ _])?licen[cs]e|dl)(?:[ _](?:no\.?|number))?",
    "BANK_ACCOUNT": r"(?:bank[ _])?account(?:[ _]number)?|iban",
    "TAX_ID": r"tax[ _](?:id|identifier)|pan|gstin",
    "ORGANIZATION": r"party|(?:organization|company)(?:[ _]name)?|confidential[ _]party",
    # Blood group is health data, printed on Indian driving licences.
    "DIAGNOSIS": r"diagnosis|condition|blood[ _]group",
    "AMOUNT": r"(?:amount|(?:sub)?total|balance)(?:[ _]due)?|invoice[ _]amount",
    # A legal matter name ("Matter: Platform Technology Litigation Defense") is the case itself.
    "PROJECT": r"(?:internal[ _])?project(?:[ _]name)?|matter(?:[ _]name)?",
    # Any "Confidential <field>:" label (except parties) and negotiated/budget fields.
    "COMMERCIAL_TERM": r"protected[ _](?:term|terms)|commercial[ _]terms"
    r"|confidential[\s_](?!party)[a-z]+(?:[\s_][a-z]+)?|negotiated[\s_][a-z]+(?:[\s_][a-z]+)?"
    r"|(?:volume[ _])?rebate|budget(?:[ _]ceiling)?|price[ _]cap|rate[ _]card",
    "INTERNAL_INFORMATION": r"internal[ _](?:information|guideline|guidelines|strategy)",
    "SECRET": r"password|secret|api[ _]key|access[ _]token",
}
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATE = (
    rf"(?:\d{{1,2}}[ /.-](?:\d{{1,2}}|{MONTH})[ /.,-]+\d{{2,4}}"
    rf"|\d{{4}}-\d{{1,2}}-\d{{1,2}}|{MONTH}[ \t]+\d{{1,2}},?[ \t]+\d{{4}})"
)
# Labeled values whose label may lack ":" (PDF tables print "Date of birth 14 Apr 1993"). A table
# cell border also separates them ("| Date of birth | 09 March 1984 |").
VALUE_PATTERNS = [
    (
        "DATE_OF_BIRTH",
        rf"(?:\bdate[ _]of[ _]birth|\bbirth[ _]?date|\bdob|\bd\.o\.b\.?"
        rf"|\bborn(?:[ \t]+on)?)(?![a-z])[ \t]*[\"']?[ \t]*[:=|-]?[ \t]*(?:\r?\n[ \t]*)?[\"']?"
        rf"(?P<value>{DATE})",
    ),
    # Indian ID cards often print only the year of birth.
    ("DATE_OF_BIRTH", r"\b(?:year[ _]of[ _]birth|yob)[ \t]*[:=|-]?[ \t]*(?P<value>\d{4})\b"),
    # Relation prefixes on ID cards, with or without a colon ("S/O Ravi Kumar, 12 Lake Road").
    ("PERSON", r"\b[sdwc][ \t]*/[ \t]*o\b[ \t]*:?[ \t]*(?P<value>[^\W\d_][^\n\r,;:]*)"),
]
# Locality parts and a postcode after a detected address ("…, New Delhi – 110000").
ADDRESS_TAIL = (
    r"(?:[ \t]*,[ \t]*[^\W\d][^\n\r,;|•\d]{0,39}?){1,4}[ \t]*[,–—-]?[ \t]*"
    r"(?:\d{3}[ \t]?\d{3}|\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?[ \t]?\d[A-Z]{2})\b"
)
# A house-number segment just before a detected address ("28 Orchid Business Park, Sector 62,
# Gurugram"): GLiNER often starts the address at the locality and leaves the street unmasked.
ADDRESS_HEAD = (
    r"(?:^|(?<=[\s(,;:]))(?P<head>\d{1,5}[A-Za-z]?[ \t]+[^\W\d][^\n\r,;:()]{0,40}?)"
    r"[ \t]*,[ \t]*$"
)
# Placeholder-shaped text written in a document ("<PERSON_001>") is never a value.
EXAMPLE_TOKEN = r"<[A-Z][A-Z_]*(?:_\d+)?>"


def table_label(labels: str) -> str:
    """A label line with at most two leading words ("Facility address", "Insurance member ID").
    A heading marker from layout extraction ("### Patient name") may precede it."""
    return rf"^[ \t]*(?:#{{1,6}}[ \t]+)?(?:[a-z]+[ \t]+){{0,2}}(?P<label>{labels})[ \t]*"


# A labeled value ends at a column gap: a tab or three or more spaces (layout extraction writes
# wide gaps that way), or a table cell border.
COLUMN_GAP = r"\t| {3,}"
TABLE_ROW = re.compile(r"(?m)^\|.*\|[ \t]*$")
TABLE_SEPARATOR = re.compile(r"[ \t]*:?-{3,}:?[ \t]*")


def markdown_tables(text: str):
    """Tables written as "| a | b |" lines. Yields (rows, has_header); a row lists the
    (start, end) offsets of each cell's text, a separator row is not a row."""
    rows, header, previous = [], False, None
    for match in [*TABLE_ROW.finditer(text), None]:
        if match is None or (previous is not None and match.start() != previous + 1):
            if rows:
                yield rows, header
            rows, header = [], False
        if match is None:
            break
        previous = match.end()
        bars = [match.start() + i for i, c in enumerate(match[0]) if c == "|"]
        cells = []
        for left, right in zip(bars, bars[1:], strict=False):
            piece = text[left + 1 : right]
            start = left + 1 + len(piece) - len(piece.lstrip())
            cells.append((start, max(start, left + 1 + len(piece.rstrip()))))
        if cells and all(TABLE_SEPARATOR.fullmatch(text[s:e] or "-") for s, e in cells):
            header = header or len(rows) == 1
            continue
        rows.append(cells)
    return


def label_kind(cell: str):
    """The CONTEXT type whose label this whole table cell is ("Patient name", "Phone:")."""
    cell = cell.strip().rstrip(":").strip()
    for kind, labels in CONTEXT.items():
        if re.fullmatch(rf"(?:[a-z]+[ \t]+){{0,2}}(?:{labels})", cell, re.I):
            return kind
    return None


def table_entities(text: str):
    """Values under label columns ("| Name | Phone |" header) or beside label cells
    ("| Phone | 98765 43210 |" rows), plus the label cells' ranges."""
    entities, labels = [], []
    for rows, header in markdown_tables(text):
        first = [
            label_kind(text[s:e]) if len(row) >= 2 else None for row in rows for s, e in row[:1]
        ]
        label_rows = sum(map(bool, first)) * 2 >= len(rows)
        kinds = [label_kind(text[s:e]) for s, e in rows[0]] if header else []
        # Two label cells in a header row ("| Name | Phone | Ward |") name label columns. One
        # label among three or more columns does too, unless the rows themselves start with labels.
        strong = sum(map(bool, kinds)) >= 2
        weak = len(kinds) >= 3 and any(kinds) and not label_rows
        if strong or weak:
            pairs = [
                (cell, kind, label)
                for row in rows[1:]
                for cell, kind, label in zip(row, kinds, rows[0], strict=False)
                if kind
            ]
            labels += [label for label, kind in zip(rows[0], kinds, strict=True) if kind]
        elif label_rows:
            pairs = [(row[1], kind, row[0]) for row, kind in zip(rows, first, strict=True) if kind]
        else:
            continue
        for (start, end), kind, label in pairs:
            labels.append(label)
            if start < end:
                entities.append(
                    Entity(start=start, end=end, entity_type=kind, confidence=1.0, source="context")
                )
    return entities, labels


# Prefixed document and ticket numbers ("INV-2026-00417", "PO-44718", "CS-20431"). GLiNER labels
# these as bank accounts, phones, projects, or organizations, and the phone pattern matches their
# digits. Model hits and those two rule misreads are dropped inside them; other rules (e.g. SSN
# shapes) still apply.
REFERENCE = r"\b(?:INV|PO|ORD|SO|QUO|CN|CS|SR|INC|TKT|CASE|REQ)[-/#]?\d(?:[\d/-]*\d)?\b"
# Any prefix after an explicit number label ("Invoice No: MB-2025-10477", "Matter No: VCG-...").
LABELLED_REFERENCE = (
    r"(?i:\b(?:invoice|matter|order|ticket|case|reference|ref)[ \t]*(?:no\.?|number|#)[ \t]*"
    r"[:#]?[ \t]*)(?P<ref>[A-Z0-9]+(?:[-/][A-Z0-9]+)+)\b"
)
REFERENCE_MISREADS = {"PHONE", "BANK_ACCOUNT"}


# GLiNER often tags pronouns, roles, and headings ("her", "All staff", "Expense Policy") as
# entities. A model-only span made entirely of these words cannot identify anyone, so it is
# ignored. Rule, context, and dictionary detections are never filtered by this list.
GENERIC_WORDS = frozenset(
    """he she her hers him his they them their theirs we us our ours you your yours i me my mine
    it its all the a an any each every both this that these those everyone someone anyone
    customer customers client clients patient patients staff employee employees manager managers
    team teams member members user users vendor vendors supplier suppliers buyer buyers seller
    sellers party parties company companies organization organisation department departments
    helpdesk doctor doctors physician physicians nurse nurses clinician clinicians caregiver
    applicant tenant landlord contractor consultant provider providers recipient sender owner
    administrator admin hr finance legal support sales management board director directors
    billing security remote work guideline guidelines policy policies standard procedure
    handbook notice invoice agreement contract document section clause service services email
    phone address name date total amount item items payment information expense expenses travel
    daughter son mother father husband wife family consulting consultancy implementation
    training maintenance hosting cloud software hardware licence license licences licenses
    subscription development installation professional managed onsite renewal migration data
    hours due balance subtotal description quantity unit price rate fee fees discount hi hello
    dear new success general medicine medical paid unpaid partially owed outstanding remaining
    still method upi cash card cheque check neft imps rtgs transfer inr usd eur gbp
    currency diagnostic diagnostics test tests lab labs laboratory screening checkup
    pricing terms credentials operational operating related restricted public non illustrative identifying
    identity linked to diagnosis privacy quality lead facility assigned
    procedures nursing monitoring care consultation consultations observation room charge charges
    medicines prescribed prescription visit visits diagnoses mrn dob id list metric unpublished
    specific record number telephone personnel male female gender sex transgender delivery
    representative representatives authorized authorised signatory signatories for of and on
    behalf external ai hospital clinic insurer insurance ward coordination account agent
    speaker caller assistant market third partner associate senior junior mid level analyst
    paralegal specialist counsel special equity patent scientist technical litigation tech sr
    ediscovery tel fax vp svp evp ceo cfo cto coo generative platform cite checking authority
    authorities firm overhead rent utilities utility technology aadhaar elector specimen driving
    transport""".split()
)


def adds_nothing(text: str, entity: Entity, found: list[Entity]) -> bool:
    """A model span whose only text outside same-type rule matches is spacing, punctuation, or
    generic words. Dropping it keeps table cells apart ("$1,050   $2,100.00" was one AMOUNT, and
    "Standard      $78.50" swallowed the Market column) while the rule matches still mask."""
    if entity.source != "gliner":
        return False
    rest = list(text[entity.start : entity.end])
    covered = False
    for rule in found:
        if rule.source == "regex" and rule.entity_type == entity.entity_type:
            if entity.start <= rule.start and rule.end <= entity.end:
                rest[rule.start - entity.start : rule.end - entity.start] = " " * (
                    rule.end - rule.start
                )
                covered = True
    rest = "".join(rest)
    words = re.findall(r"[^\W\d_]+", rest)
    return covered and not re.search(r"\d", rest) and all(map(generic_word, words))


def word_end(text: str, start: int, width: int) -> int:
    """Shrink a window to end at whitespace: a word cut in half reads as a different word
    ("briefly" became "iefly", labelled an organization). Unbroken text keeps the hard cut."""
    if start + width >= len(text):
        return width
    cut = width
    while cut > width // 2 and not text[start + cut].isspace():
        cut -= 1
    return cut if cut > width // 2 else width


def word_start(text: str, start: int, step: int) -> int:
    """Next window start, moved back to the beginning of the word it would split."""
    begin = start + step
    while begin > start + 1 and not text[begin - 1].isspace():
        begin -= 1
    return begin if begin > start + 1 else start + step


def generic_word(word: str) -> bool:
    """Listed word, or a plural of one ("names", "MRNs", "diagnoses" is listed directly)."""
    word = word.casefold()
    return (
        word in GENERIC_WORDS
        or (word.endswith("s") and word[:-1] in GENERIC_WORDS)
        or (word.endswith("es") and word[:-2] in GENERIC_WORDS)
    )


def is_generic(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text)
    return bool(words) and not re.search(r"\d", text) and all(map(generic_word, words))


class RuleDetector:
    mode = "rules"

    def __init__(self, custom_terms=None):
        self.custom_terms = custom_terms or {}

    def detect(self, text: str) -> list[Entity]:
        found = []
        for kind, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text, re.I):
                if kind == "PHONE" and len(re.sub(r"\D", "", match[0])) < 9:
                    continue
                found.append(
                    Entity(
                        start=match.start(),
                        end=match.end(),
                        entity_type=kind,
                        confidence=0.95,
                        source="regex",
                    )
                )
        for kind, pattern in VALUE_PATTERNS:
            for match in re.finditer(pattern, text, re.I):
                found.append(
                    Entity(
                        start=match.start("value"),
                        end=match.end("value"),
                        entity_type=kind,
                        confidence=1.0,
                        source="regex",
                    )
                )
        for kind, labels in CONTEXT.items():
            patterns = (
                rf"(?im)\b(?:{labels})[ \t]*[\"']?[ \t]*[:=][ \t]*[\"']?(?P<value>[^\n\r;\"}}|]+)",
                # Table layout (DOCX/PDF): a short label line, value on the next line.
                rf"(?im){table_label(labels)}\r?\n[ \t]*(?P<value>[^\n\r;\"}}|]+)",
            )
            for match in (m for pattern in patterns for m in re.finditer(pattern, text)):
                raw = match["value"]
                lead = len(raw) - len(raw.lstrip())
                # The next column is not part of this value ("Patient: Mira Vale     Ward 7").
                value = re.split(COLUMN_GAP, raw[lead:])[0].strip().rstrip(",")
                if not value:
                    continue
                start = match.start("value") + lead
                found.append(
                    Entity(
                        start=start,
                        end=start + len(value),
                        entity_type=kind,
                        confidence=1.0,
                        source="context",
                    )
                )
        found.extend(table_entities(text)[0])
        for kind, values in self.custom_terms.items():
            for value in values:
                if not value:
                    raise PrivacyError("invalid_policy")
                # Any whitespace between words: PDF/DOCX extraction may wrap a term.
                pattern = r"\s+".join(map(re.escape, value.split()))
                for match in re.finditer(pattern, text, re.I):
                    found.append(
                        Entity(
                            start=match.start(),
                            end=match.end(),
                            entity_type=kind,
                            confidence=1.0,
                            source="dictionary",
                        )
                    )
        return found


class GLiNERDetector:
    mode = "gliner"

    def __init__(self, model_path: Path, threshold: float):
        # No request-time model downloads, remote code, or hosted detection.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        if not (model_path / "gliner_config.json").is_file():
            raise PrivacyError("local_model_unavailable", 503)
        import torch
        from gliner import GLiNER

        torch.set_num_threads(min(4, os.cpu_count() or 1))

        self.model = GLiNER.from_pretrained(
            str(model_path), local_files_only=True, load_tokenizer=True, map_location="cpu"
        )
        self.model.eval()
        self.threshold = threshold
        self.lock = threading.Lock()

    def detect(self, text: str) -> list[Entity]:
        found = []
        # Bound wordpieces as well as characters; leave room for entity labels/special tokens.
        # Overlap covers boundary entities, but spans longer than the overlap can still be missed.
        with self.lock:
            start = 0
            while start < len(text):
                width = min(600, len(text) - start)
                processor = self.model.data_processor

                def token_count(candidate):
                    words = [word for word, _, _ in processor.words_splitter(candidate)]
                    if not words:
                        return 0
                    return len(
                        processor.transformer_tokenizer(
                            words,
                            is_split_into_words=True,
                            add_special_tokens=True,
                            truncation=False,
                        )["input_ids"]
                    )

                while token_count(text[start : start + width]) > 200:
                    width //= 2
                    if width < 16:
                        raise PrivacyError("text_not_safely_tokenizable")
                width = word_end(text, start, width)
                chunk = text[start : start + width]
                predictions = self.model.predict_entities(
                    chunk, list(LABELS), threshold=self.threshold
                )
                for p in predictions:
                    begin, end, score = int(p["start"]), int(p["end"]), float(p["score"])
                    if not (0 <= begin < end <= len(chunk)) or not math.isfinite(score):
                        raise PrivacyError("invalid_detector_result", 503)
                    found.append(
                        Entity(
                            start=start + begin,
                            end=start + end,
                            entity_type=LABELS[p["label"]],
                            confidence=score,
                            source="gliner",
                        )
                    )
                if start + width >= len(text):
                    break
                start = word_start(text, start, max(1, width - min(200, width // 3)))
        return found


class DetectionEngine:
    def __init__(self, rules: RuleDetector, model=None):
        self.rules = rules
        self.model = model
        self.mode = "gliner" if model else "rules"

    def detect(self, text: str) -> list[Entity]:
        try:
            found = self.rules.detect(text)
            if self.model:
                # Fixed schema labels such as "Patient:" are not field values. GLiNER can
                # misclassify these, especially after values have been replaced with blanks.
                # Exempt only exact known labels immediately followed by a field delimiter.
                labels = "|".join(f"(?:{label})" for label in CONTEXT.values())
                key_ranges = [
                    (m.start(), m.end())
                    for m in re.finditer(rf"(?im)\b(?:{labels})[ \t]*[\"']?[ \t]*[:=]", text)
                ]
                # Table label lines: only the label itself, not the leading words, is exempt.
                key_ranges += [
                    (m.start("label"), m.end("label"))
                    for m in re.finditer(rf"(?im){table_label(labels)}(?=\r?\n)", text)
                ]
                key_ranges += [(m.start(), m.end()) for m in re.finditer(EXAMPLE_TOKEN, text)]
                # Label cells of tables ("| Phone | … |"), the same labels in table form.
                key_ranges += table_entities(text)[1]
                found.extend(
                    e
                    for e in self.model.detect(text)
                    if not any(start <= e.start and e.end <= end for start, end in key_ranges)
                    and not is_generic(text[e.start : e.end])
                )
            for e in [e for e in found if e.entity_type == "ADDRESS"]:
                tail = re.match(ADDRESS_TAIL, text[e.end :])
                if tail:
                    found.append(e.model_copy(update={"end": e.end + tail.end()}))
                window = max(0, e.start - 60)
                head = re.search(ADDRESS_HEAD, text[window : e.start])
                if head:
                    found.append(e.model_copy(update={"start": window + head.start("head")}))
            references = [(m.start(), m.end()) for m in re.finditer(REFERENCE, text)]
            references += [
                (m.start("ref"), m.end("ref")) for m in re.finditer(LABELLED_REFERENCE, text)
            ]
            found = [
                e
                for e in found
                if e.source in ("context", "dictionary")
                or (e.source == "regex" and e.entity_type not in REFERENCE_MISREADS)
                or not any(start <= e.start and e.end <= end for start, end in references)
            ]
            found = [e for e in found if not adds_nothing(text, e, found)]
            unique = {(e.start, e.end, e.entity_type, e.source): e for e in found}
            return sorted(unique.values(), key=lambda e: (e.start, e.end, e.entity_type))
        except PrivacyError:
            raise
        except Exception:
            raise PrivacyError("detector_unavailable", 503) from None


def overlapping_groups(entities: list[Entity]) -> list[list[Entity]]:
    """Union all overlapping ranges; never discard a partially overlapped sensitive suffix."""
    groups = []
    end = -1
    for entity in sorted(entities, key=lambda e: (e.start, e.end)):
        if entity.start >= end:
            groups.append([entity])
            end = entity.end
        else:
            groups[-1].append(entity)
            end = max(end, entity.end)
    return groups
