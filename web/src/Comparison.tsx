import { displayValue, formatMoney } from "./format";
import type { Transaction } from "./types";

type Field = {
  key: keyof Transaction;
  label: string;
  money?: boolean;
};

const FIELDS: Field[] = [
  { key: "vendor_name_raw", label: "Vendor" },
  { key: "amount", label: "Amount", money: true },
  { key: "invoice_number_raw", label: "Invoice" },
  { key: "invoice_date", label: "Invoice date" },
  { key: "payment_date", label: "Paid" },
  { key: "invoice_amount", label: "Invoice amount", money: true },
  { key: "po_number", label: "PO" },
  { key: "payment_method", label: "Method" },
  { key: "payment_status", label: "Status" },
  { key: "department", label: "Department" },
  { key: "description", label: "Description" },
  { key: "transaction_id", label: "Id" },
];

const MONO_KEYS = new Set<keyof Transaction>([
  "amount",
  "invoice_number_raw",
  "invoice_canonical",
  "invoice_date",
  "payment_date",
  "invoice_amount",
  "po_number",
  "transaction_id",
]);

type Props = {
  left: Transaction;
  right: Transaction;
};

export function Comparison({ left, right }: Props) {
  return (
    <div className="overflow-hidden rounded-[8px] border border-line">
      <div className="grid grid-cols-[144px_1fr_1fr] border-b border-line bg-canvas">
        <div />
        <p className="truncate px-4 py-2 font-mono text-[12px] leading-[1.5] text-mute" title={left.transaction_id}>
          {left.transaction_id}
        </p>
        <p className="truncate px-4 py-2 font-mono text-[12px] leading-[1.5] text-mute" title={right.transaction_id}>
          {right.transaction_id}
        </p>
      </div>
      {FIELDS.map((field) => {
        const leftText = formatField(left, field);
        const rightText = formatField(right, field);
        const differs = leftText !== rightText;
        return (
          <div
            key={field.key}
            className="grid grid-cols-[144px_1fr_1fr] border-b border-line last:border-b-0"
          >
            <p className="px-4 py-2 text-[13px] leading-[1.5] text-mute">{field.label}</p>
            <ValueCell value={leftText} differs={differs} mono={MONO_KEYS.has(field.key)} />
            <ValueCell value={rightText} differs={differs} mono={MONO_KEYS.has(field.key)} />
          </div>
        );
      })}
    </div>
  );
}

function formatField(transaction: Transaction, field: Field): string {
  const raw = transaction[field.key];
  if (field.money) {
    if (raw == null || raw === "") {
      return "—";
    }
    return formatMoney(String(raw));
  }
  if (Array.isArray(raw)) {
    return raw.length === 0 ? "—" : raw.map(String).join(", ");
  }
  return displayValue(raw);
}

function ValueCell({
  value,
  differs,
  mono,
}: {
  value: string;
  differs: boolean;
  mono: boolean;
}) {
  const font = mono ? "font-mono tabular-nums" : "";
  const tone = differs ? "text-ink bg-diff" : "text-mute";
  return (
    <p className={`px-4 py-2 text-[15px] leading-[1.5] ${font} ${tone}`}>{value}</p>
  );
}
