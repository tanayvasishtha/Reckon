"""Score vendor canonicalisation against the publisher's vendor_id.

Stage 2 is a string transform. This module does not call a model. It
groups real Los Angeles rows by ``canonical_vendor`` and scores those
groups against the publisher-assigned ``vendor_id`` column.

Complexity: O(R) to scan R ledger rows and score the resulting clusters.
Pair counts use n*(n-1)/2 per cluster; there is no all-pairs loop.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from adapters.la import LAAdapter
from core.normalize import canonical_vendor

_PLANTED_PREFIX = "EVAL"


def score_entity_resolution(path: Path) -> tuple[float, float, float]:
    """Return pairwise precision, recall, and F1 for vendor clustering.

    Real rows only: planted ``EVAL*`` transaction ids are skipped, as are
    rows with no publisher ``vendor_id`` and rows whose name does not
    canonicalise. Each remaining row is one mention.
    """
    predicted: dict[str, list[str]] = defaultdict(list)
    gold: dict[str, list[str]] = defaultdict(list)

    for transaction in LAAdapter().iter_transactions(path):
        if transaction.transaction_id.startswith(_PLANTED_PREFIX):
            continue
        vendor_id = transaction.vendor_id
        if vendor_id is None or vendor_id.strip() == "":
            continue
        key = canonical_vendor(transaction.vendor_name_raw)
        if key is None:
            continue
        predicted[key].append(vendor_id)
        gold[vendor_id].append(key)

    true_pairs = 0
    predicted_pairs = 0
    for members in predicted.values():
        predicted_pairs += _pairs(len(members))
        counts: dict[str, int] = defaultdict(int)
        for vendor_id in members:
            counts[vendor_id] += 1
        for size in counts.values():
            true_pairs += _pairs(size)
    gold_pairs = sum(_pairs(len(members)) for members in gold.values())
    precision = _ratio(true_pairs, predicted_pairs)
    recall = _ratio(true_pairs, gold_pairs)
    return precision, recall, _f1(precision, recall)


def _pairs(n: int) -> int:
    return n * (n - 1) // 2


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)
