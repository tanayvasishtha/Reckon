# Evidence

Every number this project quotes in its README, interface or demo comes from
this file. Each one was checked against the primary document, not a summary of
it. Where a widely repeated figure has no traceable source, it is listed at the
bottom under what not to cite, because the market for this problem is full of
statistics that trace only to the marketing of companies selling the solution.

## How often payments go out twice

**0.09% of transactions, and 0.04% in a second pass.** Amtrak's Office of
Inspector General analysed 100% of 1.9 million transactions worth $14.1 billion
and found duplicate payments at those rates.
*Enhanced Controls Needed To Avoid Duplicate Payments*, OIG-A-2013-018,
20 September 2013.
https://amtrakoig.gov/sites/default/files/reports/final_report_enhanced_controls_needed_to_avoid_duplicate_payments_-_sign....pdf

This is the most defensible primary source available for the rate. It is a
government audit, it states its population, and it tested everything rather
than sampling.

**819 duplicate payments worth £11 million, of which £10.3 million was
recovered.** The same exercise corrected 548 duplicate supplier master records.
Public Sector Fraud Authority and Cabinet Office, *National Fraud Initiative
Report 2022-2024*, 12 March 2025.
https://www.gov.uk/government/publications/national-fraud-initiative-reports/national-fraud-initiative-report-2022-2024-html

**0.8% of disbursements for top performers, 2% for bottom performers.**
APQC Open Standards Benchmarking, reported by APQC's own CFO.
https://www.cfo.com/news/metric-of-the-month-detect-and-prevent-duplicate-or-erroneous-payments/656852/

Read that one carefully before quoting it. The article states explicitly that
these are the percentage of disbursements that are duplicate or erroneous,
**not the share of the money**. It is a count rate. It is routinely misquoted
as a percentage of spend, including by people selling software.

## What the errors add up to

**$186 billion in improper payments across 64 programs in FY2025**, of which
$153 billion were overpayments, with roughly $3 trillion cumulative since 2003.
US Government Accountability Office, GAO-26-108694, 27 April 2026.
https://www.gao.gov/products/gao-26-108694

GAO does not break duplicates out as a category. Do not imply that it does.

**£55 billion to £81 billion lost to fraud and error in 2023-24.**
UK National Audit Office, *Overview of the impact of fraud and error on public
funds*, 18 November 2024.
https://www.nao.org.uk/wp-content/uploads/2024/11/fraud-overview-2023-24.pdf

## India

India's Comptroller and Auditor General has documented this pattern directly.

**Ayushman Bharat PM-JAY**, CAG Report No. 11 of 2023:
- The scheme ID was not unique in 157,176 cases, with 105,138 appearing twice
- Double payment of ₹3.27 lakh to 13 hospitals that submitted claims twice for 35 patients
- 4,761 registrations against seven Aadhaar numbers in Tamil Nadu
- 214,923 claims against patients already recorded as dead

**Rajasthan direct benefit transfer audit**, CAG Report No. 5 of 2022:
9,176 cases of excess, irregular or double payment worth ₹3.44 crore, with
named categories including pension paid twice on the same order and two orders
issued for the same beneficiary.

**Assam**, CAG Report No. 2 of 2024, which is unusually useful because it shows
a before and after. 444 beneficiaries were paid twice in 2017-18. After moving
to a central payment system that fell to 7. The report notes the system was
still not able to stop the irregularity entirely. The same report describes
₹300.98 lakh released against 3,577 registrations created by adding leading
zeros to bank account numbers, which is precisely the kind of near-duplicate
that exact matching cannot see.

## Recovery economics

**Contingency fees of 9.0% to 12.5%**, rising to 14.0% to 17.5% for one
category. Recovery rate under 0.1% of $369 billion paid, at a return of
$2.48 for every $1 spent.
Centers for Medicare and Medicaid Services, *Recovery Auditing in Medicare
Fee-For-Service for FY2015, Report to Congress*.
https://www.cms.gov/Research-Statistics-Data-and-Systems/Monitoring-Programs/Medicare-FFS-Compliance-Programs/Recovery-Audit-Program/Downloads/FY2015-Medicare-FFS-RAC-Report-to-Congress.pdf

