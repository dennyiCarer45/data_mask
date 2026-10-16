# GLiNER failure log

Where `gliner_small-v2.1` (threshold 0.35) failed on a real run, and what now covers it. Add a row
for every new document that is refused, leaks, or is badly over-masked. Synthetic data only.

**Miss** = sensitive text left unmasked. **False alarm** = harmless text flagged (a second-pass
false alarm makes the whole document unsendable). **Over-mask** = harmless text replaced.

| Date | Document | Text | GLiNER did | Kind | Now covered by |
|---|---|---|---|---|---|
| 2026-10-16 | Sample agreement | `28 Orchid Business Park` | Started the address at `Sector 62` | Miss | `ADDRESS_HEAD` |
| 2026-10-16 | Aadhaar card photo | `Female`, `Delivery Manager` | PERSON / ORGANIZATION | False alarm | Generic words |
| 2026-10-16 | Call summary (TXT) | `hospital`, `insurer`, `ward doctors`, `care coordination team` | ORGANIZATION on second pass | False alarm | Generic words |
| 2026-10-16 | Call summary (TXT) | `briefly` | Tagged `iefly` as ORGANIZATION (window cut mid-word) | Over-mask | Windows split at whitespace |
| 2026-10-16 | Call summary (TXT) | `Asha Rao` / `Asha` / `Mrs. Rao` | Masked, but as three different people | Utility | Not fixed (see decisions) |
| 2026-10-16 | Chat record (JSON) | `speaker`, `agent` | PERSON | Over-mask | Generic words |
| 2026-10-16 | Chat record (JSON) | `CS-20431` | PHONE | Over-mask | Ticket prefixes in `REFERENCE` |
| 2026-10-16 | Meeting summary (PDF) | `mei.tanaka@…` | PERSON (0.977) over the EMAIL rule | Wrong label | Rule type wins |
| 2026-10-16 | Meeting summary (PDF) | `our account team` | ORGANIZATION on second pass | False alarm | Generic words |
| 2026-10-16 | Meeting summary (PDF) | `Project\nCedar` | ORGANIZATION; custom term missed the line break | Wrong label | Custom terms match any whitespace |
| 2026-10-16 | Legal invoice (`invoice1_modern.pdf`) | `New York, NY 10022`, `San Francisco, CA 94105` | Masked the street only; city/state/ZIP after `\|` or on the next line | Miss (caught by second pass) | US city/state/ZIP rule |
| 2026-10-16 | Legal invoice | `Volta` in "review of Volta document production" | Masked `Volta Commerce Group, LLC` but not the short form; second pass also missed it | **Miss, would have been sent** | First word of a legal-suffix company is propagated |
| 2026-10-16 | Legal invoice | `Partner / Member`, `Senior Associate`, `Analyst`, `Paralegal`, `Patent Agent`, … | PERSON, then every `partner` masked by propagation | Over-mask | Generic words (job titles) |
| 2026-10-16 | Legal invoice | `Market    Amount`, `eDiscovery`, `Third-party eDiscovery vendor`, `Tel`, `VP` | AMOUNT / ORGANIZATION / PHONE / PERSON on second pass | False alarm | Generic words |
| 2026-10-16 | Legal invoice | `MB-2025-10477` after `Invoice No:` | Phone pattern matched the digits | Over-mask | `LABELLED_REFERENCE` |
| 2026-10-16 | Legal invoice | `(212) 555-8800` | Rule left the `(` behind | Cosmetic | Phone pattern takes the bracket |
| 2026-10-16 | Legal invoice | `generative AI platform` | ORGANIZATION on second pass (0.354) | False alarm | Generic words |
| 2026-10-16 | Legal invoice | `Hilton Garden Inn NYC` | Missed on the original text; found only after masking (0.609) | Miss (caught by second pass) | Mask-again step (`second_look`) |
| 2026-10-16 | Legal invoice | `Marriott Courtyard SF` | Missed on both looks | **Miss, sent** | Not covered; a public hotel name, but policy masks organizations |
| 2026-10-16 | Legal invoice | `cite-checking authorities` | ORGANIZATION (0.366) | Over-mask | Generic words |
| 2026-10-16 | Legal invoice | `Platform Technology Litigation Defense` | ORGANIZATION (0.417); it is the matter name | Wrong label | `Matter:` label is PROJECT |
| 2026-10-16 | Legal invoice | `$1,050   $2,100.00`, `Standard      $78.50` | One AMOUNT span over two table cells / the Market column | Over-mask | Model spans that add only spacing or generic words to rule matches are dropped |
| 2026-10-16 | Legal invoice | `firm overhead`, `rent`, `standard technology` | COMMERCIAL_TERM on second look (0.41-0.45), which blocked the whole invoice | False alarm (block) | Generic words |
| 2026-10-16 | Legal invoice | `first-year` | Not GLiNER: the masked value `IRS` was reused inside the word | Over-mask | Recurrences match whole words |
| 2026-10-16 | ID card photos (`id_card_demo.py`) | `Kolkata - 700091`, `Karnataka - 575001` (second address line) | Masked the street line only | **Miss, would have been sent** | Indian City - PIN rule |
| 2026-10-16 | ID card photos | `Aadhaar`, `Elector`, `SPECIMEN`, `DRIVING LICENCE Transport Department` | GOVERNMENT_ID / PERSON / ORGANIZATION | Over-mask | Generic words |
| 2026-10-16 | ID card photos | `Blood Group: B+` | Not detected (no rule or label) | Miss (health data) | `Blood group` label is DIAGNOSIS |
| 2026-10-16 | ID card photos | `Perman` (OCR split `Permanent` across lines) | PERSON on second look | Over-mask | Not fixed (OCR fragment) |

Policy question, not changed: public bodies and tools (`FTC`, `IRS`, `USPTO`, `Westlaw`,
`LexisNexis`, `UberX`, `Relativity`, `ECF`, `BillerAssist`, hotel brands) are masked as
organizations although they reveal nothing about the client. Not masked: `Matter No:
VCG-LIT-2025-003`, whose `VCG` is the client's initials.

Pattern so far: most refusals are second-pass false alarms on role words and headings; the misses
are address fragments in multi-column layouts, short forms of names, and the model changing its
mind once nearby values are masked. The same kind of name (two hotel brands in one table) can be
caught in one row and missed in the next, so a clean run is not evidence that a type is covered.
