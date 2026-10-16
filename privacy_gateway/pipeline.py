import re

from .documents import check_text
from .errors import PrivacyError
from .masking import RESERVED, MaskContext, mask, propagate
from .policy import UNVERIFIED
from .schemas import Entity
from .validators import InputPrivacyValidator, OutputPrivacyValidator, PassResult


class Pipeline:
    def __init__(self, engine, policies, max_chars=32000):
        self.engine, self.policies, self.max_chars = engine, policies, max_chars
        self.input_validator = InputPrivacyValidator(engine, policies)
        self.output_validator = OutputPrivacyValidator(engine, policies)

    def sanitize(self, texts: list[str], category: str, unverified=None, spans=None):
        """unverified: per text, OCR word ranges that must be masked whatever detection finds.
        spans: a list to receive one list of replacements per text (local review only)."""
        context = MaskContext()
        try:
            if sum(map(len, texts)) > self.max_chars:
                raise PrivacyError("text_too_large", 413)
            for text in texts:
                check_text(text, self.max_chars)
                if RESERVED.search(text):
                    raise PrivacyError("reserved_placeholder_in_input")
            detected = [self.engine.detect(text) for text in texts]
            # Mask again once: with values replaced, the model reads the text differently and can
            # find names it missed ("Hilton Garden Inn NYC"). Those are added and the originals
            # re-masked from scratch. The fresh validation pass below still decides.
            for second_look in (False, True):
                context.clear()
                recorded = [] if spans is not None else None
                sanitized, counts = self._mask(
                    texts, detected, category, unverified, context, recorded
                )
                extra = [] if second_look else self._missed(texts, sanitized, category, context)
                if not any(extra):
                    break
                for entities, more in zip(detected, extra, strict=True):
                    entities.extend(more)
            if spans is not None:
                spans.extend(recorded)
            for text in sanitized:
                # Review mode records what the check still detects instead of refusing, so the
                # reviewer can see it; sending always runs the refusing check.
                found = [] if spans is not None else None
                self.validate(text, category, context, findings=found)
                if found is not None:
                    context.findings.append(found)
            context.approved.extend(text.casefold() for text in sanitized)
            return sanitized, counts, context
        except Exception:
            context.clear()
            raise

    def _mask(self, texts, detected, category, unverified, context, spans):
        detections = [list(entities) for entities in detected]
        propagate(texts, detections)
        # Added after propagation: an unreadable word is not a value to hunt elsewhere.
        for entities, ranges in zip(detections, unverified or [], strict=False):
            entities.extend(
                Entity(start=start, end=end, entity_type=UNVERIFIED, confidence=0.0, source="ocr")
                for start, end in ranges
            )
        sanitized, counts = [], []
        for text, entities in zip(texts, detections, strict=True):
            recorded = [] if spans is not None else None
            result, count = mask(text, entities, category, self.policies, context, recorded)
            if spans is not None:
                spans.append(recorded)
            sanitized.append(result)
            counts.append(count)
        return sanitized, counts

    def _missed(self, texts, sanitized, category, context):
        """Protected values the detector finds only in masked text, as entities on the originals.
        A hit touching a placeholder ("[PERSON_001] Okafor") is left for validation to refuse."""
        values = {}
        for text in sanitized:
            for e in self.engine.detect(text):
                piece = text[e.start : e.end]
                if RESERVED.search(piece) or not piece.strip():
                    continue
                if self.policies.action(category, e.entity_type) == "allow":
                    continue
                key = (piece.casefold(), e.entity_type)
                values[key] = max(e.confidence, values.get(key, 0.0))
        return [
            [
                Entity(
                    start=m.start(),
                    end=m.end(),
                    entity_type=kind,
                    confidence=confidence,
                    source="second_look",
                )
                for (piece, kind), confidence in values.items()
                for m in re.finditer(re.escape(piece), text, re.I)
            ]
            for text in texts
        ]

    def validate(self, text, category, context, output=False, findings=None):
        validator = self.output_validator if output else self.input_validator
        try:
            # Invoke the real Guardrails Validator API directly. No Guard history retains raw data.
            metadata = {"category": category, "context": context}
            if findings is not None:
                metadata["findings"] = findings
            result = validator.validate(text, metadata)
            # Review mode tolerates recorded findings, never an incomplete check.
            incomplete = getattr(result, "error_message", "") == "privacy_validation_failed"
            if not isinstance(result, PassResult) and (not findings or incomplete):
                raise PrivacyError(
                    "output_validation_failed" if output else "input_validation_failed"
                )
        except PrivacyError:
            raise
        except Exception:
            raise PrivacyError("privacy_validator_unavailable", 503) from None
