# Threat model and limits

This prototype protects against accidental disclosure of recognized confidential entities in text
sent to an LLM, and tests several malicious request bypasses. It is not an anonymization guarantee,
regulatory certification, sandbox for hostile documents, or protection against a compromised host.

## Trust and deployment boundary

The host, administrator-managed policy/model/config files, local parser libraries, gateway process,
and private proxy are trusted. One API key identifies a single trust domain. Protect transport with
TLS/authenticated ingress before allowing remote clients. There is no tenant authorization system,
per-user rate limit, or persistent audit service. Do not expose LiteLLM or the mock to clients.
The default Compose network is internal and only publishes gateway port 8000 on loopback.

Original text can exist in parser buffers and process memory; uploaded multipart files may spill
to temporary storage. Compose uses bounded tmpfs for the gateway; native runs rely on host temp
storage protection. No mapping is logged or sent to LiteLLM. The `/preview/*` endpoints return
original values beside their placeholders to the authenticated caller who supplied them; treat these
responses as confidential. `/review` is a static page with no data and no authentication of its own. Clearing Python
containers does not securely erase all copies, swap, crash dumps, or provider-independent host traces.

## Fail-closed controls

Authentication before parsing, request/file/text limits, duplicate-key and nonfinite-JSON rejection,
UTF-8 validation, restrictive document feature support, model-required default startup, validated
policy actions (a caller's `mask` selection can narrow protection to the listed types but never
unblock a blocked type or allow unverified OCR text), complete input validation before transport, signed proxy approval, restrictive
request fields, no streaming, and buffered output validation. All application errors are static codes.
Only local provisioning downloads public weights; inference is offline. No remote detection APIs.

Raw-body logging, verbose LiteLLM logging, tracing exporters, telemetry, response caches, spend/error
logs, and Guard history are disabled or absent. Do not add middleware that records bodies, query
strings, headers, exception locals, or tracing payloads. Native Uvicorn is launched without access
logs. Model/library warnings must also be reviewed; tests inspect captured synthetic logs.

LiteLLM sees sanitized messages only through normal gateway use. Its own routes and error logging
are not a safe ingress for raw documents. Network isolation is necessary even with the HMAC callback:
not every auxiliary LiteLLM HTTP route is part of this tested chat path. The approval key grants the
ability to attest payloads; compromise of it or the gateway defeats this boundary. Replays are blocked
only within one proxy process lifetime. Do not scale workers without shared replay protection.

## Detection and extraction limits

- GLiNER is probabilistic. Rules miss context, languages, aliases, encodings, indirect identifiers,
  and semantic secrets. Reusing the same detector for validation creates correlated blind spots.
  The mask-again step (one extra detection on masked text before validation) strengthens that
  correlation: validation mainly confirms masking has settled. Partial leaks beside placeholders
  are still refused. Track misses in `docs/gliner-failures.md`.
- No NER model can establish that an entire diagnosis, contract, business strategy, or document is
  safe. Defaults protect recognized labels/phrases. Unknown secret descriptions can still pass.
- Generalization can remove useful clinical details; pseudonymization leaves surrounding context
  and may still permit reidentification. Review utility and risk per document category.
- Overlap union intentionally favors privacy and may erase adjacent useful content. Literal reuse
  matches whole words only (so "IRS" no longer masks part of "first"), which means a detected
  value glued to other letters ("MiraVale") is not re-masked by reuse and relies on detection and
  validation. Different spellings/aliases are not resolved to one identity, except that the first
  word of a company name ending in a legal suffix (LLC, Ltd, …) is masked wherever it appears.
- PDF completeness checks are conservative heuristics. Images are read by OCR (or rejected when OCR
  is unavailable); forms, attachments, and blank pages are rejected (except a C2PA "Content Credentials" manifest, which is ignored and never sent), but vector-drawn glyphs, obfuscated fonts, and hidden layers cannot
  be comprehensively verified. DOCX feature rejection is similarly bounded, not a formal proof;
  DOCX extraction fails if any paragraph of a read part was not written into the text.
- Reading order and structure (columns, headings, tables, page labels) are reconstructed by
  heuristics in `layout.py`. A wrong order does not drop text, but it can put a value beside the
  wrong label, which changes what label rules and the model see. Markup added by layout extraction
  is not document text and has no page position.
- OCR (Tesseract 5.5.0, English + Hindi) reads images and PDF image regions. OCR can misread a value
  with high confidence; a misread name or number then depends on detection like any other text.
  Words below confidence 60 and every non-Latin word are masked as `UNVERIFIED_TEXT` without being
  checked. Vector-drawn text outside images is still not cross-checked against a page render.
- The masked review copy covers masked words only. Photos, QR codes (an Aadhaar QR encodes the card
  details), signatures, and other graphics remain; it is a review aid, not a redacted document.
  PDF copies are covered at each masked character's own position and then read back; a masked
  value still readable in the copy is reported as uncovered (`unlocated_spans`), but the copy is
  still returned to the local caller.
- Unlabeled names on ID cards need GLiNER; rules mode leaks them (see evaluation).
- Handwriting, legacy DOC, arbitrary binary files, multilingual detection, archive sandboxing,
  and semantic verification of contractual terms are not implemented.
- Reserved placeholder syntax is unavailable in original inputs. No reidentification endpoint exists.
- Resource limits bound bytes/characters, not total CPU time; hostile parser inputs can still exhaust
  resources. Production needs isolated parser/model workers, admission controls, timeouts, and quotas.

Use synthetic records only until deployment-specific privacy review and a much larger labeled
evaluation justify the intended use. Passing tests does not establish zero leakage.

