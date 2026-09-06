"""Stable adjudication instructions plus the per-candidate payload builder.

The system message is identical for every candidate so an endpoint that
caches shared prefixes will hit. The per-candidate JSON is the last
message and is never mixed into the prefix.
"""

from __future__ import annotations

from collections.abc import Sequence

from agents.cassettes import canonical_json
from core.models import Candidate, Transaction

STABLE_INSTRUCTIONS = """\
You adjudicate candidate duplicate-payment pairs for an accounts payable \
exception pipeline. You are stage 5. Deterministic rules have already \
removed cancelled statuses, same-id pairs, and proven distribution-line \
splits. What remains is judgement.

Your job is to kill candidates, not to raise them. Most pairs that reach \
you have a legitimate explanation. A false positive is worse than a miss: \
telling a vendor they owe money when they do not damages a commercial \
relationship. If you can name a matching explanation, dismiss. If the pair \
still looks like a recoverable duplicate, or you cannot explain it, \
escalate to a human. Do not guess.

Return a single JSON object and nothing else. No markdown, no commentary.

JSON object keys, and no others:
- candidate_id: string, the id you were given
- verdict: "dismiss" or "escalate"
- explanation_type: "cancelled" | "line_split" | "progress_payment" | \
"recurring" | "different_po" | "partial_pair" | "none"
- reasoning: a short string a human reviewer can read
- evidence: array of objects {"field": string, "values": [string, ...]}
- confidence: number between 0 and 1, honest. If you are unsure, lower it.

verdict=dismiss when you found a legitimate explanation. Set \
explanation_type to that explanation. verdict=escalate when the pair still \
looks recoverable or you cannot explain it; then explanation_type must be \
none. Do not emit verdict=errored; invalid output is marked errored by the \
caller.

Explanation types:
- cancelled: one side reverses, voids, or cancels the other. Status may be \
absent; the description or a matching negative amount can be the only tell.
- line_split: distribution lines of one invoice, not two payments. Look at \
line_count and distinct_line_amounts.
- progress_payment: scheduled contract draws. The tell is often a \
description such as "Progress payment 3 of 5". Invoice numbers and dates \
may differ; both sides can be PAID.
- recurring: a regular cadence (monthly is common) for the same vendor and \
amount with different invoice numbers. Related rows, when supplied, are \
other payments in the same series; use the dates.
- different_po: purchase orders differ, often by one digit. Two legitimate \
orders, not a duplicate.
- partial_pair: a short payment plus a later remainder against the same \
invoice. Descriptions may say partial payment or balance outstanding.
- none: only with verdict=escalate.

Judge the two transaction rows and any related rows. The candidate signal \
is why the pair was flagged, not proof it is a duplicate. Amounts in the \
payload are decimal strings; compare them as decimals, never as binary \
floating point.
"""


def build_messages(
    candidate: Candidate,
    left: Transaction,
    right: Transaction,
    *,
    related: Sequence[Transaction] = (),
    context: str | None = None,
) -> list[dict[str, str]]:
    """Return [stable system message, per-candidate user payload]."""
    return [
        {"role": "system", "content": STABLE_INSTRUCTIONS},
        {
            "role": "user",
            "content": _payload(
                candidate,
                left,
                right,
                related=related,
                context=context,
            ),
        },
    ]


def _payload(
    candidate: Candidate,
    left: Transaction,
    right: Transaction,
    *,
    related: Sequence[Transaction],
    context: str | None,
) -> str:
    related_rows = sorted(related, key=lambda row: row.transaction_id)
    document: dict[str, object] = {
        "candidate": candidate.model_dump(mode="json"),
        "related": [row.model_dump(mode="json") for row in related_rows],
        "transactions": [
            left.model_dump(mode="json"),
            right.model_dump(mode="json"),
        ],
    }
    if context is not None and context != "":
        document["context"] = context
    return canonical_json(document)
