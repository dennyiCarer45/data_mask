# Masking verification audit — 2026-10-19

Audit of commit `43f6516` against the "LLM Data Privacy Gateway — Masking Verification Checklist".
No code was changed. Synthetic data only.

## How this was verified

- Read every module in `privacy_gateway/`, all tests, `config/policies.yaml`, `scripts/evaluate.py`,
  and `docs/` (security, evaluation, decisions, gliner-failures).
- Ran the full suite in the project image `local-privacy-gateway:dev` with the current source
  mounted read-only, `docker run --network none`, `RUN_MODEL_TESTS=1 RUN_PROXY_TESTS=1`:
  **141 passed, 0 skipped**, 7 third-party deprecation warnings, 76.9 s.
  Versions: Python 3.13, fastapi 0.143.1, gliner 0.2.29, guardrails-ai 0.11.0, litellm 1.105.1,
  pymupdf 1.28.2, python-docx 1.2.0, torch 2.14.1+cpu, Tesseract 5.5.0.
- Re-ran `scripts/evaluate.py` both modes. Rules: precision 81.8%, recall 81.8%, 2/11 values leaked.
  GLiNER: precision 64.7%, recall 100%, 0/11 leaked, mean latency 867 ms (CPU-contended run).
  These match `docs/evaluation.md` (latency differs, as documented).
- Ran **audit probes** (a throwaway script, not committed) through `Pipeline.sanitize`, which
  includes the real Guardrails input validation, in both rules and GLiNER mode, plus PDF/DOCX
  extraction and the review-copy renderer. Probe findings are labelled **[probe]** below.
  A probe passing `sanitize` means the text would have been sent to the provider.

## Two separate things this codebase does

1. **Text masking before LLM inference** (the product). Every input (TXT, JSON, PDF, DOCX,
   image) is reduced to *plain text*, masked, validated, and only that text is sent.
   Structure survives only as far as plain text preserves it.
2. **A masked "review copy"** of PDFs and images (`redaction.py`), returned to the local caller by
   `/preview/document`. It is explicitly a review aid (`redaction.py:8-11`), not a redacted
   deliverable: photos, QR codes, signatures stay, and it is never re-verified. **DOCX, TXT and
   JSON get no masked document output at all.**

So most layout/format requirements are "partial" for path 1 and "missing" for a redacted
document deliverable. Tables in sections 1 and 6 show both.

Legend — **Status:** Impl / Partial / Missing / N/A. **Tests:** `T` = automated test exists and
passed in the run above; `Probe` = only my audit probe; `None`. Paths are under `privacy_gateway/`
unless shown otherwise.

---

## Critical gaps (summary)

| # | Gap | P | Evidence |
|---|---|---|---|
| 1 | **Non-Latin typed text is never masked and passes validation.** `रोगी का नाम मीरा वेल है…` was sent unchanged in both modes. OCR marks non-Latin words `UNVERIFIED_TEXT` (`ocr.py:15,97-100`), but TXT, JSON, chat messages, PDF text layers and DOCX get no equivalent check. | P0 | [probe] hindi_text: `masked=0`, not refused |
| 2 | **Identifiers split by a line break leak partially and pass validation.** GLiNER mode: `Email: mira.vale@exa\nmple.test` → `Email: [PERSON_001].vale@exa\nmple.test` (surname and domain sent). Rules mode also leaked the second half of `Phone: 98765\n43210`. PDF wraps and DOCX breaks produce exactly this. | P0 | [probe] split_ids; EMAIL/PHONE regex exclude `\n` (`detection.py:13,15`) |
| 3 | **No verified redacted document.** The PDF/image review copy is built even when spans could not be located (`unlocated_spans > 0`, `redaction.py:62`), so it can still contain the original text, and it is never re-extracted to check. No DOCX output exists. | P1 | `redaction.py:31-70`, `api.py:216` |
| 4 | **Multi-column/table PDFs lose content.** `get_text(sort=True)` puts both columns on one line (`documents.py:159`); a context label then captures to end of line (`detection.py:247`), so `Patient: Mira Vale ⟶ Right second` became one PERSON and the right-column text vanished. | P1 | [probe] pdf_two_columns |
| 5 | **Clinical and invoice meaning altered by over-wide spans.** `Possible pneumonia` → `[HEALTH_CONDITION]` (uncertainty lost); `admitted 14 March 2026 to Ward 7B, Pune 411001` → `admitted [ADDRESS_001]` (admission date lost, both modes); GLiNER merged Qty `2` into an AMOUNT and masked the `Grand total` label. | P1 | [probe] clinical, indirect, invoice_table |
| 6 | **DOCX text extraction drops `w:br`/`w:tab` and table structure.** `Mira Va` `le` + break + `Seen today.` → `Mira ValeSeen today.` (glued words weaken detection and the whole-word reuse in `masking.py:39-46`). Tables come out one cell per line; list numbers and merged cells are lost. | P1 | [probe] docx_split_runs_and_break, docx_table_layout; `documents.py:286-288` |
| 7 | **Indirect identifiers / HIPAA Safe Harbor elements not covered.** Only date of birth is a date type; admission/discharge dates, ages over 89 (`92 years old` kept), ward/room numbers, and rare-attribute descriptions pass. | P1 | `policy.py:9-27`; [probe] indirect |
| 8 | **Evaluation too small to support any rate,** and validation reuses the same detector (correlated blind spots; acknowledged in `docs/security.md`). 8 records / 11 values; 13 records × 5 renderings. | P1 | `scripts/evaluate.py:14-61` |
| 9 | **No user/tenant isolation.** One API key = one trust domain; `/preview/*` returns originals to any key holder. | P1 | `api.py:47-85`, `docs/security.md` |

