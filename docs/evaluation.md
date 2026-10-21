# Evaluation

Only synthetic fixtures are used. Results and exact execution environments are recorded below as
verification completes. No passing test count or small benchmark establishes zero leakage.

## Results measured on 2026-10-15

The complete Linux run passed **89 tests**, with GLiNER and actual LiteLLM integration enabled,
using `docker run --network none`. This includes both rules and GLiNER gateway-to-proxy-to-mock
flows, genuine PDF/DOCX extraction, and the real Guardrails API. Seven third-party deprecation
warnings were reported. An earlier Windows run passed 39 core tests; native PyTorch/PyMuPDF
were blocked by Windows application control, so full verification moved to Linux Docker.

| Metric, 8 synthetic records / 11 sensitive values | Rules only | GLiNER + rules |
|---|---:|---:|
| Exact span/type precision | 90.0% | 68.75% |
| Exact span/type recall | 81.82% | 100% |
| Missed labeled identifiers | 2 | 0 |
| Extra detections (false positives) | 1 | 5 |
| Literal sensitive values in released sanitized output | 2 | 0 |
| Literal leakage rate | 18.18% | 0% observed |
| Sensitive-value removal, including blocks | 81.82% | 100% |
| Approved / blocked records | 8 / 0 | 7 / 1 |
| Selected context phrase preservation | 100% | 75% |
| Mean pipeline latency (excludes model load) | 0.57 ms | 369.66 ms |

A later rerun after the validation change below produced identical accuracy figures (latency varies).

### Validation false-refusal check (2026-10-15)

A separate 20-document synthetic set (5 invoices, 4 agreements, 4 guidelines, 3 healthcare,
4 general) measured how often correctly masked text was refused by the re-detection check, plus
10 synthetic leak probes (unmasked names, an address, email, phone, organization, and a partial
`[PERSON_001] Surname` leak) that the output check must refuse. GLiNER mode, threshold 0.35.

| Validation design | Clean documents passed | Leak probes refused |
|---|---:|---:|
| Before: placeholders blanked, every model hit fails | 11/20 | 10/10 |
| After: placeholders kept, generic-word model hits ignored | 20/20 | 10/10 |

Before, GLiNER flagged pronouns, roles, headings, and field labels such as `her`, `customer`,
`Remote Work Guideline`, and `Email`. The set is small, hand-written, and English-only; it shows
this failure mode was addressed, not a production refusal rate. The ignored word list can drop a
model hit for a real value spelled like a generic word (for example a surname `Sales`); rule,
context, and dictionary detections still apply to such values.

### Invoice detection tuning (2026-10-15)

Before this change, 3 of 4 synthetic invoice probes were refused: GLiNER labeled prefixed
document numbers (`INV-88213`, `PO-44718`) as bank accounts, projects, or organizations
inconsistently between masking and validation, the phone pattern matched `2026-00417`, and
`Total due` was flagged as an amount. `Project name:` values were labeled PERSON, and invoice line
items such as `Consulting services - USD 2,000.00` were masked as organizations.

After the change, a rebuilt synthetic set (28 clean documents: 13 invoices, 5 agreements,
4 guidelines, 3 healthcare, 7 general, including those probes) passed 27/28, and 12/12 leak
probes were refused, including a name beside an invoice number, an organization beside
`Invoice INV-…`, and an SSN-shaped value inside `INV-123-45-6789`. The one refusal is a guideline
where GLiNER scores `MFA` as confidential organizational information at the 0.35 threshold; the
guidelines policy blocks that type, and this change does not relax it. The rebuilt set
replaces the earlier 20-document set, which was not retained, so the two are not directly
comparable. Both evaluation snapshots were regenerated with unchanged accuracy figures.

These are smoke-benchmark measurements, not representative production accuracy. The GLiNER
configuration favors recall and overmasks or blocks some useful content. The apparent zero literal
leakage on eleven values is insufficient evidence of zero real-world leakage. The rule-only mode
demonstrably misses unlabeled names/addresses and is unsuitable for external-provider use.

