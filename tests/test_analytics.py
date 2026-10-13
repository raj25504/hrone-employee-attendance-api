"""Analytics endpoints on a hand-designed July/August 2026 dataset with exact expected values.

REAL MongoDB ONLY: these use $lookup / $facet / $setWindowFields / $dateAdd, which mongomock cannot run.
Run with:  TEST_MONGO_URI=mongodb://localhost:27017 pytest tests/test_analytics.py

Calendar: 2026-07-06 is a Monday; 07-04/05, 11/12, 18/19, 25/26 are weekends; 2026-08-03 is a Monday.
Stored derived values are inserted DIRECTLY (not via the API) because analytics must read what is stored.
"""
import pytest

from app import main as m
from tests.conftest import ist_ms

pytestmark = pytest.mark.real_mongo


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": m.from_ms(1783312500000)}


def log(code, day, status="PRESENT", late=0, ot=0, wh=None, half=False, open_=False):
    has_times = status in m.PRESENCE
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": m.from_ms(ist_ms(day, "09:30")) if has_times else None,
            "punch_out": None if (open_ or not has_times) else m.from_ms(ist_ms(day, "18:30")),
            "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}


@pytest.fixture
def data(client, db):
    db.employees.insert_many([
        emp("EMP0001", "Engineering", "2026-01-05"),
        emp("EMP0002", "Engineering", "2026-07-13"),  # mid-month joiner
        emp("EMP0006", "Engineering", "2026-01-05"),
        emp("EMP0003", "Sales", "2026-01-05"),
        emp("EMP0004", "Sales", "2026-01-05"),  # no logs at all
        emp("EMP0007", "Sales", "2026-01-05"),
        emp("EMP0005", "Ops", "2026-08-03"),  # joins after July
    ])
    db.attendance_logs.insert_many([
        # EMP0001
        log("EMP0001", "2026-07-06", late=0, ot=0, wh=9.12),
        log("EMP0001", "2026-07-07", late=35, ot=40, wh=9.08),
        log("EMP0001", "2026-07-08", "WFH", wh=3.5, half=True),
        log("EMP0001", "2026-07-09", "LEAVE"),
        log("EMP0001", "2026-07-10", "ABSENT"),
        log("EMP0001", "2026-07-11", late=20, ot=60, wh=8.0),  # Saturday
        log("EMP0001", "2026-07-13", "ON_DUTY", wh=8.0),
        # EMP0002 (joined 07-13)
        log("EMP0002", "2026-07-13", late=11, wh=8.5),
        log("EMP0002", "2026-07-14", wh=9.0),
        log("EMP0002", "2026-07-18", wh=4.0, half=True),  # Saturday
        # EMP0006
        log("EMP0006", "2026-07-06", wh=9.0),
        log("EMP0006", "2026-07-08", late=55, ot=30, wh=8.55),
        log("EMP0006", "2026-07-09", open_=True),  # present, no punch-out yet
        # Sales
        log("EMP0003", "2026-07-06", late=45, wh=8.0),
        log("EMP0003", "2026-07-07", late=45, wh=8.01),
        log("EMP0003", "2026-07-08", "LEAVE"),
        log("EMP0003", "2026-08-03", late=100, wh=8.0),  # next month
        log("EMP0007", "2026-07-09", late=11, wh=8.0),
        log("EMP0007", "2026-07-10", "ON_DUTY", wh=6.0),
        # employee that does not exist
        log("EMP0099", "2026-07-06", late=500, wh=8.0),
    ])


# ---- employee monthly -------------------------------------------------------------------------
def monthly(client, code, month="2026-07"):
    return client.get(f"/analytics/employees/{code}/monthly", params={"month": month})


def test_monthly_full_month_weekend_half_day_leave(client, data):
    r = monthly(client, "EMP0001")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
        "present_days": 3.5,  # 06, 07, 13 full + 08 half; Saturday record excluded
        "leave_days": 1, "late_count": 2, "total_late_minutes": 55,  # Saturday lateness still counts
        "total_overtime_minutes": 100, "attendance_pct": 15.22}  # 3.5/23*100 = 15.217...


def test_monthly_mid_month_joiner_counts_working_days_from_join_date(client, data):
    j = monthly(client, "EMP0002").json()
    assert j["working_days"] == 15 and j["present_days"] == 2.0 and j["attendance_pct"] == 13.33
    assert (j["late_count"], j["total_late_minutes"]) == (1, 11)


