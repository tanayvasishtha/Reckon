import type { ReactNode } from "react";

import { ConfidenceBar } from "./ConfidenceBar";
import { Comparison } from "./Comparison";
import { DecisionForm } from "./DecisionForm";
import { formatMoney, formatPercent } from "./format";
import type { CandidateDetail, Decision } from "./types";

type Props = {
  finding: CandidateDetail | null;
  loading: boolean;
  emptyMessage: string;
  busy: boolean;
  error: string | null;
  onDecide: (decision: Decision, reason: string) => Promise<void>;
};

export function FindingPane({
  finding,
  loading,
  emptyMessage,
  busy,
  error,
  onDecide,
}: Props) {
  let body: ReactNode;
  if (finding === null) {
    body = (
      <p className="flex h-full min-h-[12rem] items-center justify-center px-6 text-center text-[15px] leading-[1.5] text-mute">
        {loading ? "Loading the finding." : emptyMessage}
      </p>
    );
  } else {
    body = (
      <div className="mx-auto flex max-w-[52rem] flex-col gap-6 p-6">
        <section>
          <h2 className="text-[13px] leading-[1.5] text-mute">At risk</h2>
          <p className="mt-2 font-mono text-[32px] leading-none tracking-tight tabular-nums">
            {formatMoney(finding.amount_at_risk)}
          </p>
        </section>

        <section>
          <h2 className="mb-2 text-[13px] leading-[1.5] text-mute">Transactions</h2>
          <Comparison left={finding.left} right={finding.right} />
        </section>

        <section>
          <h2 className="text-[13px] leading-[1.5] text-mute">Reasoning</h2>
          <p className="mt-2 max-w-[65ch] text-[15px] leading-[1.5]">{finding.reasoning}</p>
        </section>

        <section>
          <h2 className="text-[13px] leading-[1.5] text-mute">Evidence</h2>
          {finding.evidence.length === 0 ? (
            <p className="mt-2 text-[15px] leading-[1.5] text-mute">No evidence cited.</p>
          ) : (
            <dl className="mt-2 grid gap-3">
              {finding.evidence.map((item) => (
                <div key={item.field}>
                  <dt className="text-[13px] leading-[1.5] text-mute">{item.field}</dt>
                  <dd className="font-mono text-[15px] leading-[1.5] tabular-nums">
                    {item.values.join("  ·  ")}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </section>

        <section>
          <h2 className="text-[13px] leading-[1.5] text-mute">Model</h2>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <p className="font-mono text-[15px] leading-[1.5]">
              {finding.model_used === "" ? "—" : finding.model_used}
            </p>
            <ConfidenceBar value={finding.confidence} />
            <p className="text-[12px] leading-[1.5] text-mute tabular-nums">
              {formatPercent(finding.confidence)}
              {finding.verdict === "errored" ? "  ·  errored, not guessed" : ""}
            </p>
          </div>
        </section>
      </div>
    );
  }

  return (
    <section
      aria-label="Finding"
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[8px] border border-line bg-card"
    >
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
      {finding !== null ? (
        <div className="shrink-0 border-t border-line px-6 py-4">
          <div className="mx-auto max-w-[52rem]">
            <DecisionForm busy={busy} error={error} onDecide={onDecide} />
          </div>
        </div>
      ) : null}
    </section>
  );
}
