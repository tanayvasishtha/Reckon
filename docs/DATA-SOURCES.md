# Data sources

Every dataset here was verified by downloading it and reading the actual
columns, not by trusting a catalogue entry. Where a source turned out to be
unusable, that is recorded too, because knowing what does not exist is part of
understanding the domain.

## In use

### Checkbook L.A., City of Los Angeles Controller

Primary dataset. 6.47M rows across fiscal years 2018 to 2027, 61 columns.

- Download: `https://controllerdata.lacity.org/resource/pggv-e4fn.csv?$where=fiscal_year='2024'&$limit=N`
- Licence: CC BY 4.0
- Sample committed: `data/samples/la_sample.csv`, 20,000 rows from FY2024

Why this one. It is the only public ledger found with a genuine invoice number,
payment method, payment status, purchase order number, **and** a
publisher-assigned `vendor_id`. That last field is the important one: it gives
objective ground truth for scoring vendor entity resolution, so the headline
accuracy number is measured against the publisher's own labels rather than
graded by a model.

Grain is the invoice distribution line, not the payment, so a single invoice can
appear as 200 rows. Aggregation to transaction grain happens in stage 1.

Real vendor name variation observed in a 200,000-row sample: 150 collision
groups covering 319 raw spellings. For example `W W GRAINGER INC`,
`W. W. GRAINGER INC.`, `W. W. GRAINGER, INC.` and `W.W. GRAINGER INC.`

### State of Oklahoma Vendor Payments

Second dataset, used to prove the pipeline is not built around one schema.

- Download: quarterly CSVs from `https://data.ok.gov`
- Licence: CC BY
- Sample committed: `data/samples/ok_sample.csv`, 5,000 rows

Deliberately awkward, which is the point. It has **no invoice number at all**,
only an invoice date, so the adapter declares `has_invoice_number: false` and
blocking drops the signals that depend on it. The files are cp1252 encoded
rather than UTF-8, every column has to be read as a string to stop voucher ids
losing their leading zeros, and about a quarter of rows have the vendor name
replaced with `PROTECTED INFORMATION` by the publisher.

Vendor name chaos is worse than Los Angeles, which makes it the better entity
resolution test: 616 collision groups covering 1,400 spellings, including
trailing asterisks and padded whitespace.

## Investigated and rejected

### India: no public invoice-level vendor payment data exists

This was researched thoroughly because an Indian dataset would have been useful.
The conclusion is a finding in its own right.

**India publishes contract awards, not payments.** Seven central sources were
checked directly:

| Source | Outcome |
|---|---|
| NTPC | Main site behind a bot manager on every path. Separate e-tender host reachable but per-query CAPTCHA, no bulk export. |
| ONGC | Public tender list has no vendor or value. Invoice tracking is an SAP logon screen. Internal portal is employees only. |
| Indian Railways IREPS | Four endpoints named "anonymous search" all return a login form requiring a mobile number and an OTP from the IREPS app. |
| Central Public Procurement Portal | Award of Contract search is CAPTCHA enforced, confirmed by submitting an invalid CAPTCHA and being rejected. Result columns carry no supplier name or award value. The dashboard web service returns 401. |
| PFMS | "Know Your Payment" requires the beneficiary's bank account number to return a single record. These are direct benefit transfers, not vendor invoices. Dashboard host does not respond. |
| data.gov.in | Catalogue API is open and keyless, 354,773 resources. Searching it returns zero supplier or invoice datasets; the vendor hits are street-vendor loan aggregates. File downloads return 403 to any non-browser client. |
| Government e-Marketplace | Contract search is CAPTCHA gated. The open bid API works and reports 5.8M records, but carries no seller name and no award value, and is capped at 10 records per request. |

**What does work**, via the Open Contracting Partnership mirror:

- Himachal Pradesh, CivicDataLab: `https://data.open-contracting.org/en/publication/77`, CC BY 4.0. 4,211 award rows joinable to 1,858 distinct supplier names, with amount, date and buying department. Names are genuinely messy, which suits fuzzy matching.
- Assam, Finance Department: `https://data.open-contracting.org/en/publication/131`, Government Open Data License India. 34,232 rows, of which 10,381 are awarded, but there is no supplier table so vendor names are absent.

**Why this is not in the pipeline as a payment source.** These are contract
awards, not payments. A duplicate found in them is a duplicate award, a
near-identical work order, or a split contract, not a double-paid invoice.
Presenting award data as payment data would misrepresent what the system found,
so it is not used that way.

Contract splitting to stay under an approval threshold is a real procurement
control failure and the engine can detect it, but it is a different claim from
duplicate payment recovery and would need to be labelled as such.

### Other sources checked

- USAspending.gov: an awards and obligations database, not an accounts payable ledger. No invoice number, no invoice date, no payment record.
- SpendLedger: structurally the closest thing to an AP ledger found, but the free export is capped and bulk access is licensed.
- UK central government spend over £25,000: 16,248 separate CSVs with no consolidated download, and 3,391 links point at a web archive.
- Kaggle: every download path returns a CAPTCHA challenge, and the closest-named dataset is generated fake data.
- NYC Checkbook: blocked. Philadelphia: works but frozen at FY2017. Chicago and Austin: work but too few columns to be interesting.

## How findings are described

Everything this system outputs is a **candidate for human review**, never an
assertion of error or wrongdoing. That wording is deliberate and it appears in
the interface, not only in documentation.

Three reasons. Apparent duplicates in public data very often have legitimate
explanations, and the publishers say so themselves: York's dataset notes state
that miscoding errors occur and are usually corrected the following month.
These files contain payments to individuals as well as companies, and Los
Angeles does not redact personal names, so individual names are masked in any
shared output. And no analysis of a published extract can support a claim about
a third party's conduct.

Attribution, as the licences require:

- Contains data from Checkbook L.A. Data, City of Los Angeles Controller, licensed under CC BY 4.0.
- Contains data from State of Oklahoma Vendor Payments, Office of Management and Enterprise Services.

Not affiliated with, or endorsed by, any of the publishing bodies.
