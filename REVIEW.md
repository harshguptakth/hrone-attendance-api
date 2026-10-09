# REVIEW.md

**Review status: incomplete — original starter `app/main.py` was not included in the uploaded files.**

The assignment asks for concrete, reproducible findings against the colleague's starter implementation. It would be misleading to invent defects without seeing that source file. Upload the original `app/main.py` and this section can be completed accurately.

During implementation, the following risk areas were explicitly handled in the replacement implementation:

| Area | Risk / test case | Handling in this implementation |
|---|---|---|
| Concurrent punch-in | Two simultaneous requests for one employee/date | Unique compound index on `(emp_code, date)`; duplicate-key loser returns 409. |
| Concurrent punch-out | Two requests close the same open record | Conditional atomic update requires `punch_out: null`; loser returns 409. |
| Concurrent corrections | Two PATCH requests race | Compare-and-swap filter includes the prior record state/history; conflict returns 409 rather than losing an audit entry. |
| Time precision | Millisecond timestamps | Convert epoch milliseconds to UTC datetimes truncated to whole seconds. |
| Legacy records | Missing `history` or derived defaults | API output defaults history to `[]`, late/overtime to `0`, and half-day to `false`. |
| Hidden-data analytics | Weekend records, joiners, missing logs and ties | Dedicated aggregation pipelines and indexes are used; these still require testing against the official starter and MongoDB 7 dataset. |

These are implementation safeguards, not claims about defects in the missing starter. Do not submit this review as final until the original starter is reviewed and the known defects are recorded.