Machine-readable snapshots: [rules](evaluation-rules.json), [GLiNER](evaluation-gliner.json).
Pipeline latency varies by CPU and contention; the native rules timing and Linux model timing are
not a controlled cross-platform speed comparison. Full frozen dependency lists accompany the project.

`scripts/evaluate.py` evaluates eight intentionally small English examples, including two unlabeled
cases that expose rule-only recall limitations. Precision/recall require exact start/end/type matches;
duplicate detector hits for the same labeled span are deduplicated. A span recognized under an extra
type counts as a false positive, even if its text is correctly removed. Missed identifiers are false
negatives under that metric. Leakage checks literal protected values remaining in approved output.

The removal metric includes blocked records as removed; approved/blocked counts are reported so
rejection cannot be mistaken for useful masking. Context preservation checks manually selected
nonsensitive phrases, not medical correctness. Clinical meaning of intentionally generalized diagnoses
is reduced and must be assessed separately. Timing measures detection/masking/input revalidation,
excludes model startup and provider latency, and is hardware-sensitive.

Automated tests cover overlap unions, block precedence, consistent pseudonyms across history,
request isolation, malformed/extra fields, auth, residual and output leakage, missing validators,
detector errors, safe errors/logs, local-only Guardrails settings, document formats and partial
extraction, HMAC expiry/tampering/replay, and the installed proxy callback. The optional live test
launches the actual LiteLLM Proxy and captures requests at a local mock provider. A model test runs
the pinned local GLiNER checkpoint; these tests are skipped unless explicitly enabled.

Expand to hundreds/thousands of independently annotated cases by category/language, adversarial
encodings and prompt histories, confidence intervals, manual clinical/document utility review,
concurrent requests and load, provider adapters, document parser fuzzing, and deployment-level
network/log/trace audits before enabling any external provider with confidential records.


### First real-provider run: Gemini (2026-10-15)

`compose.gemini.yaml`, model `gemini/gemini-3.5-flash-lite` (Google AI Studio free tier), one
synthetic hospital invoice (patient name, MRN, date of birth, address with postcode, email, phone,
insurance ID, diagnosis, itemized INR amounts), category `invoices`, question prepended.
Before the changes below, the request was refused at input (`Department General Medicine`) and
then at output, although every reply contained only placeholders. After them, 14 of 15 provider
replies passed the output check (one refusal, cause not captured because replies are not logged).
Provider output varies between runs; this is one document, not a refusal-rate estimate. The same
run showed the date of birth, city/postcode, and currency-less line amounts reaching the provider
unmasked; DATE_OF_BIRTH, address-tail, and bare-amount rules were added, and the
20+8-document synthetic set and 12 leak probes were unchanged (27/28, 12/12).

### Masking per input type, including ID cards (2026-10-15)

`scripts/evaluate_documents.py` renders the same 13 synthetic English records (the 8 benchmark records
plus 5 Indian ID-card layouts: Aadhaar, PAN, voter ID, driving licence, passport) as raw text, a
text-layer PDF, a scanned PDF (image only), a clean PNG, and a phone-style JPEG (1.5° tilt, blur,
quality 55). 28 sensitive values per rendering. Run in the Linux Docker image with `--network none`;
Tesseract 5.5.0, eng+hin, confidence threshold 60. A near leak is a surviving string at least 80%
similar to the value (whitespace ignored), which catches OCR misreadings that literal checks miss.
Raw output: `evaluation-documents-gliner.json`, `evaluation-documents-rules.json`.

| GLiNER + rules | Text | PDF | Scanned PDF | Image | Photo |
|---|---:|---:|---:|---:|---:|
| Literal / near leaks | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Refused records | 0 | 0 | 0 | 0 | 0 |
| Values OCR/extraction read exactly | 28 | 28 | 27 | 27 | 27 |
| Words masked as `UNVERIFIED_TEXT` | 0 | 0 | 2 | 2 | 2 |
| Context phrase preservation | 92.3% | 92.3% | 92.3% | 92.3% | 92.3% |
| Mean latency, ms | 404 | 367 | 540 | 536 | 557 |