---

## 1. Document structure & layout

| Requirement | LLM text path | Masked document copy | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|---|
| Headings/subheadings | Partial | Partial (PDF/image) / Missing (DOCX) | PDF `get_text("text", sort=True)` `documents.py:159`; DOCX `w:p` text only `documents.py:286-288` | None | Heading levels and styles dropped. Emit Markdown-style heading markers from DOCX `pStyle` and PDF font size. | P2 |
| Paragraph order | Impl | Impl (PDF) | DOCX iterates `w:p` in part order; PDF sort by position | T `test_docx_table_header_footer` (content only) | No ordering assertion. Add an order test. | P3 |
| Page order/numbering | Partial | Impl (PDF in place) | Pages joined with `\n`, no page markers `documents.py:181` | None | LLM can't cite pages. Insert `--- page N ---` separators (excluded from detection). | P3 |
| Section boundaries | Partial | Impl (PDF) / Missing (DOCX) | Plain text only | None | Same as headings. | P3 |
| Tables, rows, columns | Partial | Impl (PDF, drawn in place) / Missing (DOCX) | DOCX: one cell per line; PDF: whitespace-aligned lines | None; [probe] docx_table_layout | Rows unrecoverable in DOCX text. Emit tab-separated rows per `w:tr`; use `page.find_tables()` for PDF. | P1 |
| Merged cells | Missing | Impl (PDF in place) / Missing (DOCX) | `gridSpan`/`vMerge` ignored | None | Repeat or mark merged values in emitted rows. | P2 |
| Lists and numbering | Missing | Impl (PDF) / Missing (DOCX) | `numPr` numbering not rendered | None | Render list numbers from `numbering.xml` or at least bullets. | P2 |
| Headers/footers | Impl (content) | Impl (PDF) / Missing (DOCX) | DOCX `header*`/`footer*` parts `documents.py:284`; PDF as page text | T `test_docx_table_header_footer` | DOCX part order follows zip listing, not document order. Order explicitly and label them. | P3 |
| Multi-column reading order | Partial | Impl (PDF in place) | `sort=True` interleaves columns line by line | [probe] pdf_two_columns — content lost | Gap #4. Cluster blocks into columns (`get_text("blocks")`) before joining; stop context values at 2+ spaces/tab. | P1 |
| Fonts/formatting | N/A | Impl (PDF untouched outside boxes) / Missing (DOCX) | Text only to LLM | None | Only relevant to a DOCX deliverable (see §6). | P3 |
| Spacing/alignment | Partial | Impl (PDF) | `sort=True` keeps spacing approximately | None | — | P3 |
| Page breaks | Partial | Impl (PDF) | `\n` between pages only | None | See page markers. | P3 |
| Form labels ↔ values | Partial | N/A | Context labels keep `Label: [TOKEN]` `detection.py:246-265`; PDF widgets refused `documents.py:152`; DOCX form fields (`fldChar`) refused `documents.py:275` | T `test_table_layout_labels_capture_next_line`, `test_unreadable_documents_rejected` | Fillable forms are rejected, not processed. Extract widget name/value pairs and mask values. | P2 |
| Footnotes/endnotes | Impl (DOCX), Partial (PDF) | Impl (PDF) / Missing (DOCX) | `footnotes.xml`, `endnotes.xml` read `documents.py:282`; PDF footnotes are page text with no anchor | None | No test. Add a DOCX footnote test; mark footnote anchors. | P3 |
| Cross-references | Missing (refused) | N/A | DOCX fields/`instrText` → `partial_extraction_risk` `documents.py:275-276` | T (docx unsupported part) | Fail-closed by design. Supporting fields needs field-result extraction. | P3 |
| No text overlap after masking | N/A | Partial (PDF) | Labels shrunk to fit `redaction.py:84-96`; image labels unsized `redaction.py:113-116` | T `test_masked_pdf_labels_fit_narrow_table_cells` | Image labels can overflow boxes; documented quirk that a cell can show another span's number. Fit image text; key labels by span. | P3 |

## 2. Content & semantic preservation

Unmasked text is copied verbatim (`masking.py:126-146`), so meaning is preserved *except where
a span is too wide* or where policy deliberately masks a value.

