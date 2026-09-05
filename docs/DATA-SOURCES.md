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

### India: central government publishes awards, but one municipality publishes real payments

A first pass concluded that India publishes no invoice-level vendor payment
data. That was wrong, and the correction matters, so both halves are recorded
here.

**Central government does publish awards rather than payments.** Seven sources
were checked directly:

| Source | Outcome |
|---|---|
| NTPC | Main site behind a bot manager on every path. Separate e-tender host reachable but per-query CAPTCHA, no bulk export. |
| ONGC | Public tender list has no vendor or value. Invoice tracking is an SAP logon screen. Internal portal is employees only. |
| Indian Railways IREPS | Four endpoints named "anonymous search" all return a login form requiring a mobile number and an OTP from the IREPS app. |
| Central Public Procurement Portal | Award of Contract search is CAPTCHA enforced, confirmed by submitting an invalid CAPTCHA and being rejected. Result columns carry no supplier name or award value. The dashboard web service returns 401. |
| PFMS | "Know Your Payment" requires the beneficiary's bank account number to return a single record. These are direct benefit transfers, not vendor invoices. Dashboard host does not respond. |
| data.gov.in | Catalogue API is open and keyless, 354,773 resources. Searching it returns zero supplier or invoice datasets; the vendor hits are street-vendor loan aggregates. File downloads return 403 to any non-browser client. |
| Government e-Marketplace | Contract search is CAPTCHA gated. The open bid API works and reports 5.8M records, but carries no seller name and no award value, and is capped at 10 records per request. |

**But Bengaluru does publish a real accounts payable ledger.** Bruhat Bengaluru
Mahanagara Palike work orders and bill payments, hosted on OpenCity and sourced
from BBMP's own accounts portal, which is live and public at
`https://account.bbmpgov.in/PublicView/?l=1` with its own export.

Roughly 499,000 payment lines across several datasets, covering 2010 to 2025-26,
direct CSV, no login. The richest of them is the Bill Register, 5,789 rows and
31 columns:

`Contractor_Name`, gross, deduction and nett amounts, `Bill Register No` and
date, `Sub Bill Register No` and date, `CBR_No` and date, `RTGS_No` and date,
`Job_Code`, `Work_Order` and date, ward, division and zone, budget head, and
engineer details.

That is an accounts payable ledger with four separate voucher identifiers, which
is more than either American source provides. Contractor name variation is
substantial: 7,715 distinct strings, of which 1,486 have more than one spelling.
Real examples include `KRIDL`, `KRIDL,`, `KRIDL:` and `KRIDl`, and
`K DAMODAR AND CO` against `K Damodar & Co` and `K Damodar & Co.`

Known traps, recorded so nobody rediscovers them the hard way. Most ward files
carry a title banner on the first row, so the header is not row one and blind
skipping breaks it. In the 2010 to 2018 files roughly 15% of rows have the total
and deduction values swapped relative to their headers, so validate with
total minus deduction equals net rather than trusting column order. The bill
reference field is a compound string needing extraction. Work order numbers are
per-ward sequences and are not globally unique.

**Also available**, via the Open Contracting Partnership mirror:

- Himachal Pradesh, CivicDataLab: `https://data.open-contracting.org/en/publication/77`, CC BY 4.0. 4,211 award rows joinable to 1,858 distinct supplier names, with amount, date and buying department. Names are genuinely messy, which suits fuzzy matching.
- Assam, Finance Department: `https://data.open-contracting.org/en/publication/131`, Government Open Data License India. 34,232 rows, of which 10,381 are awarded, but there is no supplier table so vendor names are absent.

**On the award data specifically.** Himachal Pradesh and Assam are contract
awards, not payments. A duplicate found in them is a duplicate award, a
near-identical work order, or a split contract, not a double-paid invoice.
Presenting award data as payment data would misrepresent what the system found,
so it is not used that way. Contract splitting to stay under an approval
threshold is a real control failure the engine can detect, but that is a
different claim and needs its own label.

The Bengaluru data does not carry that caveat. Those are bills paid to
contractors, with amounts, dates and voucher numbers, which is the same shape as
Los Angeles and Oklahoma.

### Other sources checked and rejected



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
