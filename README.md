# Reckon

An accounts payable exception agent.

A deterministic sweep finds every payment in a ledger that looks like it was
made twice. On a real ledger that produces thousands of alerts, which is why
nobody reads them. Reckon works only that residual, and its job is to kill
candidates rather than raise them: this one was cancelled, that one is two
hundred legitimate distribution lines on a single invoice, this pair is two
scheduled construction draws. What survives goes to a named approver with the
evidence attached. Nothing posts automatically.

Built for Syndicate by Maximor, Track 2, Autonomous Office of the CFO.
Built with AO.

Runs on real published payment ledgers. Every number below was produced by a
script in this repository, and every claim about the problem traces to a
primary source listed in `docs/EVIDENCE.md`.

## Quickstart

```
git clone https://github.com/tanayvasishtha/Reckon
cd Reckon
make demo-fast
```

The review queue is the committed fixture in `api/fixture.json`.

```
uv run uvicorn api.main:app --host 127.0.0.1 --port 8000
npm --prefix web install && npm --prefix web run dev
```

Open http://127.0.0.1:5173.

## The problem is precision, not detection

Testing every transaction instead of a sample was solved a decade ago.
Supervizor ships 350 prebuilt controls. AppZen, Oversight and MindBridge all
advertise analysing 100% of transactions. That is not the hard part and it has
not been for years.

The hard part is that full-population testing produces an exception queue
nobody works. Published precision for continuous auditing exception detection
is 0.73 with recall of 0.18, and the authors state plainly that most exceptions
at that precision are false alarms.<sup>1</sup> Worse, there is evidence the
flood actively harms judgement: auditors using full-population testing acted on
a fraud red flag 48% of the time, against 65% for auditors using
sampling.<sup>2</sup>

None of the incumbents publish a false positive rate.

Meanwhile the money is real and it is measured. Amtrak's Inspector General
analysed 100% of 1.9 million transactions worth $14.1 billion and found
duplicate payments at 0.09% of transactions.<sup>3</sup> The UK National Fraud
Initiative identified 819 duplicate payments worth £11 million in two years and
recovered £10.3 million of it.<sup>4</sup> India's Comptroller and Auditor
General published an audit paragraph in 2025 titled, in full, "Double payment
to contractors for an item of work due to incorrect estimates and
measurements", covering ₹0.95 crore across five contracts; the government
accepted the finding.<sup>5</sup>

So the gap is not finding candidates. It is returning a list short enough that
a person actually reviews it.

## How it works

Seven stages. Only one of them calls a model.

| Stage | What it does | Model? |
|---|---|---|
| 1. Aggregate | Collapse invoice distribution lines onto transaction grain | no |
| 2. Normalise | Canonicalise vendor names and invoice numbers | no |
| 3. Block | Generate candidate pairs, hash-bucketed | no |
| 4. Dismiss | Remove what a rule can prove innocent | no |
| 5. Adjudicate | Weigh the judgement cases | **yes** |
| 6. Review | Escalate to a named approver with evidence | human |
| 7. Package | Recovery letter, journal entry, audit trail | no |

Stages 1 to 4 clear the volume in under thirty seconds. Stage 5 sees only
what is left.

**Blocking is why it finishes.** Comparing every pair across 700,000
transactions is 2.4 x 10^11 comparisons. Transactions are hashed into buckets
by a composite key and compared only within a bucket, with a hard cap so one
high-volume vendor cannot blow up the run. There is a test that generates
200,000 rows and asserts it completes in under thirty seconds.

**Adjudication is two-tier and concurrent.** A fast model sees every candidate;
a stronger one sees only those where the first reports confidence below the
threshold. Calls run through `asyncio.gather` behind a semaphore, because three
hundred serial calls would take ten minutes and could not be demonstrated. A
response that fails schema validation becomes an `errored` verdict that still
reaches the human queue. It is never guessed at.

**It is provider-neutral.** Stage 5 targets any OpenAI-compatible endpoint,
configured entirely through `RECKON_API_BASE`, `RECKON_API_KEY`,
`RECKON_MODEL_FAST` and `RECKON_MODEL_ESCALATE`. With no key set it runs
against recorded cassettes, offline and free, which is the default path for
anyone cloning this repository.

## Architecture

```
ledger.csv
    |
    v
[1] Aggregate     collapse invoice distribution lines onto transaction grain    no model
[2] Normalise     canonicalise vendor names and invoice numbers                 no model
[3] Block         generate candidate pairs, hash-bucketed                       no model
[4] Dismiss       remove what a rule can prove innocent                         no model
    |
    v
[5] Adjudicate    weigh the judgement cases                                     MODEL
    |
    v
[6] Review        escalate to a named approver with evidence                    human
    |
    v
[7] Package       recovery letter, journal entry, audit trail                   no model
```

