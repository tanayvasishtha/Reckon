# Reckon: technical spec

Read `docs/STANDARDS.md` first. This file is the data contract. Do not change a
schema here without saying so, because other work is being built against it in
parallel.

## Pipeline

```
ledger.csv
   |
   v
[1] aggregate      distribution lines -> transaction grain        deterministic
[2] normalize      vendor + invoice canonicalisation              deterministic
[3] blocking       candidate pairs, hash-bucketed                 deterministic
[4] dismiss        cancelled, line splits, same transaction       deterministic
   |  residual: the judgment cases
   v
[5] adjudicate     one model call per candidate, concurrent       the agent
   |  verdict: dismiss(reason) | escalate(evidence)
   v
[6] review queue   named approver, evidence chain                 human
   |  human decision becomes a rule
   v
[7] packet         recovery letter, journal entry, audit trail    deterministic
```

Stages 1 to 4 and 7 are pure Python with no model calls. Stage 5 is the only
place a model runs.

## Source adapters

Nothing downstream of stage 1 knows where the data came from. Each adapter maps
a source onto the Transaction schema and declares what it cannot provide.
Blocking reads those declared capabilities and runs only the signals the data
supports.

- `adapters/la.py`: invoice numbers, PO numbers, payment status, and a
  publisher-assigned `vendor_id` that serves as entity-resolution ground truth.
- `adapters/oklahoma.py`: no invoice number, only `INVOICE_DT`, so
  `invoice_variant` and `exact_duplicate` are unavailable and blocking falls
  back to `(vendor, invoice_date, amount)` across distinct vouchers. Reads
  cp1252, forces every column to string so voucher ids keep leading zeros, and
  drops rows where the vendor is `PROTECTED INFORMATION`.
- `adapters/generic.py`: a YAML column map for any other CSV, same contract, so
  a NetSuite or SAP export is configuration rather than code.

## Transaction

Produced by stage 1, after aggregating distribution lines onto transaction grain.

`transaction_id, vendor_name_raw, vendor_id, vendor_canonical, invoice_number_raw,
invoice_canonical, invoice_date, payment_date, amount, invoice_amount, po_number,
payment_method, payment_status, department, description, line_count,
distinct_line_amounts`

## Candidate

`candidate_id, transaction_ids[2], signal, amount_at_risk, stage_generated`

`signal` is one of `exact_duplicate`, `fuzzy_vendor`, `invoice_variant`,
`split_payment`, `cross_department`, `overpayment_vs_invoice`.

## Verdict

Stage 5 output, structured.

`candidate_id, verdict, explanation_type, reasoning, evidence[], confidence,
model_used, tokens_in, tokens_out, cache_read`

`verdict` is one of `dismiss`, `escalate`, `errored`.
`explanation_type` is one of `cancelled`, `line_split`, `progress_payment`,
`recurring`, `different_po`, `partial_pair`, or `none`.
`evidence[]` entries are `{field, values}`.

## Review decision

`candidate_id, approver, decision, reason, becomes_rule`

`decision` is `confirmed` or `dismissed`.

## Ground truth

Synthetic only. Never read by stages 1 to 6. Only the evaluation harness opens it.

`true_duplicates[] {case_id, transaction_ids, type, amount}`
`decoys[] {case_id, transaction_ids, reason, why_rules_cannot_tell}`

Every decoy is undecidable by stages 1 to 4 by design. A progress draw whose
only tell is `description = "Progress payment 3 of 5"`. Two purchase orders
differing by one digit. A recurring invoice whose only tell is monthly cadence
across six rows. If a rule can kill a decoy, it is not a decoy and it belongs
in stage 4.

Target set: a real 20,000-row slice with 40 planted duplicates and 60 planted
decoys. Decoys outnumber truths so precision is a real measurement.

## Model routing

Reckon is provider-neutral. Stage 5 talks to any OpenAI-compatible chat
completions endpoint, configured entirely through settings, so nothing in this
codebase is tied to one vendor and a reviewer can run it with whatever key they
already have.

| Setting | Purpose | Default |
|---|---|---|
| `RECKON_API_BASE` | base URL of an OpenAI-compatible endpoint | none, required for live mode |
| `RECKON_API_KEY` | key for that endpoint | none, replay mode if absent |
| `RECKON_MODEL_FAST` | first pass, one call per candidate | set per deployment |
| `RECKON_MODEL_ESCALATE` | used when first-pass confidence is below 0.7 | set per deployment |
| `RECKON_PRICE_FAST_IN` / `_OUT` | dollars per million tokens, for the cost table | 0 |
| `RECKON_PRICE_ESC_IN` / `_OUT` | dollars per million tokens, for the cost table | 0 |

Two tiers, because most candidates are easy and a few are not. The fast model
sees every candidate. The escalation model sees only the ones the fast model is
unsure about. Report the escalation rate in the results, since it is the
cost-quality dial.

Prices are configuration rather than constants, so the cost table stays honest
whichever endpoint is used and nobody has to edit code to correct a number.

### Prompt construction

Put the stable system prompt and rules text first and the per-candidate payload
last. The prefix is identical across thousands of calls, so any endpoint that
supports prefix caching will hit on it. If the endpoint reports cache statistics,
record them and put the hit rate in the results. If the reported cache reads stay
at zero across a run, something in the prefix is varying between calls and the
cost story is broken.

Ask for structured JSON output matching the Verdict schema above. Validate every
response against the schema before use, and mark a response that fails
validation as `errored` rather than guessing at it. Do not set `max_tokens` so
low that a verdict truncates, because a truncated response costs a full retry.

## Incremental runs

The first run reads the whole ledger. Later runs read only what arrived after
the watermark, and skip any candidate pair already adjudicated, using a verdict
cache keyed on the candidate signature. A second run over unchanged data makes
zero model calls.

## What the evaluation must produce

`evaluation/harness.py` writes `RESULTS.md` containing, for both the rules-only
baseline and the full pipeline on identical data:

- precision, recall and F1 on planted duplicates
- false positives broken out by which decoy type caused them
- dollars correctly flagged and dollars wrongly flagged
- token cost, cache hit rate, wall clock
- cold run versus warm daily run

Separately, entity resolution precision and recall for `vendor_canonical`
scored against the publisher's `vendor_id` on real data. That number is not
graded by a model.

A false positive is worse than a miss. Telling a vendor they owe money when
they do not damages a real commercial relationship, so adjudication is tuned
for precision and pushes anything uncertain to human review.
