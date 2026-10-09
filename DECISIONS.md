# DECISIONS.md

1. **Indexes.** A unique `emp_code` index protects employee identity; a unique `(emp_code, date)` index prevents duplicate attendance records. Compound date/employee and employee/date indexes support list and monthly filters, while date/status/late indexes support leaderboard queries. The department/join-date index supports headcount and trend lookups. I would avoid adding indexes without checking query plans because each index adds write and storage cost.

2. **Punch-in race.** Both requests may read the employee, but the unique attendance key is the final guard. MongoDB accepts one insert; the other gets a duplicate-key error and the API returns 409.

3. **Ties.** Competition ranking gives tied employees the same rank and skips the next rank. The limit is applied after ranking, so every employee tied at a rank within the limit is returned; `emp_code` makes their order deterministic.

4. **Headcount.** The department summary starts from employees who joined by month-end and left-joins logs, preserving employees with no logs. Employees joining after month-end are excluded.

5. **100x data.** I would validate real explain plans and workload metrics, then consider pre-aggregated daily summaries and carefully bounded pagination. I would not add caching until freshness requirements were clear.
