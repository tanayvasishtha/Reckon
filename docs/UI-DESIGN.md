# Review queue: design standard

This screen is the human-judgement half of the product. It is what gets
recorded and shown publicly. It should look like a finance tool built by
someone with taste, not a hackathon table.

## Shape

Two panes. A list on the left, one finding open on the right. No navigation
bar, no feature sidebar, no dashboard of widgets. One job: look at a finding,
decide, move to the next.

### Left pane, the queue

Each row carries the amount at risk in large tabular figures, the vendor name,
the signal that raised it, and confidence as a small bar rather than a number.
The selected row takes a solid left border, not a background wash. Rows are at
least 64px tall so the list stays scannable on video.

### Right pane, the finding

In this order, top to bottom.

1. The amount, set large. It is the most important number on the screen.
2. The two transactions side by side in a two-column comparison. Cells that
   differ take a subtle background on the differing cell only. Identical fields
   are muted, so the eye lands on the differences without being told to.
3. The agent's reasoning as one readable paragraph. Never a JSON blob.
4. The evidence it cited, as field names and values in a compact definition list.
5. Which model produced the verdict, and its confidence.
6. Confirm and Dismiss, each opening a small reason field. Neither is styled
   red. Neither choice is an error.

## Visual direction

Restraint. Closer to Linear or Stripe than to Bootstrap. No purple gradient.

- One accent colour, used only for the selected state and the primary button. Everything else is greyscale.
- Off-white background, not pure white. Cards sit on it with a 1px border and no drop shadow.
- Type scale: 32px for the amount, 15px body, 13px labels, 12px metadata. Body line height 1.5.
- Tabular numerals on every figure so columns of money align.
- 8px spacing grid. At least 20px padding inside cards.
- 8px border radius, used consistently.
- Monospace for ids, invoice numbers and amounts. Sans for names and prose.
- No emoji. No icon library. If an icon is genuinely needed, inline one SVG.
- No animation beyond a 120ms colour transition on hover and focus.
- Every interactive element has a visible focus ring.
- The empty state is one centred sentence in muted text, never a blank pane.

## Two things visible without clicking or scrolling

**The approver's name**, derived from the transaction's department, presented as
a person this goes to rather than a shared queue.

**A standing line stating these are candidates for human review, not confirmed
errors.** Put it in the header where it cannot be missed. Style it as
information, not as a warning banner.

## Legibility

Recorded at 1080p and posted publicly. Nothing smaller than 12px. Contrast at
least 4.5:1 on all text including muted labels. The amount and the vendor must
survive social video compression.

## Privacy

Mask personal names in the display. Public ledgers contain payments to
individuals, and one of the sources does not redact them.

## Out of scope

No login, no authentication, no account system, no settings page. The
organisers said explicitly not to spend time on auth unless it is the core
idea. It is not.

## Dependencies

Tailwind is fine. Do not add a component library, an icon package, a chart
library, or a state management library.