Rules mode leaked 3 of 28 values in every rendering: two unlabeled names (including the Aadhaar
cardholder) and one unlabeled address. ID cards therefore require GLiNER mode. Figures above are
after the 2026-10-16 fixes below; before them one record per rendering was refused and preservation
was 84.6%. Observed over-masking on the cards: "Government
of India" as an organization, "Female" as a person, and the word "Aadhaar" as an identifier.

After the ID-number rules were added, `evaluate.py --mode gliner` kept 100% recall and 0 leaks; exact
span precision fell from 68.75% to 64.7% because the unspaced account number `123456789012` now also
matches the Aadhaar shape (same masked output). Full suite: **115 passed** (89 previous + 26 new) with
`RUN_MODEL_TESTS=1 RUN_PROXY_TESTS=1`, ruff check and format clean. The review page was driven in
headless Chrome against a running gateway: phone-photo card uploaded, previewed, and sent through the real proxy to
the mock with the reviewed hash.

Limits: one generated font, no real scans, handwriting, worn cards, glare, or Devanagari rendering
(the Hindi path is unit-tested with fixed OCR words only). 13 records is far too few for a rate.

### Real run on sample inputs, and fixes (2026-10-16)

`scripts/demo_run.py` sends six inputs through the running Compose stack (GLiNER, Tesseract, real
proxy, mock AI) and saves every input and output under `samples/demo-run/`: the two repository sample
PDFs, a scanned (image-only) copy of the invoice, a synthetic Aadhaar-style phone photo, raw text,
and raw JSON. The first run refused the agreement and the card with `input_validation_failed`:

- Agreement: GLiNER started an address at `Sector 62`, leaving `28 Orchid Business Park` unmasked;
  the input check caught it on the second pass. Fix: extend a detected address back over a
  house-number segment immediately before it (`ADDRESS_HEAD`).
- Card and agreement: second-pass model hits on `Female`, `Delivery Manager`, `Authorized
  Representative … For the Provider`, `external AI services`. Fix: gender, signatory-role, and
  connective words added to the generic-word list (model-only spans made solely of them are ignored).
- The preview now returns these second-pass findings (`validation.findings`, masked-text offsets)
  instead of an opaque error, and the review page lists them. Sending still refuses.

After the fixes: invoice, scanned invoice, card, text, and JSON are all sendable; the scanned invoice
extracted and masked text identical to the text-layer invoice (26 values). The agreement is shown
but not sendable: `COMMERCIAL_TERM` is blocked by policy and one false finding remains (`synthetic
data` as internal information, confidence 0.35). Gender (`Female`) is now kept in card text.
`evaluate.py --mode gliner` is unchanged (precision 64.7%, recall 100%, 0 leaks). 118 tests passed.

### Long, unlabelled conversation summaries (2026-10-16)

`scripts/conversation_demo.py` sends three synthetic summaries with almost no field labels through
the running Compose stack (GLiNER, real proxy, mock AI) and saves results under
`samples/conversation-demo/`: a patient follow-up call (TXT, healthcare), a support ticket with
transcript turns (JSON, general), and a partner meeting (PDF, general). First run: every listed
person, organization, address, phone, email, account, amount, member number, and diagnosis was
masked, including later short forms (`Asha`, `Mrs. Rao`, `Bluepeak`, bare `Hillcrest Lane`), but
the TXT and PDF were not sendable and several spans were wrong:

- Second-pass findings on role words (`hospital`, `insurer`, `ward doctors`, `care coordination
  team`, `our account team`), and JSON key/value `speaker`/`agent` masked as people. Fix: these
  words joined the generic-word list.
- `briefly` became `br[ORGANIZATION_003]`: a GLiNER window started mid-word and `iefly` was tagged.
  Fix: windows end and start at whitespace (`word_end`, `word_start`); unbroken text keeps the cut.
