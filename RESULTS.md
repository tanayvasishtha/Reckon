# Results

Numbers below are measured on the labelled eval set. They are not adjusted.

## Entity resolution

Vendor canonicalisation scored against the publisher `vendor_id` on real LA rows.

| metric | value |
| --- | --- |
| precision | 0.967358 |
| recall | 1.000000 |
| f1 | 0.983408 |

## Duplicate detection

Precision, recall, and F1 are computed on planted duplicates. A finding is `verdict=escalate`.

| metric | rules-only | full pipeline |
| --- | --- | --- |
| precision | 0.400000 | 0.000000 |
| recall | 1.000000 | 0.000000 |
| f1 | 0.571429 | 0.000000 |
| dollars_correct | 64134.05 | 0 |
| dollars_wrong | 76093.78 | 0 |

### False positives by decoy reason

| reason | rules-only | full pipeline |
| --- | --- | --- |
| cancelled | 10 | 0 |
| different_po | 10 | 0 |
| line_split | 10 | 0 |
| partial_pair | 10 | 0 |
| progress_payment | 10 | 0 |
| recurring | 10 | 0 |

## Cost and runtime

| metric | rules-only | full pipeline cold | full pipeline warm |
| --- | --- | --- | --- |
| wall_clock_s | 3.235286 | 3.835378 | 3.606705 |
| tokens_in | 0 | 0 | 0 |
| tokens_out | 0 | 0 | 0 |
| cache_read | 0 | 0 | 0 |
| cache_hit_rate | 0.000000 | 0.000000 | 0.000000 |
| token_cost | 0 | 0 | 0 |
| model_escalation_rate | 0.000000 | 0.000000 | 0.000000 |
| escalate | 1731 | 0 | 0 |
| dismiss | 11 | 11 | 11 |
| errored | 0 | 1731 | 1731 |
