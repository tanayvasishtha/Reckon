# Audit trail

Finding {candidate_id}. Packet {packet_id} issued {issued_at}.

Each stage the finding passed through is listed below, with what it concluded and when. Clocks default to packet issue time; Candidate, Verdict, and ReviewDecision do not carry per-stage timestamps. The trail ends at the named human who approved the packet.

| When | Stage | Conclusion |
| --- | --- | --- |
{stage_rows}

Trail ends at {approver}, who {decision} this finding.