## Results

### Entity resolution, scored against external labels

| metric | value |
|---|---|
| precision | 0.967 |
| recall | 1.000 |
| F1 | 0.983 |

The Los Angeles ledger carries a publisher-assigned `vendor_id`. Canonical
vendor grouping is scored against it, so this number is measured against
someone else's labels rather than graded by a model.

### Baseline against Reckon, identical data

Both pipelines share stages 1 to 4. The only difference is the adjudication
step, so the difference below is attributable to it and nothing else.

| metric | rules only | Reckon |
|---|---|---|
| precision | 0.400 | **0.718** |
| recall | **1.000** | 0.700 |
| F1 | 0.571 | **0.709** |
| dollars correctly flagged | $64,134 | $52,245 |
| dollars wrongly flagged | $76,093 | **$0** |

The rules engine flagged more money wrongly than it flagged correctly.

### Which explanations the rules could not see

Sixty decoys were planted, ten of each type, each one designed so no
deterministic rule can resolve it.

| decoy type | rules only | Reckon |
|---|---|---|
| cancelled | 10 | **0** |
| line_split | 10 | **0** |
| progress_payment | 10 | **0** |
| partial_pair | 10 | 1 |
| different_po | 10 | 4 |
| recurring | 10 | 6 |
| **total** | **60** | **11** |

The rules engine fell for every one. Reckon fell for eleven, and the ones it
still gets wrong are the two hardest categories: recurring billing at a
constant amount, and two genuinely different purchase orders that happen to
match.

### The trade we are making

Recall dropped from 1.000 to 0.700. Reckon misses three real duplicates in ten.

That is what the precision costs, and it is a deliberate setting rather than an
accident. A tool that flags everything scores perfect recall and is the reason
nobody works these queues. It is still a real cost, and burying it in a footnote
while quoting the precision gain would be dishonest.

### Reliability

3 of 1,731 candidates return `errored` rather than a verdict, a rate of 0.17%.
An errored candidate reaches the review queue flagged for attention. It is
never silently dropped and never guessed at.

Getting there took finding a real defect. An earlier run errored on 152
candidates, and the obvious reading was that the model had failed on them. It
had not. Validating all 1,731 recorded responses against the schema showed every
one of those 152 carried a correct verdict, a correct explanation type and sound
reasoning, and was rejected because a single field came back shaped as a nested
list instead of a flat one. Strict validation was doing exactly what it was
built to do: refusing to guess at a response it could not parse. The fix was a
coercion on that one field.

Two things made that diagnosis possible. Every model call is recorded, so the
failures could be replayed and inspected rather than reasoned about. And
failures are surfaced as a distinct verdict rather than dropped, so they were
countable in the first place.

## Verify this yourself

```
uv run python -m evaluation.harness
```

Writes `RESULTS.md` from the labelled eval set. Replay, no key.

- Entity resolution precision, recall, F1: Entity resolution table.
- Precision, recall, F1, and dollars: Duplicate detection table.
- Decoy counts: False positives by decoy reason.
- Errored rate: Cost and runtime. Full pipeline `errored` over rules-only `escalate` (3 of 1,731).

```
make demo
```

Funnel on the committed sample. Same replay path.

```
make test
```

Includes the 200,000-row blocking budget and two-tier adjudication.

## What broke

Five failures, named, with what fixed each one.

**The chat interface could not launch an agent on Windows.** Every worker died
about one second after spawn with no error. The cause was that the agent CLI
resolves to a `.cmd` shim, and the launcher cannot spawn a `.cmd` child.
Switching to terminal mode, which goes through ConPTY, fixed it. That cost
about forty minutes and two dead sessions before the pattern was clear.

**Three concurrent workers deadlock; two never do.** Three workers spawned
together ran for 7.6 hours, consuming real CPU the whole time, and produced
zero files. The same tasks respawned two at a time completed in fifteen minutes
each. Every unit after that was built two at a time. This was the single most
expensive failure of the build and it cost most of one night.

**Workers sometimes finish and stall before committing.** The baseline pipeline
was complete and passing 235 tests in its worktree, but the session never made
the commit. Its work was committed from the worktree by hand. Worth knowing if
you supervise agent fleets: "no commit" does not mean "no work".

**The evaluation scored everything as errored because of a model name.** The
replay settings defaulted the model to a placeholder string, but cassettes were
recorded under the real model name, and the model name is part of the payload
hash. Every lookup missed. Nothing was wrong with the pipeline; the key simply
did not match.

