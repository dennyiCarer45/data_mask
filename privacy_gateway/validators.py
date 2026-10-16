import os
import re

# Configure before importing Guardrails. Validators run locally; no Hub installs or reasks.
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["GUARDRAILS_NO_TELEMETRY"] = "1"

from guardrails.classes.rc import RC  # noqa: E402
from guardrails.settings import settings as guardrails_settings  # noqa: E402
from guardrails.validators import FailResult, PassResult, Validator, register_validator  # noqa: E402

guardrails_settings.rc = RC(enable_metrics=False, use_remote_inferencing=False)
guardrails_settings.disable_tracing = True
guardrails_settings.use_server = False

from .detection import is_generic  # noqa: E402
from .masking import RESERVED, literal  # noqa: E402


def only_approved_words(text: str, approved: list[str]) -> bool:
    """True if text has no digits and each word, or its singular/plural, occurs in approved."""
    words = re.findall(r"[^\W\d_]+", text.casefold())
    if not approved or not words or re.search(r"\d", text):
        return False
    vocabulary = set(re.findall(r"[^\W\d_]+", " ".join(approved)))
    return all(
        word in vocabulary or word[:-1] in vocabulary or word + "s" in vocabulary for word in words
    )


@register_validator(name="local-input-privacy", data_type="string")
class InputPrivacyValidator(Validator):
    def __init__(self, engine, policies):
        super().__init__(on_fail="exception", use_local=True)
        self.engine, self.policies = engine, policies

    def _validate(self, value, metadata):
        # Local review passes a list to receive every failure (reason, offsets, type) instead
        # of stopping at the first. The pass/fail outcome is the same either way.
        findings = metadata.get("findings")
        try:
            context = metadata["context"]
            # Blank only placeholders generated in this request, preserving offsets.
            visible = RESERVED.sub(
                lambda m: " " * len(m[0]) if m[0] in context.tokens else m[0], value
            )
            for match in RESERVED.finditer(visible):
                if findings is None:
                    return FailResult(error_message="unrecognized_placeholder")
                findings.append(finding("unrecognized_placeholder", match.start(), match.end()))
            folded = visible.casefold()
            for original in context.originals:
                # Same whole-word rule as masking, so "irs" inside "first" is not a residual.
                match = re.search(literal(original), folded)
                if not match:
                    continue
                if findings is None:
                    return FailResult(error_message="residual_sensitive_value")
                start = match.start()
                findings.append(finding("residual_sensitive_value", start, start + len(original)))
            # Re-detect with placeholders in place: blanked values make GLiNER misread field
            # labels such as "Email:" as entities. Only this request's placeholder characters
            # are discounted; any other text inside a detected span is still checked.
            for entity in self.engine.detect(value):
                if self.policies.action(metadata["category"], entity.entity_type) == "allow":
                    continue
                remainder = visible[entity.start : entity.end]
                if not re.search(r"\w", remainder):
                    continue
                if entity.source == "gliner" and is_generic(remainder):
                    continue
                # A labeled value that is exactly this request's placeholder, closed by
                # punctuation, was masked; context capture runs on to the end of the line.
                # "Name: [PERSON_001] Smith" still fails. Trailing text is scanned separately.
                if entity.source == "context" and any(
                    re.match(re.escape(token) + r"[ \t]*(?:[).,;\r\n]|$)", value[entity.start :])
                    for token in context.tokens
                ):
                    continue
                # Model text already present in the approved sanitized input reveals nothing
                # new (e.g. "CBC" repeated by the provider). Empty during input validation.
                found = value[entity.start : entity.end].casefold()
                if entity.source == "gliner" and any(found in text for text in context.approved):
                    continue
                # Likewise a digit-free model span whose every visible word was already sent
                # (provider rewording such as "record numbers", "internal paths").
                if entity.source == "gliner" and only_approved_words(remainder, context.approved):
                    continue
                if findings is None:
                    return FailResult(error_message="residual_sensitive_entity")
                findings.append(
                    finding("residual_sensitive_entity", entity.start, entity.end, entity)
                )
            if findings:
                return FailResult(error_message=findings[0]["reason"])
            return PassResult()
        except Exception:
            return FailResult(error_message="privacy_validation_failed")


def finding(reason, start, end, entity=None):
    """Offsets into the checked (masked) text; never the original value."""
    return {
        "reason": reason,
        "masked_start": start,
        "masked_end": end,
        "entity_type": entity.entity_type if entity else None,
        "source": entity.source if entity else None,
        "confidence": round(entity.confidence, 3) if entity else None,
    }


@register_validator(name="local-output-privacy", data_type="string")
class OutputPrivacyValidator(InputPrivacyValidator):
    """Same local checks apply before any model response is delivered to a client."""
