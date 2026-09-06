import type { ReactNode, Ref } from "react";

import { ConfidenceBar } from "./ConfidenceBar";
import { Comparison } from "./Comparison";
import { DecisionForm, type DecisionFormHandle } from "./DecisionForm";
import { formatMoney, formatPercent, formatVerdict } from "./format";
import type { CandidateDetail, Decision } from "./types";

type Props = {
  finding: CandidateDetail | null;
  loading: boolean;
  emptyMessage: string;
  busy: boolean;
  error: string | null;
  onDecide: (decision: Decision, reason: string) => Promise<void>;
  formRef?: Ref<DecisionFormHandle | null>;
};

export function FindingPane({
  finding,
  loading,
  emptyMessage,
  busy,
  error,
  onDecide,
  formRef,
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
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-6 overflow-y-auto p-6 min-[1100px]:grid-cols-2 min-[1100px]:grid-rows-[minmax(0,1fr)] min-[1100px]:overflow-hidden">
        <div className="flex min-h-0 flex-col gap-4 min-[1100px]:overflow-y-auto">
          <section>
            <h2 className="text-[13px] leading-[1.5] text-mute">At risk</h2>
            <p className="mt-2 font-mono text-[32px] leading-none tracking-tight tabular-nums">
              {formatMoney(finding.amount_at_risk)}
            </p>
          </section>

          <section>
            <h2 className="text-[13px] leading-[1.5] text-mute">Verdict</h2>
            <p className="mt-2 text-[15px] leading-[1.5]">{formatVerdict(finding.verdict)}</p>
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
            <h2 className="text-[13px] leading-[1.5] text-mute">Confidence</h2>
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <ConfidenceBar value={finding.confidence} />
              <p className="text-[12px] leading-[1.5] text-mute tabular-nums">
                {formatPercent(finding.confidence)}
              </p>
            </div>
          </section>

          <section>
            <h2 className="text-[13px] leading-[1.5] text-mute">Model</h2>
            <p className="mt-2 font-mono text-[15px] leading-[1.5]">
              {finding.model_used === "" ? "—" : finding.model_used}
            </p>
          </section>
        </div>

        <div className="min-h-0 min-[1100px]:overflow-y-auto">
          <h2 className="mb-2 text-[13px] leading-[1.5] text-mute">Transactions</h2>
          <Comparison left={finding.left} right={finding.right} />
        </div>
      </div>
    );
  }

  return (
    <section
      aria-label="Finding"
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[8px] border border-line bg-card"
    >
      {finding === null ? (
        <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
      ) : (
        body
      )}
      {finding !== null ? (
        <div className="shrink-0 border-t border-line px-6 py-4">
          <DecisionForm
            key={finding.candidate_id}
            formRef={formRef}
            busy={busy}
            error={error}
            onDecide={onDecide}
          />
        </div>
      ) : null}
    </section>
  );
}
