"""Explain endpoint on a ~100k-record dataset. REAL MongoDB ONLY (needs the real planner).

Set EXPLAIN_DOCS to change the size (default 100000).
"""
import json
import os
import random

import pytest

from app import main as m

pytestmark = pytest.mark.real_mongo
DOCS = int(os.getenv("EXPLAIN_DOCS", "100000"))


@pytest.fixture
def big(client, db):
    rnd = random.Random(7)
    depts = ["Engineering", "Sales", "Ops", "HR"]
    emps = [{"emp_code": f"EMP{i:05d}", "name": f"N{i}", "email": f"e{i}@x.com", "department": depts[i % 4],
             "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2025-01-01", "created_at": m.from_ms(1783312500000)}
            for i in range(1, 301)]
    db.employees.insert_many(emps)
    logs, day_no = [], 0
    from datetime import date, timedelta
    while len(logs) < DOCS:
        d = (date(2025, 1, 1) + timedelta(days=day_no)).isoformat()
        day_no += 1
        for e in emps:
            if len(logs) >= DOCS:
                break
            logs.append({"emp_code": e["emp_code"], "date": d, "status": rnd.choice(["PRESENT", "PRESENT", "WFH", "LEAVE", "ABSENT"]),
                         "punch_in": None, "punch_out": None, "work_hours": 8.0, "late_minutes": rnd.choice([0, 0, 0, 15, 40]),
                         "overtime_minutes": 0, "half_day": False, "history": []})
    db.attendance_logs.insert_many(logs, ordered=False)
    return day_no


def explain(client, name, **params):
    r = client.get(f"/admin/explain/{name}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"endpoint", "collection", "explain"} and body["endpoint"] == name
    return body, json.dumps(body["explain"])


CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP00005", "page": 3}),
    ("attendance_list", {"status": "LEAVE", "date_from": "2025-02-01", "date_to": "2025-02-28"}),
    ("attendance_list", {"date_from": "2025-02-01", "date_to": "2025-02-28", "page": 2, "page_size": 50}),
    ("employee_monthly", {"emp_code": "EMP00005", "month": "2025-02"}),
    ("department_summary", {"month": "2025-02"}),
    ("department_summary", {"month": "2025-02", "department": "Sales"}),
    ("late_leaderboard", {"month": "2025-02"}),
    ("late_leaderboard", {"month": "2025-02", "department": "Ops", "limit": 5}),
    ("department_trend", {"department": "Engineering", "from": "2025-02-01", "to": "2025-03-15"}),
]


@pytest.mark.parametrize("name,params", CASES, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CASES)])
def test_plan_uses_index_and_no_collection_scan(client, big, name, params):
    body, text = explain(client, name, **params)
    assert "IXSCAN" in text, text[:2000]
    assert "COLLSCAN" not in text, text[:2000]
    assert "executionStats" in text


def test_attendance_list_explain_really_runs_the_query(client, big):
    body, _ = explain(client, "attendance_list", date_from="2025-02-01", date_to="2025-02-03", page_size=10)
    stats = body["explain"]["executionStats"]
    assert stats["nReturned"] == 10 and stats["totalDocsExamined"] < 2000  # bounded, not a full scan


@pytest.mark.parametrize(
    "name,params",
    [("employee_monthly", {"month": "2025-02"}), ("employee_monthly", {"emp_code": "EMP00001"}), ("department_summary", {}),
     ("late_leaderboard", {}), ("department_trend", {"department": "Sales", "from": "2025-02-01"}),
     ("department_trend", {"from": "2025-02-01", "to": "2025-02-02"}), ("department_trend", {"department": "Sales", "from": "2025-02-05", "to": "2025-02-01"}),
     ("attendance_list", {"date_from": "2025-02-05", "date_to": "2025-02-01"}), ("late_leaderboard", {"month": "2025-02", "limit": 99})],
)
def test_missing_or_invalid_params_are_422(client, name, params):
    assert client.get(f"/admin/explain/{name}", params=params).status_code == 422


def test_unknown_endpoint_names_are_rejected(client):
    for name in ("users", "attendance_logs", "drop", "..%2Fadmin"):
        assert client.get(f"/admin/explain/{name}").status_code in (404, 422)
