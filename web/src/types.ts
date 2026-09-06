export type Signal =
  | "exact_duplicate"
  | "fuzzy_vendor"
  | "invoice_variant"
  | "split_payment"
  | "cross_department"
  | "overpayment_vs_invoice";

export type Verdict = "dismiss" | "escalate" | "errored";

export type Decision = "confirmed" | "dismissed";

export type Evidence = {
  field: string;
  values: string[];
};

export type Transaction = {
  transaction_id: string;
  vendor_name_raw: string;
  vendor_id: string | null;
  vendor_canonical: string | null;
  invoice_number_raw: string | null;
  invoice_canonical: string | null;
  invoice_date: string | null;
  payment_date: string | null;
  amount: string;
  invoice_amount: string | null;
  po_number: string | null;
  payment_method: string | null;
  payment_status: string | null;
  department: string | null;
  description: string | null;
  line_count: number;
  distinct_line_amounts: string[];
};

export type QueueItem = {
  candidate_id: string;
  amount_at_risk: string;
  vendor_name: string;
  signal: Signal;
  confidence: number;
  approver: string;
  department: string;
};

export type CandidateDetail = {
  candidate_id: string;
  amount_at_risk: string;
  signal: Signal;
  approver: string;
  department: string;
  left: Transaction;
  right: Transaction;
  reasoning: string;
  evidence: Evidence[];
  model_used: string;
  confidence: number;
  verdict: Verdict;
};

export type Stats = {
  rows_in: number;
  transactions: number;
  candidates: number;
  after_dismiss: number;
  after_adjudicate: number;
  dollars_at_risk: string;
  pending: number;
  confirmed: number;
  dismissed: number;
};

export type ReviewDecision = {
  candidate_id: string;
  approver: string;
  decision: Decision;
  reason: string;
  becomes_rule: boolean;
};