- Bank account ran on into `and asked us to` (case-insensitive tail). Fix: IBAN pattern is
  case-sensitive. Amounts no longer swallow a sentence-final `.` or `,`.
- An email tagged PERSON by GLiNER (0.977) beat the EMAIL rule (0.95). Fix: within an overlap
  group of equal policy action, a rule-sourced type wins (except the broad PHONE pattern).
- `Project\nCedar` (wrapped in the PDF) missed the custom term. Fix: custom terms match any
  whitespace between words. Ticket numbers (`CS-20431`, also `SR`, `INC`, `TKT`, `CASE`, `REQ`)
  joined the document-number exemption.

After the fixes all three are sendable with zero second-pass findings (25, 13, and 14 masked
values). Full suite: **125 passed** with `RUN_MODEL_TESTS=1 RUN_PROXY_TESTS=1`; ruff check and
format clean. `evaluate.py` and `evaluate_documents.py` results are unchanged by these fixes; the
committed `evaluation-rules.json`/`evaluation-gliner.json` were stale and are now refreshed (rules
precision 81.8% and GLiNER 64.7%, the extra detection being the Aadhaar-shaped match noted above).

Remaining limits: aliases are not linked, so `Asha Rao`, `Asha`, and `Mrs. Rao` get three different
placeholders (nothing leaks, but the AI reads three people). Descriptions without an entity ("the
client's CFO") and confidential meaning without names are not detected. Three documents are far
too few for a rate.

### Legal invoice and the mask-again step (2026-10-16)

`invoice1_modern.pdf` (synthetic two-column legal invoice, added to `samples/demo-run/input`; run
alone with `python scripts/demo_run.py invoice1_modern.pdf --category invoices`) was first refused
with five second-pass findings and heavily over-masked (69 PERSON values, mostly job titles, and the
invoice number as a phone). Each failure and its fix is listed in `docs/gliner-failures.md`.

New: after masking, `Pipeline.sanitize` runs detection once more on the masked text. Protected
values found only there that do not touch a placeholder are located in the original text, added
as `second_look` entities, and everything is re-masked from scratch before the usual validation
pass. A hit adjacent to a placeholder (`[PERSON_001] Okafor`) is still refused.

Results after all fixes: the invoice is sendable (115 masked values, 0 second-pass findings, mock AI
call 200); all ten demo and conversation inputs are unchanged except the agreement, which stays
blocked by policy. Known miss: `Marriott Courtyard SF` is sent unmasked. `evaluate.py --mode gliner`:
recall 100%, 0 leaks, precision 64.7%, approved/blocked 8/0 (was 7/1), context preservation 87.5%
(was 75%), mean latency about 600 ms (was about 370 ms, the cost of the second detection).
`evaluate.py --mode rules` and both `evaluate_documents.py` runs are unchanged. Full suite:
**131 passed** with `RUN_MODEL_TESTS=1 RUN_PROXY_TESTS=1`; ruff check and format clean.

Review copy (masked PDF) labels, same day: many boxes in the invoice's Rate and Amount columns were
plain black. The label went only on a value's first box (the narrow Rate cell when rate and amount
were masked together), and PyMuPDF 1.28.2 silently skips redaction text that does not fit or is
below about 4 pt. Now every box is labelled once, the label is shrunk to fit, narrow cells show only
the placeholder number (`[AMOUNT_003]` -> `003`), and boxes inside a larger masked box are left to
that box's label. Known quirk: a value repeated on a page is found by every span that contains it,
so a cell can show another span's number (the 03/28 Rate cell shows `016`, the text says
`[AMOUNT_024]` for that row). Only the review copy is affected; 132 tests passed.

