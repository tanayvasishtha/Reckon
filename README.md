# Reckon

An accounts payable exception agent. A deterministic sweep finds every payment
that looks like a duplicate or an overpayment. On a real ledger that produces
thousands of alerts. Reckon works only that residual, and its job is to kill
candidates: this one was cancelled, that one is 200 legitimate distribution
lines on a single invoice, this pair is two scheduled construction draws.
What survives goes to a named approver with the evidence attached. Nothing
posts automatically.

Built for Syndicate by Maximor, Track 2: Autonomous Office of the CFO.
Built with AO (Agent Orchestrator).

Runs on real published municipal payment data. Every number reported is
measured against ground truth.

Work in progress during the hackathon window.