def test_monthly_employee_without_logs_and_before_join(client, data):
    j = monthly(client, "EMP0004").json()
    assert (j["working_days"], j["present_days"], j["attendance_pct"], j["late_count"], j["leave_days"]) == (23, 0.0, 0.0, 0, 0)
    j = monthly(client, "EMP0005").json()  # joins in August
    assert j["working_days"] == 0 and j["attendance_pct"] is None and j["present_days"] == 0.0


def test_monthly_only_that_month_and_errors(client, data):
    assert monthly(client, "EMP0003", "2026-08").json()["total_late_minutes"] == 100
    assert monthly(client, "EMP0003", "2026-07").json()["total_late_minutes"] == 90
    assert monthly(client, "EMP9999").status_code == 404
    for bad in ("2026-13", "2026-00", "26-07", "2026-7", "July"):
        assert monthly(client, "EMP0001", bad).status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422


# ---- department summary -----------------------------------------------------------------------
def summary(client, **params):
    return client.get("/analytics/departments/summary", params={"month": "2026-07", **params})


def test_department_summary_exact_values(client, data):
    r = summary(client)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["department"] for i in items] == ["Engineering", "Sales"]  # Ops omitted: headcount 0 in July
    eng, sales = items
    assert eng == {"department": "Engineering", "headcount": 3, "present_days": 8.5, "avg_work_hours": 7.68,
                   "late_count": 4, "total_late_minutes": 121, "leave_count": 1, "on_duty_count": 1}
    # 76.75 hours over 10 present records with hours = 7.675 -> half-up 7.68 (float round() would give 7.67);
    # it is NOT the mean of per-employee means, and the open record / LEAVE / ABSENT are excluded
    assert sales == {"department": "Sales", "headcount": 3,  # EMP0004 has zero logs but still counts
                     "present_days": 4.0, "avg_work_hours": 7.5, "late_count": 3, "total_late_minutes": 101,
                     "leave_count": 1, "on_duty_count": 1}  # 30.01 / 4 = 7.5025


def test_department_summary_filter_and_months_without_data(client, data):
    assert [i["department"] for i in summary(client, department="Sales").json()["items"]] == ["Sales"]
    assert summary(client, department="Nope").json()["items"] == []
    aug = {i["department"]: i for i in summary(client, month="2026-08").json()["items"]}
    assert set(aug) == {"Engineering", "Ops", "Sales"}
    assert aug["Ops"]["headcount"] == 1 and aug["Ops"]["avg_work_hours"] is None and aug["Ops"]["present_days"] == 0.0
    assert aug["Engineering"]["avg_work_hours"] is None
    assert aug["Sales"]["present_days"] == 1.0 and aug["Sales"]["total_late_minutes"] == 100 and aug["Sales"]["avg_work_hours"] == 8.0
    jun = {i["department"]: i for i in summary(client, month="2026-06").json()["items"]}  # EMP0002 joined in July
    assert jun["Engineering"]["headcount"] == 2 and "Ops" not in jun


def test_department_summary_validation(client, data):
    assert client.get("/analytics/departments/summary").status_code == 422
    assert summary(client, month="2026-1").status_code == 422


# ---- late leaderboard ---------------------------------------------------------------------------
def board(client, **params):
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", **params})
    assert r.status_code == 200, r.text
    return [(i["rank"], i["emp_code"], i["total_late_minutes"], i["late_count"]) for i in r.json()["items"]]


def test_leaderboard_competition_ranking_and_row_order(client, data):
    assert board(client, limit=10) == [
        (1, "EMP0003", 90, 2), (2, "EMP0001", 55, 2), (2, "EMP0006", 55, 1), (4, "EMP0002", 11, 1), (4, "EMP0007", 11, 1)]


def test_leaderboard_limit_applies_after_ranking_so_ties_at_cutoff_are_kept(client, data):
    assert [r[1] for r in board(client, limit=1)] == ["EMP0003"]
    assert [r[1] for r in board(client, limit=2)] == ["EMP0003", "EMP0001", "EMP0006"]  # tie at rank 2 -> 3 rows
    assert [r[1] for r in board(client, limit=3)] == ["EMP0003", "EMP0001", "EMP0006"]  # there is no rank 3
    assert len(board(client, limit=4)) == 5


def test_leaderboard_department_ranks_inside_department(client, data):
    assert board(client, department="Engineering") == [(1, "EMP0001", 55, 2), (1, "EMP0006", 55, 1), (3, "EMP0002", 11, 1)]
    assert board(client, department="Engineering", limit=1) == [(1, "EMP0001", 55, 2), (1, "EMP0006", 55, 1)]
    assert board(client, department="Sales") == [(1, "EMP0003", 90, 2), (2, "EMP0007", 11, 1)]
    assert board(client, department="Nope") == []


