"""Canonicalise vendor names and invoice numbers for entity resolution.

Stage 2 of the pipeline. Both helpers are pure string transforms: no I/O,
no model calls, and no dependence on wall-clock time.

Complexity: O(L) per call for an identifier of length L, O(R * L) to
normalise a ledger of R rows. Work is a single left-to-right scan of each
string. There is no pairwise comparison at this stage.
"""

from __future__ import annotations

_CORPORATE_SUFFIXES: frozenset[str] = frozenset(
    {
        "INC",
        "LLC",
        "LTD",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LP",
        "LLP",
        "THE",
    }
)
_AND_TOKEN = "AND"
_THE_TOKEN = "THE"
_INVOICE_PREFIX = "INV"


def canonical_vendor(name: str | None) -> str | None:
    """Return a stable vendor key, or None when the name is missing or empty."""
    if name is None:
        return None
    tokens = [token for token in _alnum_tokens(name) if token != _AND_TOKEN]
    while tokens and tokens[0] == _THE_TOKEN:
        tokens.pop(0)
    while tokens and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    if not tokens:
        return None
    return "".join(tokens)


def canonical_invoice(number: str | None) -> str | None:
    """Return a stable invoice key, or None when the number is missing or empty."""
    if number is None:
        return None
    text = number.strip().upper()
    if not text:
        return None
    if text.startswith(_INVOICE_PREFIX):
        rest = text[len(_INVOICE_PREFIX) :]
        if not rest or not rest[0].isalpha():
            text = rest
    compact = "".join(char for char in text if char.isalnum())
    if not compact:
        return None
    stripped = compact.lstrip("0")
    return stripped if stripped else "0"


def _alnum_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char.upper())
        elif current:
            tokens.append("".join(current))
            current.clear()
    if current:
        tokens.append("".join(current))
    return tokens
