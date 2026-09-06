import type { ReactNode } from "react";

import { ConfidenceBar } from "./ConfidenceBar";
import { formatMoney, formatPercent, formatSignal } from "./format";
import type { QueueItem } from "./types";

type Props = {
  items: QueueItem[] | null;
  selectedId: string | null;
  error: string | null;
  onSelect: (candidateId: string) => void;
};

export function QueuePane({ items, selectedId, error, onSelect }: Props) {
  let body: ReactNode;
  if (error !== null && items === null) {
    body = <Empty message={error} />;
  } else if (items === null) {
    body = <Empty message="Loading the queue." />;
  } else if (items.length === 0) {
    body = <Empty message="No candidates waiting for review." />;
  } else {
    body = (
      <div role="radiogroup" aria-label="Candidates">
        {items.map((item) => {
          const selected = item.candidate_id === selectedId;
          return (
            <label
              key={item.candidate_id}
              className={[
                "flex min-h-16 cursor-pointer flex-col border-b border-b-line px-5 py-3 last:border-b-0",
                "border-l-4",
                selected ? "border-l-accent" : "border-l-transparent hover:border-l-line",
                "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-[-2px] has-[:focus-visible]:outline-ink",
              ].join(" ")}
            >
              <input
                type="radio"
                name="queue"
                value={item.candidate_id}
                checked={selected}
                onChange={() => onSelect(item.candidate_id)}
                className="sr-only"
              />
              <span className="font-mono text-[24px] leading-none tracking-tight tabular-nums">
                {formatMoney(item.amount_at_risk)}
              </span>
              <span className="mt-2 truncate text-[15px] leading-[1.5]" title={item.vendor_name}>
                {item.vendor_name}
              </span>
              <span className="mt-2 flex items-center justify-between gap-3">
                <span className="min-w-0 truncate text-[12px] leading-[1.5] text-mute">
                  {formatSignal(item.signal)}
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <ConfidenceBar value={item.confidence} />
                  <span className="text-[12px] leading-[1.5] text-mute tabular-nums">
                    {formatPercent(item.confidence)}
                  </span>
                </span>
              </span>
            </label>
          );
        })}
      </div>
    );
  }

  return (
    <section
      aria-label="Queue"
      className="flex min-h-0 w-[360px] shrink-0 flex-col overflow-hidden rounded-[8px] border border-line bg-card"
    >
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
      <p className="shrink-0 border-t border-line px-5 py-2 text-[12px] leading-[1.5] text-mute">
        j / ↓ next · k / ↑ previous · c confirm · d dismiss · esc close
      </p>
    </section>
  );
}

function Empty({ message }: { message: string }) {
  return (
    <p className="flex min-h-[12rem] items-center justify-center px-6 text-center text-[15px] leading-[1.5] text-mute">
      {message}
    </p>
  );
}