def test_leaderboard_ignores_unknown_employees_zero_late_and_other_months(client, data):
    codes = [r[1] for r in board(client, limit=50)]
    assert "EMP0099" not in codes and "EMP0004" not in codes
    assert board(client, month="2026-08") == [(1, "EMP0003", 100, 1)]
    assert board(client, month="2026-05") == []


def test_leaderboard_validation(client, data):
    for params in ({"limit": 0}, {"limit": 51}, {"month": "bad"}):
        assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", **params}).status_code == 422
    assert client.get("/analytics/leaderboard/late").status_code == 422


# ---- department trend -----------------------------------------------------------------------------
def trend(client, dept, start, end):
    return client.get(f"/analytics/departments/{dept}/trend", params={"from": start, "to": end})


def test_trend_engineering_exact_rows_and_moving_average(client, data):
    r = trend(client, "Engineering", "2026-07-06", "2026-07-19")
    assert r.status_code == 200, r.text
    rows = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"])
            for i in r.json()["items"]]
    assert rows == [
        ("2026-07-06", True, 2, 2.0, 0, 1.0, 1.0),
        ("2026-07-07", True, 2, 1.0, 1, 0.5, 0.75),
        ("2026-07-08", True, 2, 1.5, 1, 0.75, 0.75),  # half-day counts 0.5
        ("2026-07-09", True, 2, 1.0, 0, 0.5, 0.6875),  # LEAVE does not count
        ("2026-07-10", True, 2, 0.0, 0, 0.0, 0.55),  # ABSENT record -> rate 0.0
        ("2026-07-11", False, 2, 1.0, 1, None, 0.55),  # Saturday: present_count/late_count still reported, rate null
        ("2026-07-12", False, 2, 0.0, 0, None, 0.55),
        ("2026-07-13", True, 3, 2.0, 1, 0.6667, 0.4833),  # joiner raises headcount to 3; window = rows 07-07..07-13
        ("2026-07-14", True, 3, 1.0, 0, 0.3333, 0.45),
        ("2026-07-15", True, 3, 0.0, 0, 0.0, 0.3),  # weekday with no records at all -> 0.0
        ("2026-07-16", True, 3, 0.0, 0, 0.0, 0.2),
        ("2026-07-17", True, 3, 0.0, 0, 0.0, 0.2),
        ("2026-07-18", False, 3, 0.5, 0, None, 0.2),
        ("2026-07-19", False, 3, 0.0, 0, None, 0.2),
    ]


def test_trend_one_row_per_day_and_window_does_not_see_rows_before_from(client, data):
    items = trend(client, "Engineering", "2026-07-14", "2026-07-14").json()["items"]
    assert len(items) == 1 and items[0]["attendance_rate"] == 0.3333 and items[0]["moving_avg_7d"] == 0.3333
    items = trend(client, "Engineering", "2026-07-11", "2026-07-12").json()["items"]  # weekend only
    assert [i["moving_avg_7d"] for i in items] == [None, None] and [i["attendance_rate"] for i in items] == [None, None]


def test_trend_headcount_zero_before_anyone_joined(client, data):
    items = trend(client, "Ops", "2026-07-30", "2026-08-04").json()["items"]
    got = [(i["date"], i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in items]
    assert got == [
        ("2026-07-30", 0, None, None), ("2026-07-31", 0, None, None), ("2026-08-01", 0, None, None),
        ("2026-08-02", 0, None, None), ("2026-08-03", 1, 0.0, 0.0), ("2026-08-04", 1, 0.0, 0.0)]


def test_trend_crosses_month_and_year_boundaries(client, data):
    items = trend(client, "Sales", "2026-12-28", "2027-01-03").json()["items"]
    assert [i["date"] for i in items] == ["2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03"]
    assert [i["is_working_day"] for i in items] == [True, True, True, True, True, False, False]


def test_trend_range_limits_and_errors(client, data):
    assert len(trend(client, "Sales", "2026-07-01", "2026-09-30").json()["items"]) == 92  # exactly 92 days is allowed
    assert trend(client, "Sales", "2026-07-01", "2026-10-01").status_code == 422  # 93 days
    assert trend(client, "Sales", "2026-07-10", "2026-07-09").status_code == 422
    assert trend(client, "Nope", "2026-07-01", "2026-07-02").status_code == 404
    assert trend(client, "Sales", "2026-02-30", "2026-03-02").status_code == 422
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422
    assert trend(client, "engineering", "2026-07-01", "2026-07-02").status_code == 404  # case-sensitive