| Requirement | Status | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|
| Meaning of permitted content | Partial | Verbatim copy outside spans | `context_phrase_preservation` 87.5% (GLiNER) | Over-wide spans remove context (gaps #4, #5). Cap span widths; see below. | P1 |
| Factual information | Partial | Same | Eval phrase check only | Same. | P1 |
| Technical/medical terminology | Partial | DIAGNOSIS generalized in healthcare `config/policies.yaml:24` | None | Diagnoses become `[HEALTH_CONDITION]` by design; this removes clinical detail. Offer an approved `allow` profile per use case (AGENTS forbids automatic allow; keep it opt-in). | P2 |
| Numerical values | Partial | AMOUNT masks money; other numbers kept | [probe] clinical: labs/BP/HR preserved | Invoice Qty swallowed into AMOUNT (GLiNER). Reject model AMOUNT spans that cross 2+ spaces or contain non-money numbers. | P1 |
| Measurement units | Impl | Not detected as entities | [probe] clinical (`500 mg`, `72.5 kg` kept) | No automated test. Add regression test. | P2 |
| Currency values & precision | N/A by policy | AMOUNT `mask` in all categories `config/policies.yaml:16,26` | T invoice tests | AGENTS forbids auto-allowing amounts. If needed, add opt-in bucketing (e.g. `[AMOUNT_001 ≈ 1–5k INR]`). | P2 |
| Relationships between entities | Partial | Consistent tokens per value `masking.py:110-118` | T `test_cross_message_consistency_and_history` | Aliases split one person into several tokens (gap in §3). | P2 |
| Chronological order | Partial | Dates other than DOB kept | [probe] indirect | `City - PIN` rule absorbed an admission date into ADDRESS. Exclude month names/years from that regex (`detection.py:33-34`). | P1 |
| Negation | Impl | Not masked | [probe] clinical (`denies`, `not ruled out` kept) | No test. Add regression test. | P2 |
| Uncertainty | Partial | — | [probe] `Possible pneumonia` → `[HEALTH_CONDITION]` | GLiNER includes the hedge in the DIAGNOSIS span. Trim leading hedge/negation words (`possible`, `suspected`, `no`, `rule out`…) from DIAGNOSIS spans before masking. | P1 |
| Conditions/exceptions | Impl | Not masked | [probe] contract (`unless`, `before`) | No test. | P2 |
| Comparisons/operators | Impl | Not masked | [probe] contract (`1.5x`, `exceed`) | No test. | P3 |
| Document completeness | Partial | Whole request blocked on COMMERCIAL_TERM/INTERNAL_INFORMATION/SECRET `config/policies.yaml:17-19`; unreadable image regions silently skipped `documents.py:168-169` | T `test_block_prevents_transport` | Silent skip of image regions with no OCR text means the LLM gets an incomplete document without notice. Report skipped regions in the preview. | P2 |
| Task-relevant context | Partial | — | Eval phrase check | Over-masking of public bodies (`IRS`, hotel brands) — documented policy question in `docs/gliner-failures.md`. | P2 |
| Original intent | Partial | — | None | No measurement. | P2 |
| Medication dosages / clinical facts unchanged | Partial | No dose/drug types | [probe] dosages kept | Kept today, but the 9-digit PHONE pattern (`detection.py:15`) can match long runs of lab values; untested. Add a clinical regression set; exclude unit-adjacent digits from PHONE. | P1 |
| Contractual obligations / legal meaning | Partial | Only COMMERCIAL_TERM % discounts detected `detection.py:41-43` | T `test_commercial_terms_are_detected_by_rules` | Obligations are kept verbatim (good), but a late fee `2% per month` and liability caps were not treated as commercial terms. Decide policy; broaden patterns if they are confidential. | P2 |

## 3. PII/PHI detection & masking

| Requirement | Status | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|
| Personal names | Partial | GLiNER `person` `policy.py:10`; context labels `detection.py:47-49`; relation prefixes `detection.py:87` | T `test_real_local_gliner_and_guardrails`, eval (recall 100% on 4 names) | Rules mode misses unlabeled names (documented). Non-Latin names leak (gap #1). | P0 (via #1) |
| Patient names | Impl | `patient name` context + GLiNER | T `test_healthcare_example` | — | — |
| Doctor names | Impl (always masked) | PERSON, no role distinction | [probe] `Dr. Arun Shah` masked (GLiNER only) | No option to keep clinician names when the task needs them. Add a `CLINICIAN` sub-type with its own policy. Rules mode leaves them unmasked. | P3 |
| Phone numbers | Partial | Regex ≥9 digits `detection.py:15,223`; context | T `test_phone_keeps_area_code_bracket`, `test_ticket_numbers_are_not_phones` | Line-split numbers leak in rules mode (gap #2). | P0 (via #2) |
| Email addresses | Partial | Regex `detection.py:13` | T eval, `test_rule_type_wins_over_model_label` | Line-split email leaked in GLiNER mode (gap #2). Obfuscated (`mira dot vale at …`) leaks [probe]. | P0 |
| Physical addresses | Partial | GLiNER + US city/state/ZIP, Indian City-PIN, head/tail `detection.py:27-34,90-99` | T `test_address_extends_*`, `test_us_city_state_zip_is_an_address`, `test_indian_city_pin_line_is_an_address` | Rules mode misses unlabeled street addresses (eval). Other countries' formats only via GLiNER. | P2 |
| Dates of birth | Partial | Labeled only `detection.py:78-85` | T `test_date_of_birth_is_detected` | Unlabeled DOB (`born` covered; bare date beside a name not) relies on nothing — GLiNER has no date label. | P2 |
| Medical record numbers | Partial | `MRN\d+` regex + labels `detection.py:16,50-51` | T `test_healthcare_example` | Unlabeled MRNs in other formats are missed. Add site-configurable MRN regexes in policy. | P2 |
| Patient IDs | Partial | `patient id` label → MRN | T (labels) | Same as MRN. | P2 |
| Government identifiers | Partial | SSN, Aadhaar/VID, EPIC, Indian passport/DL `detection.py:19-21` | T `test_indian_id_numbers`, `test_id_card_labels` | US/India only; spaced SSN `123 45 6789` was typed PHONE (still masked) [probe]. Add other jurisdictions as needed. | P2 |
| Bank account numbers | Partial | IBAN-shaped regex + `account` label `detection.py:23,56` | T `test_bank_account_stops_before_prose` | Unlabeled domestic account numbers rely on GLiNER. | P2 |
| Financial identifiers | Partial | PAN/GSTIN `detection.py:24` | T | No payment-card (Luhn), routing/IFSC/SWIFT types; a card number was typed GOVERNMENT_ID [probe]. Add `PAYMENT_CARD` with Luhn check, IFSC/SWIFT. | P2 |
| Confidential org names | Impl (over-masks) | GLiNER ORGANIZATION; legal-suffix short form `masking.py:59-66` | T `test_registered_company_first_word_is_masked_everywhere` | Masks all organizations, including public bodies (documented). One known miss sent (`Marriott Courtyard SF`). | P2 |
| Internal project names | Partial | `custom_terms` `config/policies.yaml:34-36`; `Project:`/`Matter:` labels | T `test_custom_terms_match_across_line_breaks`, `test_matter_label_is_a_project` | Unlisted, unlabeled project names depend on GLiNER `project name`. | P2 |
| Sensitive contractual info | Partial | COMMERCIAL_TERM regex + label + block `detection.py:41-43,65-67` | T | Narrow pattern set (see §2). | P2 |
| Indirect identifiers | Missing | No type for ages >89, admission dates, ward/room, rare attributes | [probe] indirect | Gap #7. Add `DATE` (non-DOB, healthcare policy), `AGE_OVER_89`, room/bed patterns; document residual quasi-identifier risk. | P1 |
| Repeated identifiers | Impl | `propagate()` `masking.py:49-78` | T `test_cross_message_consistency_and_history`, `test_recurrences_match_whole_words_only` | Glued forms (`MiraVale`) not re-masked (documented). | P3 |
| Fragmented/split identifiers | Partial | Custom terms allow any whitespace `detection.py:271` | T `test_custom_terms_match_across_line_breaks` | Gap #2: emails/phones/IDs split by `\n` or PDF hyphenation. Detect on a whitespace-normalized shadow text with an offset map; residual-check every token of masked emails. | P0 |
| Overlapping detections | Impl | `overlapping_groups` union `detection.py:416-427`; winner rules `masking.py:92-103` | T `test_overlap_union_does_not_leave_suffix`, `test_block_wins_over_overlap` | Union can erase adjacent useful text (documented). | P3 |
| Unusual names/formats | Partial | GLiNER | T eval (`Zorina Quell`), ID-card tests | Tiny sample. | P2 |
| Consistent placeholders | Impl | Casefolded key `masking.py:109-118` | T | Per request only (by design). | — |
| Distinct placeholders per identity | Partial | One token per distinct string | [probe] aliases: `Mira Vale`/`Mira` and `Dr. Arun Shah`/`Dr. Shah` → 4 tokens | Aliases not linked (documented). Two different people with the same name share a token. Add surname/first-name alias linking within a request. | P2 |
| Relationships between pseudonyms | Partial | Same | Same | Same. | P2 |
| Secure mappings | Impl | Request-local `MaskContext`, cleared in `finally` `masking.py:18-36`, `api.py:204-206,256-257,317-318` | T `test_request_local_mapping` | Python memory not securely erased (documented). | P3 |
| Mappings never reach LLM | Impl | Payload rebuilt from `{model, messages, max_tokens, stream}` `transport.py:25-31`, `proxy_hook.py:52-70` | T `test_request_local_mapping`, `test_hook_discards_unsigned_metadata_*`, integration | — | — |
| Evaluate false positives | Partial | `scripts/evaluate.py` | Eval: 6 FP (GLiNER) | 8 records. Gap #8. | P1 |
| Evaluate false negatives | Partial | Same | Eval: 0 FN (GLiNER), 2 (rules) | Same. | P1 |
| Block data that cannot be validated | Partial | UNVERIFIED masking, OCR-unavailable refusal, `partial_extraction_risk`, validator 503 | T `test_fail_closed_validator`, `test_images_refused_without_ocr`, `test_unreadable_and_non_latin_ocr_words_are_masked` | Non-Latin non-OCR text is not treated as unverifiable (gap #1). | P0 |

## 4. Tables, forms & structured data

| Requirement | Status | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|
| Row-to-record | Partial (PDF lines) / Missing (DOCX) | See §1 tables | [probe] docx_table_layout | Emit rows as TSV. | P1 |
| Column-to-value | Partial | Context label captures to end of line | [probe] pdf_two_columns | Stop at column gaps. | P1 |
| Table headers | Partial | Kept as text; GLiNER masked `Grand total` as AMOUNT [probe] | T `test_invoice_line_items_and_total_labels_are_not_entities` (rules) | Add GLiNER-mode invoice regression. | P2 |
| Cell boundaries | Partial | `adds_nothing` keeps cells apart `detection.py:159-176` | T `test_model_amount_span_does_not_merge_table_cells` | Qty+amount still merged in probe. | P1 |
| Merged cells | Missing | — | None | See §1. | P2 |
| Invoice line items | Partial | Descriptions kept; amounts masked | T `test_invoice_line_items_*` | — | P2 |
| Quantities, prices, taxes, totals | Partial | Prices/totals masked by policy; quantity can be absorbed | [probe] | Fix span merge; keep quantities. | P1 |
| Arithmetic consistency | Missing | Amounts become independent tokens | None | If needed, opt-in consistent scaling/bucketing or relation tags (`[AMOUNT_004] = sum`). Policy decision. | P2 |
| Form field associations | Partial | Labels kept beside tokens; fillable forms refused | T | See §1. | P2 |
| JSON structure | Missing | Flattened to `path: value` lines `documents.py:28-56` (docstring says not promised) | T `test_sensitive_json_keys_and_numbers` | Masked output is text, not JSON. Mask leaf strings in place and re-serialize. | P2 |
| Nested objects/arrays | Partial | Paths encode nesting | [probe] json | Same. | P2 |
| Data types | Partial | Numbers/null rendered via `json.dumps`; strings unquoted | [probe] json | Types are ambiguous (`"34"` vs `34`). Same fix. | P2 |
| Null/missing semantics | Partial | `null` kept; empty dict renders as `[]` `documents.py:52-53` | None | Empty `{}` and `[]` are indistinguishable. Render `{}` for dicts. | P3 |
| Record ordering | Impl | Dict/list order preserved | None | — | P3 |
| Safe identifiers for joins | Partial | Tokens consistent within one request only | T | No stable cross-request pseudonyms (deliberate). Keyed HMAC pseudonyms per tenant would allow joins; privacy review needed. | P3 |

## 5. PDF masking & redaction

Text path vs review copy noted per row.

| Requirement | Status | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|
| Extract searchable text | Impl | `documents.py:139-182` | T `test_searchable_pdf` | — | — |
| Reading order | Partial | `sort=True` | [probe] | Gap #4. | P1 |
| Identifiers across text blocks | Partial | Pages joined; GLiNER 600-char windows with overlap `detection.py:307-355` | None | Values wrapped across lines (gap #2) and columns. | P0 (via #2) |
| Map text to page coordinates | Partial (review copy) | Text layer: `search_for(fragment)` `redaction.py:46-47`; OCR: exact word boxes | T `test_pdf_image_regions_are_read_and_masked` | `search_for` re-finds text, so it can hit other occurrences and misses ligature/hyphenated text. Map offsets via `get_text("rawdict")` char boxes instead. | P2 |
| Redact correct boxes | Partial (review copy) | `add_redact_annot` per rect | T `test_masked_pdf_labels_fit_narrow_table_cells` | Documented label mix-up on repeated values. | P2 |
| Remove underlying text | Impl (review copy) | `apply_redactions(images=PIXELS)` `redaction.py:63-66` | T asserts name absent from `get_text()` | Only when located. | — |
| Prevent recovery (copy/search) | Partial | Same + `scrub()` | T (one case) | Gap #3: copy is still produced when `unlocated > 0`. Re-extract the copy and refuse it if any `context.originals` value remains. | P1 |
| Headers/footers | Impl (text) | Page text includes them | None | No test. | P3 |
| Forms/annotations | Partial (refused) | Any annot/widget → `partial_extraction_risk` `documents.py:152-153` | None for annots specifically | Fail-closed, but many real PDFs carry harmless link annots. Extract annot contents/widget values and mask them. | P2 |
| Embedded images/attachments | Impl | Images OCR'd `documents.py:155-174`; attachments refused except C2PA `documents.py:148-149,214-236` | T `test_pdf_image_regions_*`, `test_pdf_other_attachments_rejected`, `test_pdf_content_credentials_manifest_is_ignored` | — | — |
| Hidden content/layers | Missing | Hidden text extracted with visible text; OCGs not inspected; vector glyphs not cross-checked (documented) | [probe] white text and 1-pt text were extracted and sent (masked), unflagged | Detect invisible text (render mode 3, white-on-white, tiny fonts, off-page, hidden OCG) and flag or refuse it. | P2 |
| Metadata | Impl | Not extracted or sent; review copy `doc.scrub()` `redaction.py:68` | [probe] metadata empty in copy | No test. Add one. | P3 |
| Non-sensitive visual content | Impl (review copy) | `PDF_REDACT_LINE_ART_NONE`; image pixels only inside boxes | None | — | P3 |
| Redacted PDF readable | Partial | — | T reads text back | No render check. | P3 |
| Re-extract and validate sanitized PDF | Missing (production) | Only tests re-extract | T only | Gap #3. | P1 |

## 6. DOCX masking

| Requirement | LLM text path | Masked DOCX | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|---|
| Paragraphs | Impl | Missing | `documents.py:286-288` | T `test_docx_table_header_footer` | — | — |
| Tables & nested tables | Partial | Missing | Cells flattened | T (content only); [probe] | Gap #6. | P1 |
| Headers/footers | Impl | Missing | `documents.py:284` | T | Order. | P3 |
| Names split across runs | Partial | Missing | All `w:t` in a paragraph joined | [probe] `Mi`+`ra Va`+`le` detected | `w:br`, `w:tab`, `w:cr`, `w:noBreakHyphen` dropped → glued words. Emit `\n`/`\t`/`-`. | P1 |
| Permitted formatting | N/A | Missing | No DOCX writer | None | Build a run-level rewriter (replace text in `w:t` across runs using the offset map) if a redacted DOCX is required. | P2 |
| Numbered lists | Missing | Missing | `numPr` ignored | None | See §1. | P2 |
| Tables & cell relationships | Partial | Missing | — | None | Gap #6. | P1 |
| Text boxes/embedded objects | Partial (refused) | N/A | `drawing`, `pict`, `object`, `AlternateContent`, media → refused `documents.py:248-281` | T `test_docx_unsupported_part_rejected` | Fail-closed; any DOCX with an image or text box cannot be processed at all. Extract `txbxContent` and OCR media. | P2 |
| Comments | Impl | Missing | `comments.xml` text extracted and masked `documents.py:282` | None | Comment authors (attributes) not extracted (fine). Add a test. | P3 |
| Tracked changes | Partial (refused) | N/A | `ins`/`del` → refused | [probe] refused | Fail-closed. Acceptable; message could say "accept changes first". | P3 |
| Hidden text | Partial | Missing | `w:vanish` text extracted like visible text | [probe] hidden run sent (masked), unflagged | Flag hidden text in the preview. | P2 |
| Properties/metadata | Impl (never sent) | N/A | `docProps/` not read | [probe] author/comments absent | Sanitizing them only matters for a DOCX deliverable. | P3 |
| Removed info unrecoverable | N/A | Missing | No DOCX output | None | — | P2 |
| Sanitized DOCX opens | N/A | Missing | — | None | — | P2 |

## 7. Scanned documents & images

| Requirement | Status | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|
| Scanned vs searchable | Partial | Implicit per page: text layer + OCR of every image region `documents.py:155-174` | T `test_pdf_image_regions_are_read_and_masked[mixed/scanned]` | No explicit classification reported. Add `pages_ocr`/`pages_text` to preview. | P3 |
| Local OCR or reject | Impl | Tesseract CLI, no network `ocr.py:36-60`; refused when missing `documents.py:130-131,157-158` | T `test_images_refused_without_ocr`, real-Tesseract tests | — | — |
| OCR reading order | Partial | Tesseract `--psm 3` order `ocr.py:73-75` | T `test_layout_offsets_match_words` | Multi-column scans untested. | P2 |
| Scanned tables | Partial | Text only | None | No structure. | P2 |
| Sensitive info inside images | Partial | Text via OCR only | T ID-card tests | Non-text content not analysed (images are never sent to the LLM, so this is a review-copy gap, not an LLM leak). | P2 |
| Handwriting | Missing | Documented not implemented | None | Low-confidence handwriting becomes UNVERIFIED if read at all; unread regions skipped. | P2 |
| Signatures/stamps/photos | Missing | Review copy keeps them `redaction.py:8-11` | None | Not sent to LLM. For a deliverable, detect faces/signature blobs locally and box them. | P2 |
| QR/barcodes | Missing | Not decoded | None | QR content (e.g. Aadhaar) never sent, but kept in review copy. Decode locally (zbar) to mask copy or refuse. | P2 |
| Mask sensitive visual info | Partial | Boxes over masked OCR words `redaction.py:99-119` | T | Text only. | P2 |
| OCR completeness | Partial | Low-confidence words → UNVERIFIED; empty regions silently skipped `documents.py:168-169` | T `test_unreadable_and_non_latin_ocr_words_are_masked` | No coverage metric. Report regions with zero OCR text; optionally refuse. | P2 |
| Flag uncertain regions | Impl | `UNVERIFIED_TEXT` masked; count in preview `api.py:239-246` | T | — | — |
| Block when content can't be inspected | Partial | OCR missing → refuse; blank page → refuse `documents.py:176` | T `test_blank_pdf_page_rejected` | Unreadable image regions on pages that also have text pass silently. | P2 |

## 8. LLM integration & security

| Requirement | Status | Evidence | Tests | Gap → recommended fix | P |
|---|---|---|---|---|---|
| Mask before external requests | Impl | `api.py:295-308` sanitize → transport | T `test_cross_message_consistency_and_history`, integration | — | — |
| Validate every outbound request | Impl | Guardrails input validator per message `pipeline.py:45-51`; HMAC at proxy | T `test_fail_closed_validator`, `test_actual_hook_*` | Validator correlated with detector (documented). Add an independent rule-only/second-model validator. | P1 |
| Validate responses | Impl | `api.py:309-310`, `validators.py:113-115` | T `test_output_blocked` | — | — |
| Prevent forwarding originals | Impl | Only sanitized messages sent | T integration captures provider body | Non-Latin text exception (gap #1). | P0 (via #1) |
| Prevent forwarding original identifiers | Partial | Same | T | Gaps #1, #2. | P0 |
| Leakage via system prompts | Impl | Client `system` messages masked like others `schemas.py:25`; proxy forbids `system` key `proxy_hook.py:46` | T `test_bypass_fields_rejected` | No server-side system prompt exists. | — |
| Leakage via history | Impl | All messages sanitized together `pipeline.py:17-56` | T `test_cross_message_consistency_and_history` | — | — |
| Leakage via retrieved context | N/A | No RAG | — | — | — |
| Logs/traces | Impl | No logging; telemetry off `validators.py:5-14`, `detection.py:290-292`; `--no-access-log` | T `test_detector_failure_no_outbound_or_trace`, `test_provider_error_no_echo_or_log` | — | — |
| Error messages | Impl | Static codes `errors.py`, handlers `api.py:131-153` | T `test_validation_errors_do_not_echo`, `test_unauthorized_never_echoes` | — | — |
| Temporary files | Partial | Multipart may spool to disk; Compose tmpfs (documented) | None | Native runs use host temp. Read uploads into memory with a cap, or set `tempfile.tempdir` to tmpfs. | P2 |
| Document metadata | Impl | PDF/DOCX metadata never extracted | [probe] | — | — |
| User/tenant isolation | Missing | Single API key `api.py:57-60` | None | Gap #9. | P1 |
| Prevent bypassing the gateway | Impl | Signed approval + replay `approval.py`, strict hook `proxy_hook.py`, internal network | T `test_real_litellm_ordering_and_bypass` (rules + GLiNER), `test_ticket_integrity_expiry_replay` | Replay cache per proxy process (documented). Shared nonce store before scaling. | P2 |
| Restrict access to original patient info | Partial | `/preview/*` returns `original_text` and spans to any key holder `api.py:225-227` | T | Add a role/scope for preview. | P1 |
| Configurable policies | Impl | `config/policies.yaml`, validated `policy.py:43-67` | T `test_explicit_allow_policy_preserves_diagnosis` | Server-side only (good). | — |
| Document-specific policies | Partial | 5 fixed categories `policy.py:47-53`, caller-chosen | None | `invoices`, `agreements`, `guidelines` equal the defaults; only `healthcare` differs. See §9. | P2 |
| Block on detection/validation failure | Impl | `pipeline.py:105-122`, `detection.py:410-413` | T `test_fail_closed_validator`, `test_detector_failure_no_outbound_or_trace` | — | — |
| No transmission on incomplete extraction | Impl | `partial_extraction_risk`, `ocr_unverified_text` | T document tests | Silent-skip of empty OCR regions (§7). | P2 |
| Re-identification authorization | N/A | No re-identification exists; replies keep placeholders (`README.md:71`) | — | If restoration is added, require a separate authorized endpoint. | — |

## 9. Document-specific masking policies

Policy categories are `general`, `healthcare`, `invoices`, `agreements`, `guidelines`
(`config/policies.yaml:21-30`). Only `healthcare` changes anything (DIAGNOSIS → generalize). The
caller picks the category; nothing classifies the document.

| Document type | Status | Gap → recommended fix | P |
|---|---|---|---|
| Healthcare records | Partial | `healthcare` exists but lacks non-DOB dates, ages >89, room/bed. | P1 |
| Discharge summaries | Partial | Same category; no admission/discharge date handling. | P1 |
| Medical reports | Partial | Same. | P2 |
| Prescriptions | Missing | No drug/dose protection rules, prescriber licence/DEA number types. | P2 |
| Invoices | Partial | Category identical to defaults. | P2 |
| Financial statements | Missing | No category; card/routing numbers not typed. | P2 |
| Agreements/contracts | Partial | Identical to defaults; COMMERCIAL_TERM blocks the whole request. | P2 |
| Internal guidelines | Partial | Identical to defaults. | P3 |
| SOPs | Missing | Use `guidelines`; no category. | P3 |
| Insurance documents | Missing | Only `insurance member id` label → MRN. Add policy number/claim ID types. | P2 |
| HR documents | Missing | No employee ID, salary, performance, or protected-attribute handling. | P2 |
| General confidential | Impl | `general`. | — |

Recommended: add the missing categories with real differences, and optionally a local classifier
that *suggests* the strictest matching category (never weakens the caller's choice).

## 10. Testing & evaluation

| Requirement | Status | Evidence | Gap → recommended fix | P |
|---|---|---|---|---|
| Precision | Impl (tiny) | `evaluate.py`: 64.7% GLiNER, 81.8% rules | 11 values; no CI. Gap #8. | P1 |
| Recall | Impl (tiny) | 100% / 81.8% | Same. | P1 |
| Masking accuracy | Impl (tiny) | `masking_accuracy_approved_values` | Same. | P1 |
| Leakage rate | Impl (tiny) | Literal; near-leak in `evaluate_documents.py` | Same. | P1 |
| False positives | Impl (tiny) | 6 / 2 | Same. | P1 |
| False negatives | Impl (tiny) | 0 / 2 | Same. | P1 |
| Latency | Impl | Mean/max per record | No load/concurrency test. | P3 |
| Semantic preservation | Partial | Selected phrase check | No meaning metric. | P2 |
| Numerical consistency | Missing | — | Add assertions that non-protected numbers survive byte-for-byte. | P1 |
| Clinical integrity | Missing | — | Add a synthetic clinical set asserting negation, hedges, doses, units, dates survive. | P1 |
| Contractual meaning | Missing | — | Same for obligations/conditions. | P2 |
| Table preservation | Missing | — | Add PDF/DOCX table fixtures. | P1 |
| Layout preservation | Missing | — | Only relevant once a document deliverable exists. | P2 |
| OCR accuracy/coverage | Partial | `evaluate_documents.py` (27/28 exact) | One synthetic font; no real scans. | P2 |
| Hidden-content removal | Partial | One review-copy text check | Add hidden-layer/white-text fixtures. | P2 |
| Repeated/overlapping entities | Impl | `test_overlap_*`, `test_cross_message_*` | — | — |
| Multilingual | Partial | Hindi OCR unit test with fake words | Typed non-Latin untested and fails (gap #1). | P0 |
| Unusual identifier formats | Partial | ID-card tests | Line-split and obfuscated forms untested and fail. | P0 |
| Malformed/corrupted documents | Impl | `test_unreadable_documents_rejected` | No fuzzing (documented). | P2 |
| Extraction/model failures | Impl | `test_detector_failure_*`, `test_fail_closed_validator`, `test_images_refused_without_ocr` | No OCR-timeout test. | P3 |
| Gateway bypass | Impl | `test_real_litellm_ordering_and_bypass`, approval tests | — | — |
| Synthetic healthcare | Impl | Eval + tests | — | — |
| Synthetic invoices/agreements | Partial | Demo runs and samples; few automated assertions | Turn `samples/demo-run` outputs into golden regression tests. | P2 |
| Automated regression tests | Impl | 141 passing | — | — |
| Human review for uncertain cases | Impl | `/preview/*`, `review.html`, `approved_preview_sha256` `api.py:302-303` | T `test_chat_sends_only_the_reviewed_masking`, `test_chat_refuses_text_that_differs_from_review` | — | — |

---

## Prioritized recommendations

**P0 — fix before any external provider use**

1. Treat non-Latin text as unverifiable on *every* input path: reuse `ocr.LATIN_ONLY` in
   `Pipeline.sanitize` to add `UNVERIFIED_TEXT` ranges (or refuse) for words outside the evaluated
   scripts. Add tests for TXT, JSON, chat, PDF text layer and DOCX.
2. Detect identifiers across line breaks and hyphenation: run detection on a whitespace-normalized
   shadow text with an offset map back to the original; extend the residual check to every
   alphanumeric token of a masked email/phone/ID. Add the split-email and split-phone probes as
   regression tests.

**P1**

3. Make the PDF/image copy safe or label it unsafe: if `unlocated_spans > 0`, or re-extraction of
   the copy finds any original value, return no copy.
4. Column-aware PDF extraction and context values that stop at column gaps (2+ spaces/tab).
5. Narrow span boundaries: strip hedge/negation words from DIAGNOSIS; exclude dates from the City-PIN
   rule; stop model AMOUNT spans at column gaps and non-money numbers.
6. DOCX extraction: emit breaks/tabs, tab-separated table rows, list numbers.
7. Healthcare Safe-Harbor coverage: non-DOB dates, ages over 89, room/bed numbers.
8. Build a larger labeled synthetic corpus per category (clinical, invoice, contract, tables,
   multi-column, multilingual, split/obfuscated identifiers) with confidence intervals, and an
   independent second validator.
9. Per-user/tenant keys with scoped policies; restrict `/preview` originals to a reviewer scope.

**P2** — alias linking; real per-category policies (prescriptions, insurance, HR, financial);
payment-card/IFSC/SWIFT types; hidden-text flagging; annotation/form/text-box extraction instead of
refusal; JSON in-place masking; OCR coverage reporting; QR/signature handling in the copy; shared
replay store; temp-file control on native runs.

**P3** — clinician-name policy, page markers, `{}` vs `[]`, minor mislabels, label collisions in
the review copy.

## Limitations of this audit

- Probe findings come from one run per case on hand-written synthetic text; they show a failure
  mode exists, not how often it occurs.
- GLiNER output can vary with surrounding text; some probes may behave differently in other contexts.
- No real scans, real PDFs from production tools, or non-English corpora were tested.
- Passing tests and these probes do not establish zero leakage.
