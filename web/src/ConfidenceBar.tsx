import { formatPercent } from "./format";

type Props = {
  value: number;
};

export function ConfidenceBar({ value }: Props) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <span
      className="inline-flex h-1 w-16 items-center rounded-[8px] bg-line"
      aria-label={`Confidence ${formatPercent(value)}`}
    >
      <span
        className="block h-1 rounded-[8px] bg-ink"
        style={{ width: `${pct}%` }}
      />
    </span>
  );
}
