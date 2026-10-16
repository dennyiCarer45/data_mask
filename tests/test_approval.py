import copy

import pytest

from privacy_gateway.approval import ApprovalVerifier, sign
from privacy_gateway.errors import PrivacyError

KEY = "synthetic-key-" + "x" * 40
PAYLOAD = {
    "model": "privacy-mock",
    "messages": [{"role": "user", "content": "[PERSON_001]"}],
    "max_tokens": 64,
    "stream": False,
}


def test_ticket_integrity_expiry_replay():
    verifier = ApprovalVerifier(KEY)
    ticket = sign(PAYLOAD, KEY, now=100)
    verifier.verify(PAYLOAD, ticket, now=100)
    with pytest.raises(PrivacyError):
        verifier.verify(PAYLOAD, ticket, now=100)
    with pytest.raises(PrivacyError):
        ApprovalVerifier(KEY).verify(PAYLOAD, ticket, now=200)
    changed = copy.deepcopy(PAYLOAD)
    changed["messages"][0]["content"] = "Patient: John Smith"
    with pytest.raises(PrivacyError):
        ApprovalVerifier(KEY).verify(changed, ticket, now=100)


async def test_actual_hook_rejects_missing_ticket(monkeypatch):
    monkeypatch.setenv("PRIVACY_SIGNING_KEY", KEY)
    from fastapi import HTTPException

    from privacy_gateway.proxy_hook import ApprovalHook

    with pytest.raises(HTTPException):
        await ApprovalHook().async_pre_call_hook(None, None, copy.deepcopy(PAYLOAD), "completion")


async def test_actual_hook_accepts_only_signed_body(monkeypatch):
    monkeypatch.setenv("PRIVACY_SIGNING_KEY", KEY)
    from fastapi import HTTPException

    from privacy_gateway.proxy_hook import ApprovalHook

    payload = copy.deepcopy(PAYLOAD)
    payload["metadata"] = {"privacy_approval": sign(PAYLOAD, KEY)}
    payload["proxy_server_request"] = {"body": copy.deepcopy(payload)}
    result = await ApprovalHook().async_pre_call_hook(None, None, payload, "completion")
    assert "privacy_approval" not in result["metadata"]
    assert "proxy_server_request" not in result
    payload = copy.deepcopy(PAYLOAD)
    payload["metadata"] = {"privacy_approval": sign(PAYLOAD, KEY)}
    payload["extra_body"] = {"messages": ["raw"]}
    with pytest.raises(HTTPException):
        await ApprovalHook().async_pre_call_hook(None, None, payload, "completion")


async def test_hook_discards_unsigned_metadata_and_unknown_provider_fields(monkeypatch):
    monkeypatch.setenv("PRIVACY_SIGNING_KEY", KEY)
    from privacy_gateway.proxy_hook import ApprovalHook

    payload = copy.deepcopy(PAYLOAD)
    payload["metadata"] = {"privacy_approval": sign(PAYLOAD, KEY), "private_note": "Mira Vale"}
    payload["future_provider_option"] = "9876543210"
    payload["extra_headers"] = {"X-Private": "Mira Vale"}
    result = await ApprovalHook().async_pre_call_hook(None, None, payload, "acompletion")
    assert set(result) == set(PAYLOAD) | {"metadata"}
    assert result["metadata"] == {}
    assert "Mira Vale" not in str(result) and "9876543210" not in str(result)
