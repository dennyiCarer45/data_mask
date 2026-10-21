# What the gateway masks

Everything below is detected locally. Sources: `privacy_gateway/detection.py`,
`privacy_gateway/policy.py`, `config/policies.yaml`. See `docs/evaluation.md` for measured
results and known misses; the gateway does not guarantee zero leakage.

## How a value is handled

Each detected value gets one of four actions from `config/policies.yaml`:

| Action | What happens | Example output |
|---|---|---|
| `mask` | Replaced with a numbered placeholder; the same value gets the same number within a request | `[PERSON_001]` |
| `generalize` | Replaced with a fixed category token | `[HEALTH_CONDITION]` |
| `block` | The whole request is refused (`policy_blocked`); nothing is sent | n/a |
| `allow` | Left as is (no type is allowed by default) | n/a |

When detections overlap, the strictest action wins (block > mask > generalize > allow).

## Entity types

"Model" means the local GLiNER model (only in `gliner` mode). "Rules" means regex patterns,
labelled fields (`Name: ...`, or a label line with the value on the next line), and custom terms.
Rules run in both modes.

| Type | What it covers | Detected by | Default action |
|---|---|---|---|
| `PERSON` | People's names; values after `Name`, `Patient`, `Customer`, `Contact`, `Surname`, `Elector's name`, `Father's/Mother's/Husband's/Spouse's/Guardian's name`; names after `S/O`, `D/O`, `W/O`, `C/O` | Model + labelled fields | mask |
| `EMAIL` | Email addresses | Rules + model | mask |
| `PHONE` | Phone numbers with 9+ digits, including `+` and `(212)` forms; values after `Phone`, `Mobile`, `Telephone` | Rules + model | mask |
| `ADDRESS` | Street addresses; US `City, ST 12345` lines; Indian `City - 560038` PIN lines; values after `Address`, `Billing/Shipping/Street address`. Adds the house-number segment before, and the locality/postcode after, a detected address | Rules + model | mask |
| `MRN` | `MRN 12345`-style numbers; values after `Patient ID`, `Medical record number`, `Insurance member ID`, `Member number` | Rules + model | mask |
| `DATE_OF_BIRTH` | Dates after `Date of birth`, `DOB`, `D.O.B.`, `Birth date`, `Born (on)`; years after `Year of birth`, `YOB` | Rules only | mask |
| `GOVERNMENT_ID` | US SSN; Aadhaar (12 digits) and VID (16 digits), including `XXXX XXXX 1234`; voter ID (EPIC); Indian passport; Indian driving licence; values after `SSN`, `Aadhaar`, `Passport`, `UID`, `VID`, `EPIC`, `Voter ID`, `Enrolment`, `Licence/License`, `DL` | Rules + model | mask |
| `BANK_ACCOUNT` | IBAN-shaped numbers (capitals); values after `Account`, `Bank account (number)`, `IBAN` | Rules + model | mask |
| `TAX_ID` | Indian PAN and GSTIN; values after `Tax ID`, `PAN`, `GSTIN` | Rules + model | mask |
| `ORGANIZATION` | Company and organization names; values after `Party`, `Company (name)`, `Organization (name)`, `Confidential party`. The first word of a company name ending in a legal suffix (LLC, Ltd, ...) is masked wherever it appears | Model + labelled fields | mask |
| `PROJECT` | Project and legal-matter names; values after `Project (name)`, `Internal project`, `Matter (name)`; custom terms (`Project Cedar`, `Orion Vault`) | Model + labelled fields + custom terms | mask |
| `DIAGNOSIS` | Medical conditions; values after `Diagnosis`, `Condition`, `Blood group` | Model + labelled fields | mask, or **generalize** in the `healthcare` category |
| `AMOUNT` | Currency values (`$`, `€`, `£`, `₹`, `USD`, `INR`, `EUR`, `GBP`); money-shaped table numbers (`1,200.00`); values after `Amount`, `Total`, `Subtotal`, `Balance (due)`, `Invoice amount` | Rules + model | mask |
| `COMMERCIAL_TERM` | Percentage concessions (`22 percent rebate`, `discount of 17%`); values after `Commercial terms`, `Protected terms`, `Confidential <field>`, `Negotiated <field>`, `Rebate`, `Budget (ceiling)`, `Price cap`, `Rate card` | Rules + model | **block** |
| `INTERNAL_INFORMATION` | Confidential organizational content; values after `Internal information/guidelines/strategy`; custom term `confidential roadmap` | Model + labelled fields + custom terms | **block** |
| `SECRET` | API keys (`sk-...`) and AWS access key IDs (`AKIA...`); values after `Password`, `Secret`, `API key`, `Access token` | Rules + model | **block** |
| `UNVERIFIED_TEXT` | OCR words read with confidence below 60, and any non-Latin word (e.g. Hindi), since their content can't be checked | OCR | mask (policy forbids allow) |

## What is deliberately not masked

- Document and ticket numbers such as `INV-2026-00417`, `PO-44718`, `CS-20431`, or anything after
  `Invoice No`, `Order No`, `Ticket No`, `Case No`, `Reference No`. The model often mistakes these
  for phones or bank accounts, so those guesses are dropped. SSN-shaped and other rule matches
  inside them still apply.
- Generic words the model sometimes tags, such as pronouns, roles and headings ("her", "All staff",
  "Expense Policy"). This filter only applies to model detections, never to rules or labelled fields.
- Field labels themselves (`Patient:`, `Date of birth`); only the value after them is masked.

## Custom terms

Organization-specific phrases go under `custom_terms` in `config/policies.yaml`. They are matched
as literal text, case-insensitive, across line breaks (never as regex). Current list:

- `PROJECT`: `Project Cedar`, `Orion Vault`
- `INTERNAL_INFORMATION`: `confidential roadmap`

## Categories

The request's `category` can change actions. Current overrides:

| Category | Overrides |
|---|---|
| `general` | none |
| `healthcare` | `DIAGNOSIS` is generalized to `[HEALTH_CONDITION]` |
| `invoices` | `AMOUNT` is masked (same as the default) |
| `agreements` | `COMMERCIAL_TERM` blocks (same as the default) |
| `guidelines` | `INTERNAL_INFORMATION` blocks (same as the default) |

## Limitations

- **Rules mode is for demos only.** Names, organizations, diagnoses and projects without a label
  need the GLiNER model, so rules mode leaks unlabelled names (for example on ID cards).
- Known miss in the evaluation fixtures: `Marriott Courtyard SF` is sent unmasked.
- Different spellings or aliases of the same entity are not linked to one placeholder.
- Dates other than dates of birth (visit, invoice, contract dates), ages, gender, and free-text
  descriptions of people have no type of their own, so they are sent as is.
- Photos, QR codes, signatures and other graphics are not masked. Only text is sent to the AI, but
  the PDF/image review copy still shows them.
- Handwriting, legacy `.doc` files and languages other than English are not supported (non-Latin
  OCR words are masked as `UNVERIFIED_TEXT` without being checked).
