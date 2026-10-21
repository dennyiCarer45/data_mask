from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from .errors import PrivacyError

LABELS = {
    "person": "PERSON",
    "email address": "EMAIL",
    "phone number": "PHONE",
    "street address": "ADDRESS",
    "medical record number": "MRN",
    "government identifier": "GOVERNMENT_ID",
    "bank account number": "BANK_ACCOUNT",
    "tax identifier": "TAX_ID",
    "organization": "ORGANIZATION",
    "project name": "PROJECT",
    "diagnosis": "DIAGNOSIS",
    "monetary amount": "AMOUNT",
    "confidential commercial term": "COMMERCIAL_TERM",
    "confidential organizational information": "INTERNAL_INFORMATION",
    "api key": "SECRET",
}
# Rule-only types have no GLiNER label; adding labels changes every model prediction.
TYPES = set(LABELS.values()) | {"DATE_OF_BIRTH", "UNVERIFIED_TEXT"}
# OCR words that could not be read confidently, or are in a script the detectors do not cover.
# Their content is unknown, so they can never be allowed or generalized.
UNVERIFIED = "UNVERIFIED_TEXT"
Action = Literal["allow", "mask", "generalize", "block"]


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    defaults: dict[str, Action]
    categories: dict[str, dict[str, Action]]
    generalizations: dict[str, str]
    custom_terms: dict[str, list[str]]


class Policies:
    def __init__(self, path: Path):
        self.config = PolicyFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        c = self.config
        if set(c.defaults) != TYPES or set(c.categories) != {
            "general",
            "healthcare",
            "invoices",
            "agreements",
            "guidelines",
        }:
            raise ValueError("invalid_policy_configuration")
        for overrides in c.categories.values():
            if not set(overrides) <= TYPES:
                raise ValueError("invalid_policy_entity")
        if not set(c.custom_terms) <= TYPES:
            raise ValueError("invalid_custom_terms")
        for category in c.categories:
            if self.action(category, UNVERIFIED) not in ("mask", "block"):
                raise ValueError("unverified_text_must_be_masked")
            for kind in TYPES:
                if self.action(category, kind) == "generalize":
                    token = c.generalizations.get(kind, "")
                    if not token or not token.startswith("[") or not token.endswith("]"):
                        raise ValueError("generalization_token_required")

    def action(self, category: str, entity_type: str, only=None) -> Action:
        """only: a caller's selection of types to protect; every other type is allowed. Blocked
        types and unverified OCR text keep the configured action whatever the caller selects."""
        if category not in self.config.categories or entity_type not in TYPES:
            raise PrivacyError("invalid_policy")
        action = self.config.categories[category].get(
            entity_type, self.config.defaults[entity_type]
        )
        if only is None or action == "block" or entity_type == UNVERIFIED:
            return action
        if entity_type not in only:
            return "allow"
        return "mask" if action == "allow" else action


def selection(types) -> frozenset[str] | None:
    """A caller's list of types to mask; None keeps the server policy unchanged."""
    if types is None:
        return None
    if not types or not set(types) <= TYPES:
        raise PrivacyError("invalid_mask_types", 422)
    return frozenset(types)
