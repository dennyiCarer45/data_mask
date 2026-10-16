import json
from urllib.parse import urlparse

import httpx

from .approval import sign
from .errors import PrivacyError


class ProxyTransport:
    def __init__(self, settings):
        self.settings = settings
        url = urlparse(settings.proxy_url)
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "proxy"}:
            raise PrivacyError("proxy_address_not_allowed", 503)
        if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise PrivacyError("proxy_address_not_allowed", 503)
        if settings.proxy_model != "privacy-mock" and not settings.external_enabled:
            raise PrivacyError("external_models_disabled", 503)
        if settings.detector != "gliner" and settings.proxy_model != "privacy-mock":
            raise PrivacyError("full_detector_required", 503)

    async def chat(self, messages, max_tokens):
        s = self.settings
        payload = {
            "model": s.proxy_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        payload["metadata"] = {"privacy_approval": sign(payload, s.signing_key.get_secret_value())}
        try:
            async with httpx.AsyncClient(
                timeout=60, trust_env=False, follow_redirects=False
            ) as client:
                async with client.stream(
                    "POST",
                    s.proxy_url.rstrip("/") + "/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer " + s.proxy_key.get_secret_value()},
                ) as response:
                    if response.status_code != 200:
                        raise PrivacyError("upstream_unavailable", 502)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > s.max_output_chars * 8 + 10000:
                            raise PrivacyError("upstream_response_too_large", 502)
            data = json.loads(raw)
            choices = data["choices"]
            if len(choices) != 1:
                raise ValueError()
            message = choices[0]["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ValueError()
            text = message["content"]
            if not isinstance(text, str) or not text.strip() or len(text) > s.max_output_chars:
                raise ValueError()
            return text
        except PrivacyError:
            raise
        except Exception:
            raise PrivacyError("upstream_unavailable", 502) from None
