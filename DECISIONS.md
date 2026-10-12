# Design Decisions

## 1. Database indexes

Use a unique employee-code index and a unique employee/date attendance index to enforce identity and daily attendance constraints at the database level. Add indexes for frequently filtered fields where query patterns justify them. Verify their effectiveness with MongoDB execution statistics.

## 2. Concurrent punch-ins

Enforce uniqueness for each employee/date pair in MongoDB. Handle duplicate-key errors as conflicts instead of relying only on an application-side existence check, which can race under concurrent requests.

## 3. Leaderboard ties

Use rank semantics that assign equal ranks to equal totals and skip the next rank, such as 1, 2, 2, 4. Apply the rank cutoff after ranking so tied employees at the cutoff remain included. Sort the final results by total late minutes descending and employee code ascending.

## 4. Department summary

Include employees who have joined by the end of the reporting period, even if they have no attendance logs. Exclude employees who join after the period. Calculate averages from underlying sums and counts rather than averaging employee-level averages.

## 5. Scaling

Keep filtering, grouping, joining, ranking, and date-series generation in MongoDB aggregation pipelines where appropriate. Avoid loading entire collections into application memory. Benchmark representative workloads and use execution statistics to identify collection scans and excessive document examination before changing indexes or architecture.

## 6. Concurrency and correctness

Use conditional database updates for attendance corrections so a concurrent change can be detected. Verify race conditions with integration tests against MongoDB before considering the behavior fully tested.