This is the only hard, primary-sourced contingency rate that could be found. It
covers healthcare claims rather than general accounts payable, so scope any
claim accordingly.

## Why precision is the hard part

This is the argument the project rests on, so it needs the best evidence.

**Full population testing made auditors less skeptical.** 48% of auditors using
full-population testing acted on a fraud red flag, against 65% of those using
sampling, across 125 practising auditors. Adding visualisation did not fix it.
Li, Brazel and Gold, *An Unintended Consequence of Full Population Testing on
Auditors' Professional Skepticism*, Foundation for Auditing Research working
paper 2021B01, October 2024. Working paper, not yet journal published.
https://foundationforauditingresearch.org/wp-content/uploads/2024/11/2021B01_Li_working-papers_AnUnintendedConsequenceOfFullPopulationTestingOnAuditorsProfessionalSkepticism.pdf

**Published precision on continuous auditing exception detection: 0.73
precision, 0.18 recall, 0.29 F1**, with the authors stating that most of the
exceptions are false alarms at that precision.
Svanberg and others, *Addressing the Exception Prioritization Problem in
Continuous Auditing Systems With Thresholding*, Intelligent Systems in
Accounting, Finance and Management 32(4), 2025. Open access.
https://onlinelibrary.wiley.com/doi/full/10.1002/isaf.70022

**A real false positive rate from the field.** In the Amtrak audit, roughly
$7.5 million was flagged as potential duplicates. Accounts payable reviewed 76
of 1,075 flagged items worth $2.2 million, confirmed about $1.8 million, and
determined the remaining $0.4 million were not duplicates. The report states
plainly that its output contained false positives.

**Academic work on this specific task is thin.** A full-text search of arXiv
returns zero results for the exact phrases "duplicate invoice" and "invoice
deduplication", with control queries confirming the search worked. Issa at the
Rutgers Continuous Auditing lab states that datasets are not labelled and that
prioritisation methodology could not be evaluated. That absence is part of why
this project reports precision and recall against a labelled set at all.

## Fraud context

**Billing schemes account for 21% of occupational fraud cases with a median
loss of $90,000**, from 2,402 cases across 143 countries.
Association of Certified Fraud Examiners, *Occupational Fraud 2026: A Report to
the Nations*.
https://www.acfe.com/-/media/files/acfe/pdfs/rttn/2026/2026-report-to-the-nations.pdf

The frequently quoted "5% of revenue lost to fraud" from the same report is
worded as what certified examiners estimate. It is surveyed opinion rather than
measurement. Label it that way or leave it out.

## What not to cite, and why

These circulate widely in this market. None of them survived a search for a
primary source.

- **"Duplicate payments are 0.1% to 0.5% of disbursements"**, attributed to the Association for Financial Professionals. No primary source found.
- **"One in a thousand invoices is a duplicate"**, attributed to the Institute of Internal Auditors. No primary source found. This is the likely origin of the ubiquitous 0.1% figure.
- **"Up to 1.5% of outgoing cash flow"**, attributed to the Institute of Finance and Management, whose material is membership gated. The figure appears in the UK National Fraud Initiative report, but its footnote credits a company that sells duplicate payment detection software. A government report reprinting a vendor claim does not make it an audit finding.
- **"Recovery audit contingency fees are 20% to 35%"**. Every source found is a recovery audit firm's own marketing. No trade association publishes a rate. Use the CMS figure above instead.
- **"1.29% of invoices are duplicates"**, attributed to a large expense software vendor. Unverified.
- **"Anti money laundering alerts are 95% to 98% false positives"**. The most cited source carries no footnote and the underlying paper is unreachable.

## A domain note

`openbudgetsindia.org` no longer belongs to the organisation of that name. It
currently serves gambling affiliate content. Do not link it.