**The model returns empty content when the token cap is low.** The endpoint
used here is a reasoning model that emits its reasoning as completion tokens.
With a low `max_tokens` the entire budget goes to reasoning and the answer
comes back as an empty string with `finish_reason: stop`. Nothing is raised and
nothing is logged, so the only symptom is a verdict that fails validation later.

**152 verdicts were thrown away over one field's shape.** The model returned
`evidence[].values` as a nested list on some responses where the schema expected
a flat one, so strict validation rejected the whole response even though the
judgement inside it was correct. The error rate looked like a model reliability
problem and was a serialisation mismatch. Coercing that one field took the rate
from 8.8% to 0.17%.

## How it was built

Every line of the pipeline was written by AO workers. This session planned the
work, wrote the briefs, reviewed each diff, ran the checks and merged.

| | |
|---|---|
| AO sessions | 31 |
| Merged units | 20 |
| Commits | 52 |
| Python modules | 32 |
| Tests | 330 |

Work was decomposed by file boundary rather than by feature, so parallel
workers never collided in a worktree merge. The schema in `core/models.py`
landed second and every later worker built against it; across six parallel
workers there was not one interface conflict.

Each brief named the files a worker could touch, the files it could not, the
contract it was coding against, and the exact command that had to pass before
it could claim to be done. Nothing was merged on a worker's own report that it
had finished. Every unit was verified here by running ruff, mypy strict and the
full test suite against its branch before the merge.

`docs/STANDARDS.md` was committed to the repository early and referenced by
every brief, because workers run in isolated worktrees and cannot see a
document that lives outside them.

## Run it

```
git clone https://github.com/tanayvasishtha/Reckon
cd Reckon
make demo
```

No API key required. With none set the pipeline runs against recorded
cassettes: full output, deterministic, offline.

```
make demo-fast                              a smaller slice, under 30 seconds
make demo SOURCE=oklahoma                   a different ledger, different schema
make demo SOURCE=mine.csv MAP=mine.yaml     your own export
```

That last one is the point of the adapter layer. Each adapter declares what its
source cannot provide, and blocking only runs the checks the data supports. The
Oklahoma ledger has no invoice number at all, so those signals switch off rather
than producing nonsense.

## Data and caveats

Sources, licences and the ones that turned out to be unusable are documented in
`docs/DATA-SOURCES.md`. Summary:

- Checkbook L.A., City of Los Angeles Controller, CC BY 4.0
- State of Oklahoma Vendor Payments, Office of Management and Enterprise Services

## Credits

- Data: Checkbook L.A., City of Los Angeles Controller, licensed CC BY 4.0.
- Data: State of Oklahoma Vendor Payments, Office of Management and Enterprise Services, licensed CC BY.
- Inference for the recorded runs: TensorMux, `api.tensormux.com`, running `glm-4-7-flash`.
- Built with AO, Agent Orchestrator, `aoagents.dev`.
- Python libraries: pydantic, pandas, httpx, PyYAML, FastAPI, pytest, ruff, mypy, uv.
- Frontend: React, Vite, Tailwind CSS, TypeScript.

**Everything this system outputs is a candidate for human review.** It is not an
assertion that an error or any wrongdoing occurred. Apparent duplicates in
public ledgers frequently have legitimate explanations, and the publishers say
so themselves. Payments to individuals appear in these files and one source does
not redact them, so personal names are masked in any shared output.

Not affiliated with, or endorsed by, any publishing body.

## Track

Autonomous Office of the CFO. Reckon automates the accounts payable payment
review that runs before each payment run and the recovery sweep that runs
monthly. The track asks for exception handling and human review, and here they
are the product itself rather than a feature added at the end. The reason this
workflow is still manual is that existing tools return more exceptions than
anyone can work through.

---

<sup>1</sup> Svanberg et al., *Addressing the Exception Prioritization Problem
in Continuous Auditing Systems With Thresholding*, Intelligent Systems in
Accounting, Finance and Management 32(4), 2025.
<sup>2</sup> Li, Brazel and Gold, *An Unintended Consequence of Full Population
Testing on Auditors' Professional Skepticism*, Foundation for Auditing Research
working paper 2021B01, 2024.
<sup>3</sup> Amtrak Office of Inspector General, *Enhanced Controls Needed To
Avoid Duplicate Payments*, OIG-A-2013-018, 2013.
<sup>4</sup> Public Sector Fraud Authority, *National Fraud Initiative Report
2022-2024*, 2025.
<sup>5</sup> Comptroller and Auditor General of India, Karnataka Report No. 3
of 2025, paragraph 3.5.

Full citations with URLs in `docs/EVIDENCE.md`.
