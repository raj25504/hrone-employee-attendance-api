# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate guard, join key), `(department, emp_code)` (sorted filtered list), `(department, joined_on)` and `joined_on` (headcount scans). `attendance_logs`: unique `(emp_code, date)` (punch-in guard, monthly, PATCH lookup), `(date desc, emp_code)` (list sort, month scans), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (punch-out lookup). Rejected: copying `department` onto every log to index `(department, date)`; seeded data lacks it and it would go stale.

2. **Punch-in race.** Both requests compute the same attendance date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed; the other raises `DuplicateKeyError`, which becomes 409. There is no check-then-insert.

3. **Ties.** Employees are grouped, then ranked with `$rank` (1, 2, 2, 4), and only then filtered with `rank <= limit`. With `limit=2` and a tie at rank 2, all three rows are returned, as the contract says.

4. **Headcount.** The summary starts from `employees` (joined on or before month end), joins each employee's month of logs, then groups by department counting employees. An employee with no logs gets an empty join and counts with zeros.

5. **100x data.** Stop aggregating raw logs per request: keep a per-(employee, month) rollup updated on write, with `department` copied into it, so analytics read a few rows instead of millions.