Manual review of the invoice's 89 distinct replacements, same day. Fixed: `cite-checking
authorities` as an organization; the matter name labelled organization (the `Matter:` label now
gives PROJECT); `first-year` masked as `f[ORGANIZATION_015]t-year` (reused values, and the literal
residual check, now match whole words); rate and amount cells merged into one placeholder and
`Standard` swallowed from the Market column (model spans adding only spacing or generic words to
same-type rule matches are dropped). Splitting the amounts changed the second look, which then
blocked the invoice on `firm overhead` as a commercial term; billing words joined the generic list.
Result: sendable, 127 masked values, 0 findings. All other samples and all four evaluation runs are
unchanged. `demo_run.py` now clears a file's output folder first (a stale findings file had
survived a clean run). 135 tests passed.

### ID card phone photos (2026-10-16)

`scripts/id_card_demo.py` renders four fictional Indian-style cards (Aadhaar-, PAN-, voter ID-, and
driving licence-style; SPECIMEN watermark, silhouette, no emblems) as phone photos (desk background,
2.5° tilt, blur, JPEG quality 62) and runs them through the Compose stack (`general` category).
Tesseract read all fields; two characters were misread (`BQKPM4821K` as `BOKPM4821K`, `KA05` as
`KAO5`) and both values were still masked. First run: all sendable, but the second address line
(`Kolkata - 700091`, `Karnataka - 575001`) was sent unmasked on two cards, blood group was kept, and
card titles were over-masked. After the fixes (Indian City - PIN rule, `Blood group` as DIAGNOSIS,
card words generic) every name, number, date of birth, address line, and blood group is masked on
all four cards, all are sendable, and the earlier samples are unchanged except the original
Aadhaar photo no longer masks the word `Aadhaar`. Kept on purpose: gender, validity date, vehicle
class. Still over-masked: `Government of India` and similar issuers (policy question above), and
an OCR fragment `Perman`. 138 tests passed. One rendering per card; not a rate.

### Layout-aware extraction (2026-10-19)

PDF and DOCX text now carries reading order and structure (see `docs/decisions.md`). Measured in
the Docker image (`local-privacy-gateway:dev`: Python 3.13, PyMuPDF 1.28.2, python-docx 1.2.0,
GLiNER 0.2.29, Tesseract 5.5.0) with `--network none`, comparing the previous commit (`43f6516`)
and the new code on the same inputs through extraction, masking, input validation, and the review
copy: the 12 repository samples (PDFs, scanned PDF, DOCX, ID-card photos) plus a synthetic
two-page two-column paper with a bordered table and a synthetic structured DOCX.

| Check (GLiNER + rules) | Before | After |
|---|---:|---:|
| Inputs whose sendable/refused outcome changed | — | 0 of 14 |
| Values masked before that now reach the text unmasked | — | 0 (one diff-script hit was the document's own `<PERSON_001>` example) |
| Review-copy spans not covered | 0 | 0 |

What changed in the text: the two-column paper is read column by column instead of spliced line by
line; bordered tables (paper results, legal invoice fees) are `| cell |` rows; the legal invoice
header blocks are read one after the other; DOCX tables are rows, headings and list numbers are
kept, and line breaks no longer glue words (`Follow-up withDr.`). On the agreement, two earlier
false alarms disappeared (`Initial milestone` as AMOUNT, `software testing` as internal
information) and the payment reference `DEMO-ACCT-7421` is now masked (as SECRET, so it adds a
block; the agreement was already refused by policy). New over-masking: `High-Cost` and one job
title in the legal invoice, `Baseline` in the paper's table. One regression was found and fixed
before release: the guideline DOCX was blocked on `unpublished operating metrics` (0.359), now a
generic-word filter entry; and a date of birth in a DOCX table row was missed until the
date-of-birth rule accepted `|`.

`evaluate.py` (both modes) and `evaluate_documents.py` (both modes) returned the same accuracy
figures as the committed snapshots; only latency differed, and that run shared the CPU with other
containers, so the snapshots were not replaced. Full suite: **153 passed** with
`RUN_MODEL_TESTS=1 RUN_PROXY_TESTS=1`, 0 skipped; ruff check and format clean.

Limits: one synthetic paper, rendered by PyMuPDF with one font and no bold headings; no real
journal PDFs, three-column pages, borderless tables, or text wrapped around figures. Long papers
still exceed the 32,000-character limit.
