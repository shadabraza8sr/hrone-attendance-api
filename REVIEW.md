# Code Review — HROne Attendance & Analytics API

## Verification status

The existing automated suite has four tests. Syntax compilation and these tests pass, but MongoDB integration, concurrency, analytics results, and execution plans still require verification.

## Findings

1. **Attendance history format — Fixed, integration test pending.** `attendance_response()` and the correction handler were updated to use `at`, `by`, `reason`, and `changes`, with `from`/`to` values. Test compatibility with existing stored history.

2. **Invalid attendance status — Fixed, endpoint test pending.** `list_attendance()` now rejects statuses outside `ALL_STATUSES` with HTTP 422.

3. **Punch-out timestamp validation — Needs verification.** The open-record query may hide a timestamp-order error and return 404 instead of 422. Test earlier, equal, and later punch-out timestamps.

4. **Concurrent punch-outs — Needs verification.** Test simultaneous requests to ensure only one closes the record and competing requests receive the specified conflict response.

5. **Monthly weekday presence — Needs verification.** `build_employee_monthly_pipeline()` must exclude weekend records from `present_days`.

6. **Monthly attendance-rate rounding — Updated, boundary test pending.** The precision expression was changed to four decimal places. Verify HALF_UP rounding at boundary values.

7. **Department weekday presence — Needs verification.** `build_department_summary_pipeline()` must exclude weekend attendance from present-day totals without losing weekend late/overtime totals.

8. **Employees without logs — Review result.** The department summary starts from eligible employees and looks up attendance, which supports including employees with no logs. Test this and exclude employees who join after the period.

9. **Leaderboard ties — Review result.** The pipeline uses rank-based window logic before applying the rank cutoff. Test tied employees at the cutoff and deterministic employee-code ordering.

10. **Complete department trend — Needs verification.** `build_department_trend_pipeline()` must return every requested calendar day, including weekends and days without logs. Test departments with no eligible employees.

11. **Seven-day moving average — Needs verification.** Check window boundaries and missing-day behavior against manually calculated fixtures.

12. **Query execution plans — Needs verification.** Index definitions alone do not prove index usage. Run `executionStats` and check for `IXSCAN`, unexpected `COLLSCAN`, and excessive document examination.

13. **Test coverage — Incomplete.** The four existing tests focus on date-range validation. Add tests for history serialization, pagination, analytics edge cases, and concurrent updates.

## Areas reviewed

- Unique indexes are defined for employee codes and employee/date attendance keys.
- The inspected late-arrival helper applies a strict greater-than-ten-minute grace threshold.
- The inspected overtime helper applies a thirty-minute minimum.
- Work-hour rounding uses decimal HALF_UP rounding.
- Attendance pagination counts filtered records before applying offset and page size.
- Correction updates use a conditional filter to detect concurrent changes; verify this with a MongoDB integration test.

## Final status

This review distinguishes implemented changes from outstanding verification. Do not treat untested integration, concurrency, or query-plan behavior as passed.
