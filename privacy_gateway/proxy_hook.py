"""Mandatory OSS LiteLLM pre-call callback; never use a concurrent moderation hook."""

import os

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from .approval import FIELDS, ApprovalVerifier


class ApprovalHook(CustomLogger):
    enforces_request_content = True

    def __init__(self):
        super().__init__()
        self.verifier = ApprovalVerifier(os.environ.get("PRIVACY_SIGNING_KEY", ""))

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        stage = "call_type"
        try:
            if call_type not in {"completion", "acompletion"} or data.get("stream") is not False:
                raise ValueError()
            # Some LiteLLM configurations retain a body snapshot; message logging disables it.
            # When present, inspect it too. The outgoing object is always rebuilt below.
            stage = "body_shape"
            body = data.get("proxy_server_request", {}).get("body")
            if body is not None and (
                not isinstance(body, dict) or set(body) != FIELDS | {"metadata"}
            ):
                raise ValueError()
            stage = "unsupported_parameters"
            forbidden = {
                "tools",
                "tool_choice",
                "functions",
                "function_call",
                "extra_body",
                "api_base",
                "api_key",
                "base_url",
                "custom_llm_provider",
                "user",
                "response_format",
                "prompt",
                "input",
                "system",
                "attachments",
            }
            if any(key in data for key in forbidden):
                raise ValueError()
            stage = "envelope"
            payload = {key: data[key] for key in FIELDS}
            if payload["model"] != os.environ.get("PRIVACY_PROXY_MODEL", "privacy-mock"):
                raise ValueError()
            for message in payload["messages"]:
                if set(message) != {"role", "content"} or not isinstance(message["content"], str):
                    raise ValueError()
            stage = "signature"
            ticket = data.get("metadata", {}).get("privacy_approval")
            self.verifier.verify(payload, ticket)
            # A strict outbound allowlist also covers unknown future provider parameters and
            # untrusted metadata/headers. Preserve only LiteLLM's internal logging handles.
            internal = {
                key: data[key] for key in ("litellm_call_id", "litellm_logging_obj") if key in data
            }
            data.clear()
            data.update(payload)
            data.update(internal)
            data["metadata"] = {}
            return data
        except Exception:
            raise HTTPException(403, "privacy_approval_required_" + stage) from None


approval_hook = ApprovalHook()
