import base64
import hashlib
import hmac
import json
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .config import Settings
from .detection import DetectionEngine, GLiNERDetector, RuleDetector, overlapping_groups
from .documents import (
    DocumentProcessor,
    Extracted,
    check_text,
    invalid_constant,
    json_text,
    unique_object,
)
from .errors import PrivacyError
from .ocr import TesseractOCR
from .pipeline import Pipeline
from .policy import Policies, selection
from .redaction import render_masked
from .schemas import Category, ChatRequest, TextRequest
from .transport import ProxyTransport

# The review page is static and holds no data; it asks for the API key and calls the API.
PUBLIC_PATHS = {"/health", "/review"}
REVIEW_PAGE = Path(__file__).with_name("review.html")
REVIEW_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src blob: data:; frame-src blob:; object-src blob:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def form_types(raw: str | None) -> frozenset[str] | None:
    """A form's comma-separated mask field ("PERSON,EMAIL") as a caller selection. An empty
    field (a curl config with no MASK set) keeps the server policy."""
    return None if not raw or not raw.strip() else selection([x.strip() for x in raw.split(",")])


def echo(only):
    return None if only is None else sorted(only)


class PrivateBoundary:
    """Authenticate and bound bodies before FastAPI parses uploads or validation errors."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        supplied = headers.get(b"authorization", b"")
        key = self.settings.api_key.get_secret_value()
        if len(key) < 32 or not hmac.compare_digest(supplied, ("Bearer " + key).encode()):
            return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > self.settings.max_body_bytes:
                return await JSONResponse({"error": "request_too_large"}, 413)(scope, receive, send)
            if not event.get("more_body", False):
                break
        if headers.get(b"content-type", b"").split(b";")[0] == b"application/json":
            try:
                json.loads(body, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            except Exception:
                return await JSONResponse({"error": "invalid_json"}, 422)(scope, receive, send)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def create_app(settings=None, engine=None, transport=None, ocr=None):
    s = settings or Settings()
    policies = Policies(s.policies_path)

    def start_ocr():
        if ocr is not None or s.ocr == "off":
            return ocr
        try:
            return TesseractOCR(
                "tesseract", s.ocr_languages, s.ocr_min_confidence, s.ocr_timeout_seconds
            )
        except PrivacyError:
            # Fail closed per request: images are refused and image-bearing PDFs rejected.
            return None

    @asynccontextmanager
    async def lifespan(app):
        if len(s.api_key.get_secret_value()) < 32:
            raise RuntimeError("PRIVACY_API_KEY must contain at least 32 characters")
        active_engine = engine
        if active_engine is None:
            model = (
                GLiNERDetector(s.model_path, s.gliner_threshold) if s.detector == "gliner" else None
            )
            active_engine = DetectionEngine(RuleDetector(policies.config.custom_terms), model)
        app.state.pipeline = Pipeline(active_engine, policies, s.max_text_chars)
        app.state.transport = transport or ProxyTransport(s)
        app.state.ocr = start_ocr()
        app.state.documents = DocumentProcessor(
            s.max_document_bytes, s.max_text_chars, app.state.ocr
        )
        yield

    app = FastAPI(
        title="Local Privacy Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(PrivateBoundary, settings=s)

    @app.exception_handler(PrivacyError)
    async def privacy_error(request, exc):
        return JSONResponse({"error": exc.code}, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default errors contain input values. Never serialize them.
        return JSONResponse({"error": "invalid_request"}, 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": "request_rejected"}, exc.status_code)

    # Catch inside ASGI stack so Uvicorn does not log exception/traceback containing payloads.
    @app.middleware("http")
    async def safe_errors(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"error": "processing_failed"}, 503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def input_text(body):
        return (
            check_text(body.text, s.max_text_chars)
            if body.text is not None
            else json_text(body.data, s.max_text_chars)
        )

    @app.get("/health")
    async def health(request: Request):
        pipeline = getattr(request.app.state, "pipeline", None)
        ocr_engine = getattr(request.app.state, "ocr", None)
        return {
            "status": "ready" if pipeline else "starting",
            "detector": pipeline.engine.mode if pipeline else s.detector,
            "ocr": f"tesseract {ocr_engine.version}" if ocr_engine else "unavailable",
            "external_enabled": s.external_enabled,
            "scope": "prototype",
        }

    @app.get("/review")
    async def review():
        response = HTMLResponse(REVIEW_PAGE.read_text(encoding="utf-8"))
        response.headers["Content-Security-Policy"] = REVIEW_CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post("/detect/text")
    def detect(body: TextRequest, request: Request):
        text = input_text(body)
        entities = request.app.state.pipeline.engine.detect(text)
        return {
            "entities": [e.model_dump() for e in entities],
            "overlap_groups": [
                [e.model_dump() for e in group] for group in overlapping_groups(entities)
            ],
            "detector": request.app.state.pipeline.engine.mode,
        }

    def sanitized_response(extracted, category, pipeline, only=None):
        sanitized, counts, context = pipeline.sanitize(
            [extracted.text], category, [extracted.unverified], only=only
        )
        try:
            return {
                "sanitized_text": sanitized[0],
                "masked_spans": counts[0],
                "category": category,
                "mask": echo(only),
                "detector": pipeline.engine.mode,
            }
        finally:
            context.clear()

    def preview_response(extracted, category, app_state, only=None):
        """Local review: original and masked text, every replacement, and a masked copy."""
        pipeline = app_state.pipeline
        recorded = []
        sanitized, counts, context = pipeline.sanitize(
            [extracted.text], category, [extracted.unverified], recorded, only
        )
        try:
            spans = recorded[0]
            document, unlocated = render_masked(extracted, spans)
            blocked = sorted({x["entity_type"] for x in spans if x["action"] == "block"})
            findings = context.findings[0] if context.findings else []
            sendable = not blocked and not findings
            ocr_engine = app_state.ocr
            return {
                "input_type": extracted.kind,
                "category": category,
                "mask": echo(only),
                "detector": pipeline.engine.mode,
                "original_text": extracted.text,
                "sanitized_text": sanitized[0],
                "spans": spans,
                "summary": {
                    "masked_spans": counts[0],
                    "by_type": dict(sorted(Counter(x["entity_type"] for x in spans).items())),
                    "blocked_types": blocked,
                },
                # The input check: values still detectable after masking. Sending refuses these.
                "validation": {"passed": not findings, "findings": findings},
                # A blocked type or a failed check would be refused by /llm/*: nothing to approve.
                "sendable": sendable,
                "preview_sha256": digest(sanitized[0]) if sendable else None,
                "ocr": None
                if not (extracted.ocr_words or extracted.kind == "image")
                else {
                    "engine": f"tesseract {ocr_engine.version}",
                    "languages": s.ocr_languages,
                    "words": extracted.ocr_words,
                    "unverified_words": len(extracted.unverified),
                    "min_confidence": s.ocr_min_confidence,
                },
                "masked_document": None
                if document is None
                else {
                    "media_type": document["media_type"],
                    "base64": base64.b64encode(document["content"]).decode("ascii"),
                    "note": document["note"],
                    "unlocated_spans": unlocated,
                },
            }
        finally:
            context.clear()

    async def read_upload(request, file):
        try:
            raw = await file.read(s.max_document_bytes + 1)
            return await run_in_threadpool(
                request.app.state.documents.extract_document, raw, file.filename or ""
            )
        finally:
            await file.close()

    @app.post("/mask/text")
    def mask_text(body: TextRequest, request: Request):
        extracted = Extracted(input_text(body), "text")
        return sanitized_response(
            extracted, body.category, request.app.state.pipeline, selection(body.mask)
        )

    @app.post("/mask/document")
    async def mask_document(
        request: Request,
        file: UploadFile = File(...),
        category: Category = Form("general"),
        mask: str | None = Form(None, max_length=512),
    ):
        only = form_types(mask)
        extracted = await read_upload(request, file)
        return await run_in_threadpool(
            sanitized_response, extracted, category, request.app.state.pipeline, only
        )

    @app.post("/preview/text")
    def preview_text(body: TextRequest, request: Request):
        kind = "text" if body.text is not None else "json"
        extracted = Extracted(input_text(body), kind)
        return preview_response(extracted, body.category, request.app.state, selection(body.mask))

    @app.post("/preview/document")
    async def preview_document(
        request: Request,
        file: UploadFile = File(...),
        category: Category = Form("general"),
        mask: str | None = Form(None, max_length=512),
    ):
        only = form_types(mask)
        extracted = await read_upload(request, file)
        return await run_in_threadpool(
            preview_response, extracted, category, request.app.state, only
        )

    async def send(
        request, roles, texts, category, max_tokens, approved, unverified=None, only=None
    ):
        pipeline = request.app.state.pipeline
        sanitized, counts, context = await run_in_threadpool(
            pipeline.sanitize, texts, category, unverified, None, only
        )
        try:
            # Send only what was reviewed: masking is recomputed here, never taken from the client.
            if approved is not None and not hmac.compare_digest(digest(sanitized[0]), approved):
                raise PrivacyError("preview_mismatch", 409)
            messages = [
                {"role": role, "content": text} for role, text in zip(roles, sanitized, strict=True)
            ]
            # This is the only outbound call and is reached only after all validators passed.
            output = await request.app.state.transport.chat(messages, max_tokens)
            check_text(output, s.max_output_chars)
            await run_in_threadpool(pipeline.validate, output, category, context, True)
            return {
                "message": {"role": "assistant", "content": output},
                "masked_spans": sum(counts),
                "category": category,
                "mask": echo(only),
                "sent_messages": messages,
            }
        finally:
            context.clear()

    @app.post("/llm/chat")
    async def chat(body: ChatRequest, request: Request):
        return await send(
            request,
            [m.role for m in body.messages],
            [m.content for m in body.messages],
            body.category,
            body.max_tokens,
            body.approved_preview_sha256,
            only=selection(body.mask),
        )

    @app.post("/llm/document")
    async def chat_document(
        request: Request,
        file: UploadFile = File(...),
        category: Category = Form("general"),
        question: str | None = Form(None, min_length=1, max_length=32000),
        max_tokens: int = Form(512, ge=1, le=2048),
        approved_preview_sha256: str | None = Form(None, pattern=r"^[0-9a-f]{64}$"),
        mask: str | None = Form(None, max_length=512),
    ):
        only = form_types(mask)
        extracted = await read_upload(request, file)
        texts, unverified = [extracted.text], [extracted.unverified]
        if question is not None:
            texts.append(question)
            unverified.append([])
        return await send(
            request,
            ["user"] * len(texts),
            texts,
            category,
            max_tokens,
            approved_preview_sha256,
            unverified,
            only,
        )

    return app
