"""Parallel-request tests. REAL MongoDB ONLY: they prove the unique indexes / atomic updates work, which mongomock cannot."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import main as m
from tests.conftest import EMP, ist_ms, make_employee

pytestmark = pytest.mark.real_mongo
N = 16


def parallel(fn, n=N):
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))


def test_concurrent_employee_creates_yield_one_201_and_rest_409(client, db):
    codes = [r.status_code for r in parallel(lambda i: TestClient(m.app).post("/employees", json=EMP))]
    assert sorted(codes) == [201] + [409] * (N - 1)
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1


def test_concurrent_punch_ins_yield_one_201_and_rest_409(client, db):
    make_employee(client)
    body = {"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06", "09:30")}
    codes = [r.status_code for r in parallel(lambda i: TestClient(m.app).post("/attendance/punch-in", json=body))]
    assert sorted(codes) == [201] + [409] * (N - 1)
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}) == 1


def test_concurrent_punch_outs_yield_one_200(client, db):
    make_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06", "09:30")})
    body = {"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06", "18:30")}
    codes = [r.status_code for r in parallel(lambda i: TestClient(m.app).post("/attendance/punch-out", json=body))]
    assert sorted(codes) == [200] + [409] * (N - 1)


def test_concurrent_corrections_never_lose_an_audit_entry(client, db):
    make_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06", "09:30")})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06", "18:30")})

    def correct(i):
        body = {"punch_out": ist_ms("2026-07-06", "18:31") + i * 60_000, "reason": f"correction {i}", "regularized_by": f"user{i}"}
        return TestClient(m.app).patch("/attendance/EMP0001/2026-07-06", json=body)

    responses = parallel(correct, 8)
    ok = [r for r in responses if r.status_code == 200]
    assert ok and all(r.status_code in (200, 409) for r in responses)
    doc = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(doc["history"]) == len(ok)  # exactly one entry per successful correction, none lost
    assert len({h["reason"] for h in doc["history"]}) == len(ok)
    # the stored state equals the last applied entry's "to" value (no stale overwrite)
    last = doc["history"][-1]["changes"]["punch_out"]["to"]
    assert doc["punch_out"] == last
