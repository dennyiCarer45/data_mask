# Technical decisions and compatibility

Research date: 2026-10-15. Package APIs were also inspected in installed source and exercised by tests.

## Selected components

| Component | Selected version | Decision |
|---|---|---|
| Python | >=3.11,<3.14 | Signed 3.13.12 on Windows; Linux 3.13 image for native libraries |
| FastAPI | 0.143.1 | Strict API schemas and sanitized error handling |
| Pydantic | 2.14.1 | V2 schemas; extra fields forbidden |
| Guardrails AI | 0.11.0 | Custom local Validator subclasses; public validate calls; no Hub/Guard history |
| GLiNER | 0.2.29 | Local pretrained inference with custom labels and rules |
| Transformers | 5.16.1 pinned | Verified with the pinned checkpoint and offline inference |
| PyTorch | 2.14.1; Linux +cpu | Local CPU inference |
| LiteLLM Proxy | 1.105.1 | Mandatory CustomLogger async_pre_call_hook, separate private process |
| PyMuPDF | 1.28.2 | Conservative PDF extraction and feature rejection |
| python-docx | 1.2.0 | DOCX package validation; XML traversal includes tables/auxiliary text parts |
| Tesseract OCR | 5.5.0 (Debian 5.5.0-1+b1), eng/hin data 1:4.1.0-2 | Local OCR via CLI subprocess (stdin/stdout, timeout, no shell); Apache-2.0; installed in the image at build time |
| Pillow | 12.2.0 | Image decoding with pixel limit, EXIF rotation, masked review copy |

Direct safety-critical package versions are pinned in pyproject.toml. Environment freeze files record
transitive resolutions; the Dockerfile installs the tested Linux freeze against a pinned base-image
digest. These are platform-specific version pins, not artifact-hash locks or security audits.
Revalidate compatibility and advisories before updating. The optional `litellm[proxy]` dependency
installs enterprise-related modules transitively, but this implementation uses the public OSS
CustomLogger callback and requires no enterprise license key or paid guardrail integration.

Official sources:

- [LiteLLM custom callbacks](https://docs.litellm.ai/docs/proxy/call_hooks) and
  [custom guardrail timing](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail): pre-call
  hooks finish before sending; moderation/during-call can run concurrently. Only pre-call is suitable.
- [Guardrails Validator source](https://github.com/guardrails-ai/guardrails/blob/main/guardrails/validator_base.py):
  subclass `_validate`, return PassResult/FailResult, call public `validate`.
- [Guardrails package metadata](https://pypi.org/project/guardrails-ai/0.11.0/): Python <3.14.
- [GLiNER repository and API](https://github.com/urchade/GLiNER): `from_pretrained` and `predict_entities`.
- [GLiNER checkpoint](https://huggingface.co/urchade/gliner_small-v2.1): Apache-2.0 model card.
- [DeBERTa backbone](https://huggingface.co/microsoft/deberta-v3-small): MIT model card/license.
- [PyMuPDF licensing](https://pymupdf.readthedocs.io/en/latest/about.html): AGPL or commercial license.
- [Tesseract](https://github.com/tesseract-ocr/tesseract): Apache-2.0, TSV output with per-word confidence.
- [python-docx documentation](https://python-docx.readthedocs.io/en/latest/): local DOCX handling.

## Model provenance and license

Chosen checkpoint: `urchade/gliner_small-v2.1`, immutable revision
`4e091416cf7c3481db542c2a3d26156916f3a47f`, Apache-2.0 per model card. Earlier v0/v1 GLiNER
weights have noncommercial licenses; they are not selected. The DeBERTa tokenizer/config comes from
`microsoft/deberta-v3-small`, revision `a36c739020e01763fe789b4b85e2df55d6180012`.
The provisioning script embeds the local backbone config and tokenizer, retains model cards, and
writes a SHA-256 artifact manifest. Runtime does not fetch model artifacts or execute downloaded
Python code. Pinning permits reproducibility; a locally generated hash is not an independent
authenticity guarantee. Preserve upstream notices when redistributing weights/tokenizers.

This prototype is offered under AGPL-3.0-or-later to align the distributed application with the
required PyMuPDF dependency. Commercial deployment can be possible under open-source obligations;
Apache/MIT model licensing does not remove PyMuPDF's AGPL requirements. A closed-source deployment
may require Artifex's commercial PyMuPDF license and an appropriate application licensing review.

## Tradeoffs

Direct Guardrails Validator calls avoid storing original inputs or mapping metadata in Guard histories.
Validation still runs through the real package API, but there is no remote reask or external validator.
Local re-detection is an extra check, not an independent privacy oracle.

The proxy callback verifies approval instead of rerunning an ML model in LiteLLM. This keeps all
original text outside the proxy, provides a testable ordered boundary, and avoids paid integrations.
The API and network topology remain the primary privacy boundary. One worker is required for the
current in-memory replay cache. Only text-only nonstreaming chat completions are supported.

Installed LiteLLM 1.105.1 passes `acompletion` as the chat hook call type, while some callback
examples use `completion`; the adapter supports both and rejects other routes. The callback sets
`enforces_request_content=True`, so content-enforcement-only passes also run it. Only fixed error
stage codes are emitted. The live integration test verifies behavior without an enterprise license.
With message logging disabled, LiteLLM intentionally leaves its request-body snapshot empty.
The callback therefore rebuilds a clean outgoing envelope from signed fields instead of relying on
that snapshot to reject every possible future provider option. Only internal logging handles survive.

GLiNER sometimes classifies schema labels such as `Patient:` or `Phone:` as sensitive entities,
especially when adjacent values are blanked during validation. Only exact known field labels followed
by `:` or `=` are exempted from model detections; field values remain scanned. A regression test
ensures a value literally named `Patient` is still protected. Character windows also check the actual
split-word tokenizer budget, leave room for entity-label prompts, and overlap to reduce boundary misses.

Model and documents are not silently downgraded when native libraries fail. This Windows host's
application control blocked an unsigned standalone Python build and native PyTorch/PyMuPDF DLLs.
A signed python.org runtime supported core tests; Linux Docker provides the full runtime without
changing host security settings. These environment failures must not be mistaken for passing tests.


Cross-message reference propagation keeps the strongest confidence any detector reported for each
value/type pair. It previously assigned 1.0 to every reference, letting a weak GLiNER guess (for
example ADDRESS at 0.63 on a phone number) tie a context rule and win the non-PHONE tie-break,
producing `[ADDRESS_001]` for a phone. Span coverage and evaluation metrics were unaffected.

Compose places all services on an `internal: true` network, so Docker does not publish the
gateway's host port. Exposing it requires a non-internal network for the gateway, which also
grants that container egress; this tradeoff is left as an explicit deployment decision.
The opt-in `compose.ui.yaml` resolves it for browser review without that tradeoff: a stdlib
`ui-relay` joins the internal and an ingress network and forwards loopback port 8010 to
`gateway:8000`. Disabling bridge IP masquerade was tested first and rejected, because Docker
Desktop still gave the container internet egress. Verified on Docker Desktop (Compose v5.1.3): the
page loads through the relay and the gateway still cannot open a connection to `1.1.1.1:443`.
`compose.lan.yaml` adds an opt-in `lan-relay` for other devices on the same network: the same
relay with stdlib TLS termination (TLS 1.2 minimum, self-signed ECDSA P-256 certificate from
`scripts/make_lan_cert.py`, key kept in gitignored `.runtime/lan-tls`), bound to one host address
instead of `0.0.0.0`. Plain HTTP on the LAN was rejected because the API key, uploads, and preview
responses (which contain original values) would cross the network unencrypted. Verified from the
host against its Wi-Fi address: TLS 1.3 negotiated, TLS 1.1 and plaintext refused, wrong key `401`,
text and PDF previews masked; not yet verified from a second physical device.

Re-detection during validation runs on the sanitized text with this request's placeholders left in
place, then discounts only placeholder characters inside each detected span. Blanking placeholders
to spaces made GLiNER misread labels (`Email:`), headings, and pronouns as entities and refused
11 of 20 correctly masked synthetic documents. Model-only spans made entirely of a fixed list of
generic words (pronouns, roles, departments, document headings) are ignored in both detection and
validation; rule, context, and dictionary hits never are. Tradeoff: a real value spelled only with
those words loses its model detection and depends on the rules. See docs/evaluation.md.

Prefixed document numbers (`INV`, `PO`, `ORD`, `SO`, `QUO`, `CN` followed by digits, `-`, `/`)
are recognized only to discard misreads inside them: every GLiNER hit wholly within one, and
PHONE/BANK_ACCOUNT pattern hits. GLiNER labeled the same number differently on the original and
the masked text, so masked invoices were refused. Context labels, custom terms, other patterns
(an SSN shape inside a number), and model spans extending past the number still apply.
Tradeoff: a phone or account number written with one of these prefixes is not masked.
Unprefixed values after `Invoice number:` are still treated as possible phones.

The PERSON label `name` no longer fires after `project`, `company`, or `organization`; those
values are captured as PROJECT or ORGANIZATION, so they remain masked with the right type.
`Subtotal`, `Total due`, `Balance due`, and `Amount due` are AMOUNT labels. Common service
line-item and greeting words (`consulting`, `training`, `hosting`, `hi`, `new`, ...) joined the
generic model-hit list, with the same tradeoff as the existing list.

The first real-provider run (Gemini, synthetic hospital invoice) showed the output check refusing
a reply that contained only placeholders. Two narrow output-side relaxations followed. A context
hit whose value is exactly one of this request's placeholders, closed by `)`, `.`, `,`, `;`, or
end of line, counts as masked (context capture otherwise runs to the line end, absorbing provider
prose); `Name: [PERSON_001] Okafor` still fails. A GLiNER-only hit whose exact text already
appeared in this request's validated sanitized input (for example `CBC`) is not new disclosure
and is ignored; rule hits are never exempted, and the approved texts are request-local and
cleared with the mapping. Payment and department words (`owed`, `paid`, `general`, `medicine`)
joined the generic list after GLiNER flagged `Department General Medicine` and `Amount Still Owed`.

DATE_OF_BIRTH is a rule-only type: dates after `date of birth`, `DOB`, `birth date`, or `born (on)`,
with or without `:` (extracted PDF tables drop delimiters). Adding a GLiNER label would change every
model prediction, so `TYPES` adds it outside `LABELS`. Other dates (visit, invoice, due) are not
detected. An ADDRESS hit extends over up to four comma-separated locality parts ending in an
Indian, US, or UK postcode. Bare numbers shaped like money (three or more integer digits or comma
grouping, exactly two decimals: `800.00`, `1,200.00`) are AMOUNT; this can mask lab values or
version numbers written that way, while `12.50` is missed. Generic-word additions from provider
replies (payment methods, currency codes, medical service words such as `diagnostic`, `nursing`,
`consultation`) carry the existing tradeoff: a model-only value spelled entirely with them is
dropped, rules still apply.

PDFs from many current tools (Canva, Adobe, AI document generators) embed a C2PA provenance
manifest named "Content Credentials", which made extraction reject them. The extractor now
tolerates embedded files only when every one is an indirect file specification with
`/AFRelationship /C2PA_Manifest`, a single `/EF /F` stream of subtype `application/c2pa`, and the
count of such specifications equals the document's embedded-file count. The manifest is never
extracted or forwarded; any other attachment, a renamed file lacking the markers, inline file
specifications, or extra attachments are still rejected. Tradeoff: manifest contents (which can
name the creator or tool) are not inspected, because they never leave the extractor.

A synthetic agreement showed GLiNER missing planted commercial terms (`Negotiated discount: 17%`,
`Confidential budget ceiling: INR 5,20,000`) while flagging the confidentiality clause's category
nouns (`pricing`, `contract terms`, `credentials`) as COMMERCIAL_TERM/INTERNAL_INFORMATION; the
document was blocked for the wrong reason. Rules now detect COMMERCIAL_TERM: any `Confidential
<field>:` label except `Confidential party`, `Negotiated <field>:`, `Rebate`, `Budget (ceiling)`,
`Price cap`, `Rate card` labels (allowing a line break inside the label, as PDF wrapping
produces), and percentage concessions in prose (`22 percent volume rebate`, `discount of 17%`).
The category nouns joined the generic model-hit list. Tradeoff: COMMERCIAL_TERM blocks by default
in every category, so an invoice line such as `Discount 10%` now blocks; `Discount ₹350.00` does not.

DOCX tables (and some PDFs) extract each cell on its own line, so `Date of birth` / `09 March
1984` and `Insurance member ID` / `PLAN-DEMO-552018` evaded `Label: value` rules; the member ID
reached the masked text unprotected in a synthetic guideline. Context labels now also match a
table layout: a line holding a known label with at most two leading words, value on the next line.
The date-of-birth rule accepts one line break. `Insurance (member) ID/number` and `Member ID` are
MRN labels. Only the label itself on such lines is exempt from model hits, so a name preceding a
label word is still detected. Placeholder-shaped document text in angle brackets (`<PERSON_001>`)
is exempt from model hits. Tradeoff: a short heading ending in a label word (`Contact`) masks the
following line.
Provider summaries restate category nouns in plural or abbreviated form (`patient names`, `MRNs`,
`member IDs`, `diagnoses`), so generic-word matching accepts simple plurals of listed words and
lists `mrn`, `dob`, and `id`. It still applies only to digit-free GLiNER spans.
The output check also ignores a digit-free GLiNER span when every visible word (or its singular or
plural) occurs in this request's validated sanitized input: provider rewording such as `record
numbers` or `internal paths` adds nothing the provider was not sent. Words absent from the sent
text (a name the provider could not have received) still fail, and the original-value check is
unchanged. Over-masked describing phrases are a second source of output refusals: a masked
`medical record number` becomes a protected original, so its reuse fails; `record`, `number`,
`telephone`, and `personnel` joined the generic list. Synthetic guideline: 10/10 replies passed.

## Review flow and OCR (2026-10-15)

- **Tesseract over RapidOCR/EasyOCR:** no extra model to download per request, deterministic CLI, and
  per-word confidences used for fail-safe masking. Hindi data is installed because Indian ID cards
  are bilingual; non-Latin words are masked rather than trusted to English detectors.
- **Unverified words are masked, not refused:** refusing every low-confidence word would reject most
  phone photos. They are masked in place and are not propagated as values elsewhere in the text.
- **Reviewed-hash sending instead of server-side preview state:** the gateway keeps no state between
  preview and send; it re-masks and compares SHA-256, refusing on any difference.
- **ID-number rules:** Aadhaar/VID (including `XXXX XXXX 1234`), EPIC, passport, driving licence,
  relation prefixes (`S/O`, `D/O`, `W/O`, `C/O`), `Father's name`, `Surname`, `Year of birth`.
  The Aadhaar shape also matches any unspaced 12-digit number (an extra detection, never an unmasking).
- Not added: face, QR, and signature detection for the review copy (would need OpenCV or a model).

## Conversation summaries (2026-10-16)

- **Role words stay generic, keys stay scanned:** JSON keys are user-controlled and remain in the
  detected text; `speaker`/`agent` were fixed through the generic-word list rather than by
  exempting keys. The list only filters model-only spans; rules are never filtered.
- **Rule type beats model label:** in an overlap group the action is unchanged (strictest wins);
  only the label/placeholder type now prefers an exact rule match over a higher GLiNER score.
- **Not added: alias linking** (`Asha` → same placeholder as `Asha Rao`). Shared surnames in one
  family (`Mrs. Rao`) make a wrong merge likely, and a wrong merge misleads the AI more than
  separate placeholders do.

## Mask-again step (2026-10-16)

- **Chosen by the project owner** over adding each second-pass hit to `custom_terms` or keeping the
  refusal. Most refusals were names GLiNER finds only after nearby values are masked.
- **Trade-off:** validation now uses the same detector that just did the extra masking, so it mostly
  checks that masking has settled; it is less of an independent check than before (it was never
  fully independent). Literal-residual checks, placeholder checks, and output validation are as
  before. Only one extra pass runs; anything validation still finds is refused.
- **Partial leaks stay refused:** a second-look hit that overlaps a placeholder is never re-masked.
- **Company short forms:** the first word of an organization ending in a legal suffix (LLC, LLP,
  Inc, Ltd, Group, …) is masked wherever it appears as a whole word. Names without a suffix are
  not shortened, because their first words are often common ("Platform Technology …").
