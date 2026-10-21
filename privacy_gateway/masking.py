import re
from dataclasses import dataclass, field

from .detection import generic_word, overlapping_groups
from .errors import PrivacyError
from .policy import UNVERIFIED
from .schemas import Entity

RESERVED = re.compile(r"\[[A-Z][A-Z_]*(?:_\d+)?\]")
PRIORITY = {"allow": 0, "generalize": 1, "mask": 2, "block": 3}
RULE_SOURCES = {"regex", "context", "dictionary"}
LEGAL_SUFFIX = (
    r"(?i)\b(?:llc|llp|lp|inc|ltd|limited|corp|corporation|co|group|gmbh|ag|plc|pvt|pty|sa|bv)"
    r"\.?[\s.,)]*$"
)


@dataclass(repr=False)
class MaskContext:
    # Request-local only; no persistence, logs, response serialization, or LLM metadata.
    replacements: dict[str, str] = field(default_factory=dict, repr=False)
    originals: set[str] = field(default_factory=set, repr=False)
    tokens: set[str] = field(default_factory=set)
    counters: dict[str, int] = field(default_factory=dict)
    # Sanitized texts that passed input validation; the output check may cite them.
    approved: list[str] = field(default_factory=list, repr=False)
    # Review mode only: per text, what the input check still detected after masking.
    findings: list[list[dict]] = field(default_factory=list, repr=False)
    # The caller's selection of types to mask (policy.selection); kept across clear().
    only: frozenset[str] | None = None

    def clear(self):
        self.replacements.clear()
        self.originals.clear()
        self.tokens.clear()
        self.counters.clear()
        self.approved.clear()
        self.findings.clear()


def literal(value: str) -> str:
    """Pattern for a recurrence of a value: whole words only where the value starts or ends with
    one ("IRS" masked "first-year" as "f[ORGANIZATION_015]t-year")."""
    return (
        (r"(?<!\w)" if re.match(r"\w", value) else "")
        + re.escape(value)
        + (r"(?!\w)" if re.search(r"\w$", value) else "")
    )


def propagate(texts, detections):
    """Once a value is found, protect every literal recurrence across all history messages."""
    values = {}
    for text, entities in zip(texts, detections, strict=True):
        for e in entities:
            key = (text[e.start : e.end].casefold(), e.entity_type)
            # Keep the strongest observed confidence so references cannot inflate weak types.
            confidence = max(e.confidence, values.get(key, ("", 0.0))[1])
            values[key] = (text[e.start : e.end], confidence)
    patterns = [(literal(original), kind, conf) for (_, kind), (original, conf) in values.items()]
    # A registered company is often named later by its first word alone ("Volta Commerce Group,
    # LLC" -> "Volta document production"); GLiNER misses the short form. Only names ending in
    # a legal suffix qualify, and only a distinctive capitalized first word, as a whole word.
    for (_, kind), (original, conf) in values.items():
        head = re.match(r"([A-Z][A-Za-z'-]{3,})\b", original)
        if kind == "ORGANIZATION" and head and not generic_word(head[1]):
            if re.search(LEGAL_SUFFIX, original):
                patterns.append((rf"\b{re.escape(head[1])}\b", kind, conf))
    for text, entities in zip(texts, detections, strict=True):
        for pattern, kind, confidence in patterns:
            for match in re.finditer(pattern, text, re.I):
                entities.append(
                    Entity(
                        start=match.start(),
                        end=match.end(),
                        entity_type=kind,
                        confidence=confidence,
                        source="reference",
                    )
                )


def mask(text, entities, category, policies, context: MaskContext, spans=None):
    """Replace protected groups. With a spans list (local review only), record each replacement
    and placeholder-mask blocked values instead of refusing, so the reviewer can see them."""
    pieces = []
    size = 0
    cursor = 0
    count = 0
    for group in overlapping_groups(entities):
        protected = [
            e for e in group if policies.action(category, e.entity_type, context.only) != "allow"
        ]
        if not protected:
            continue
        winner = max(
            protected,
            key=lambda e: (
                PRIORITY[policies.action(category, e.entity_type, context.only)],
                # A rule's type is exact (an email is an EMAIL even if GLiNER says PERSON);
                # the broad phone pattern is the exception.
                e.source in RULE_SOURCES and e.entity_type != "PHONE",
                e.confidence,
                e.end - e.start,
                e.entity_type != "PHONE",
            ),
        )
        action = policies.action(category, winner.entity_type, context.only)
        if action == "block" and spans is None:
            raise PrivacyError("policy_blocked")
        start, end = min(e.start for e in group), max(e.end for e in group)
        original = text[start:end]
        key = original.casefold()
        if key not in context.replacements:
            if action == "generalize":
                token = policies.config.generalizations[winner.entity_type]
            else:
                number = context.counters.get(winner.entity_type, 0) + 1
                context.counters[winner.entity_type] = number
                token = f"[{winner.entity_type}_{number:03d}]"
            context.replacements[key] = token
        token = context.replacements[key]
        context.tokens.add(token)
        # Unverified OCR words are masked because they could not be read, not because their
        # text is secret; the same word read clearly elsewhere is not a residual value.
        secret = [e for e in protected if e.entity_type != UNVERIFIED]
        if secret:
            context.originals.add(key)
            context.originals.update(text[e.start : e.end].casefold() for e in secret)
        before = text[cursor:start]
        pieces.extend((before, token))
        size += len(before)
        if spans is not None:
            spans.append(
                {
                    "start": start,
                    "end": end,
                    "masked_start": size,
                    "masked_end": size + len(token),
                    "entity_type": winner.entity_type,
                    "action": action,
                    "token": token,
                    "confidence": round(max(e.confidence for e in protected), 3),
                    "sources": sorted({e.source for e in protected}),
                }
            )
        size += len(token)
        cursor = end
        count += 1
    pieces.append(text[cursor:])
    return "".join(pieces), count
