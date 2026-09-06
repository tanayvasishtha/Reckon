import { formatCount, formatMoney } from "./format";
import type { Stats } from "./types";

type Props = {
  stats: Stats | null;
  approver: string | null;
  department: string | null;
};

export function Header({ stats, approver, department }: Props) {
  const funnel = stats === null ? null : funnelLine(stats);

  return (
    <header aria-label="Summary" className="header-bar">
      <div className="header-top">
        <p className="text-[13px] leading-[1.5] text-mute">Reckon</p>
        {funnel !== null ? (
          <p className="header-funnel text-right font-mono text-[12px] leading-[1.5] text-mute tabular-nums">
            {funnel}
          </p>
        ) : null}
      </div>
      <p className="mt-2 max-w-[52rem] text-[15px] leading-[1.5] text-ink">
        These are candidates for human review, not confirmed errors.
      </p>
      {approver !== null ? (
        <p className="mt-2 text-[15px] leading-[1.5] text-ink">
          Goes to {approver}
          {department !== null ? (
            <span className="text-[12px] leading-[1.5] text-mute">
              {" "}
              · {department}
            </span>
          ) : null}
        </p>
      ) : null}
    </header>
  );
}

function funnelLine(stats: Stats): string {
  const parts = [
    `${formatCount(stats.rows_in)} rows`,
    `${formatCount(stats.transactions)} payments`,
    `${formatCount(stats.candidates)} flagged`,
    `${formatCount(stats.after_dismiss)} after rules`,
    `${formatCount(stats.pending)} for review`,
    `${formatMoney(stats.dollars_at_risk)} at risk`,
  ];
  if (stats.confirmed > 0) {
    parts.push(`${formatCount(stats.confirmed)} confirmed`);
  }
  if (stats.dismissed > 0) {
    parts.push(`${formatCount(stats.dismissed)} dismissed`);
  }
  return parts.join("  ·  ");
}
