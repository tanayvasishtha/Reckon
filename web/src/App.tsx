import { useEffect, useState } from "react";

import { fetchCandidate, fetchQueue, fetchStats, postDecision } from "./api";
import { FindingPane } from "./FindingPane";
import { Header } from "./Header";
import { QueuePane } from "./QueuePane";
import type { CandidateDetail, Decision, QueueItem, Stats } from "./types";

export function App() {
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [finding, setFinding] = useState<CandidateDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [decideError, setDecideError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [findingLoading, setFindingLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [items, funnel] = await Promise.all([fetchQueue(), fetchStats()]);
        if (cancelled) {
          return;
        }
        setQueue(items);
        setStats(funnel);
        setSelectedId(items[0]?.candidate_id ?? null);
        setLoadError(null);
      } catch (error) {
        if (!cancelled) {
          setLoadError(asMessage(error, "Could not load the queue."));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedId === null) {
      setFinding(null);
      setFindingLoading(false);
      return;
    }
    let cancelled = false;
    setFindingLoading(true);
    void (async () => {
      try {
        const detail = await fetchCandidate(selectedId);
        if (cancelled) {
          return;
        }
        setFinding(detail);
        setDecideError(null);
      } catch (error) {
        if (!cancelled) {
          setFinding(null);
          setDecideError(asMessage(error, "Could not load this finding."));
        }
      } finally {
        if (!cancelled) {
          setFindingLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const selected = queue?.find((item) => item.candidate_id === selectedId) ?? null;

  async function onDecide(decision: Decision, reason: string) {
    if (selectedId === null || finding === null) {
      return;
    }
    const index = queue?.findIndex((item) => item.candidate_id === selectedId) ?? 0;
    setBusy(true);
    setDecideError(null);
    try {
      await postDecision({
        candidateId: selectedId,
        decision,
        reason,
        approver: finding.approver,
      });
      const [items, funnel] = await Promise.all([fetchQueue(), fetchStats()]);
      setQueue(items);
      setStats(funnel);
      if (items.length === 0) {
        setSelectedId(null);
        setFinding(null);
      } else {
        const nextIndex = Math.min(Math.max(index, 0), items.length - 1);
        setSelectedId(items[nextIndex].candidate_id);
      }
    } catch (error) {
      setDecideError(asMessage(error, "Could not record the decision."));
    } finally {
      setBusy(false);
    }
  }

  const emptyRight =
    queue !== null && queue.length === 0
      ? "No candidates waiting for review."
      : "Select a candidate from the queue.";

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <h1 className="sr-only">Review queue</h1>
      <Header
        stats={stats}
        approver={selected?.approver ?? finding?.approver ?? null}
        department={selected?.department ?? finding?.department ?? null}
      />
      <div className="flex min-h-0 flex-1 gap-4 p-4">
        <QueuePane
          items={queue}
          selectedId={selectedId}
          error={loadError}
          onSelect={setSelectedId}
        />
        <FindingPane
          finding={finding}
          loading={findingLoading}
          emptyMessage={loadError ?? emptyRight}
          busy={busy}
          error={decideError}
          onDecide={onDecide}
        />
      </div>
    </div>
  );
}

function asMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message !== "") {
    return error.message;
  }
  return fallback;
}
