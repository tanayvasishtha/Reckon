import { useState, type FormEvent } from "react";

import type { Decision } from "./types";

type Props = {
  busy: boolean;
  error: string | null;
  onDecide: (decision: Decision, reason: string) => Promise<void>;
};

export function DecisionForm({ busy, error, onDecide }: Props) {
  const [open, setOpen] = useState<Decision | null>(null);
  const [reason, setReason] = useState("");

  function toggle(decision: Decision, checked: boolean) {
    if (checked) {
      setOpen(decision);
      setReason("");
      return;
    }
    if (open === decision) {
      setOpen(null);
      setReason("");
    }
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (open === null || reason.trim() === "" || busy) {
      return;
    }
    await onDecide(open, reason.trim());
    setOpen(null);
    setReason("");
  }

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        <label className="cursor-pointer rounded-[8px] bg-accent px-4 py-3 text-[15px] leading-[1.5] text-white hover:bg-accent-hover has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ink">
          <input
            type="checkbox"
            className="sr-only"
            checked={open === "confirmed"}
            onChange={(event) => toggle("confirmed", event.target.checked)}
          />
          Confirm
        </label>
        <label className="cursor-pointer rounded-[8px] border border-line bg-card px-4 py-3 text-[15px] leading-[1.5] text-ink hover:border-ink has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ink">
          <input
            type="checkbox"
            className="sr-only"
            checked={open === "dismissed"}
            onChange={(event) => toggle("dismissed", event.target.checked)}
          />
          Dismiss
        </label>
      </div>
      {open !== null ? (
        <form onSubmit={onSubmit} className="mt-4">
          <label htmlFor="reason" className="block text-[13px] leading-[1.5] text-mute">
            Reason
          </label>
          <textarea
            id="reason"
            name="reason"
            required
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={
              open === "confirmed"
                ? "Why this is a recoverable duplicate."
                : "Why this should leave the queue."
            }
            className="mt-2 min-h-[88px] w-full resize-y rounded-[8px] border border-line bg-card px-3 py-3 text-[15px] leading-[1.5] text-ink"
          />
          <button
            type="submit"
            disabled={busy || reason.trim() === ""}
            className={
              open === "confirmed"
                ? "mt-3 cursor-pointer rounded-[8px] bg-accent px-4 py-3 text-[15px] leading-[1.5] text-white hover:bg-accent-hover disabled:bg-line disabled:text-mute"
                : "mt-3 cursor-pointer rounded-[8px] border border-line bg-card px-4 py-3 text-[15px] leading-[1.5] text-ink hover:border-ink disabled:text-mute"
            }
          >
            {open === "confirmed" ? "Record confirmation" : "Record dismissal"}
          </button>
        </form>
      ) : null}
      {error !== null ? (
        <p className="mt-3 text-[15px] leading-[1.5] text-mute">{error}</p>
      ) : null}
    </div>
  );
}
