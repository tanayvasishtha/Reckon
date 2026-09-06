import type {
  CandidateDetail,
  Decision,
  QueueItem,
  ReviewDecision,
  Stats,
} from "./types";

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback;
    try {
      const body: unknown = await response.json();
      if (
        typeof body === "object" &&
        body !== null &&
        "detail" in body &&
        typeof body.detail === "string"
      ) {
        detail = body.detail;
      }
    } catch {
      detail = fallback;
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export async function fetchQueue(): Promise<QueueItem[]> {
  const response = await fetch("/queue");
  const payload = await readJson<{ items: QueueItem[] }>(
    response,
    "Could not load the queue.",
  );
  return payload.items;
}

export async function fetchStats(): Promise<Stats> {
  const response = await fetch("/stats");
  return readJson<Stats>(response, "Could not load funnel counts.");
}

export async function fetchCandidate(candidateId: string): Promise<CandidateDetail> {
  const response = await fetch(`/candidate/${encodeURIComponent(candidateId)}`);
  return readJson<CandidateDetail>(response, "Could not load this finding.");
}

export async function postDecision(input: {
  candidateId: string;
  decision: Decision;
  reason: string;
  approver: string;
}): Promise<ReviewDecision> {
  const response = await fetch("/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      candidate_id: input.candidateId,
      decision: input.decision,
      reason: input.reason,
      approver: input.approver,
      becomes_rule: true,
    }),
  });
  return readJson<ReviewDecision>(response, "Could not record the decision.");
}
