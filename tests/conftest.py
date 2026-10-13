"""Test fixtures.

* With TEST_MONGO_URI set, tests run against a real MongoDB in a throw-away database
  (random name, dropped afterwards - nothing else on that server is touched).
* Without it, API tests that only need basic CRUD fall back to mongomock, and tests that need real
  aggregation / explain / concurrency semantics are SKIPPED (never silently passed).
"""
import os
import uuid

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

TEST_URI = os.getenv("TEST_MONGO_URI")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_mongo: needs a real MongoDB (set TEST_MONGO_URI)")


def pytest_collection_modifyitems(config, items):
    if TEST_URI:
        return
    skip = pytest.mark.skip(reason="needs real MongoDB: set TEST_MONGO_URI (see README)")
    for item in items:
        if "real_mongo" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def db():
    if TEST_URI:
        client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
        name = f"attendance_test_{uuid.uuid4().hex[:10]}"
        database = client[name]
        yield database
        client.drop_database(name)
        client.close()
    else:
        client = mongomock.MongoClient(tz_aware=True)
        yield client["attendance_test"]


@pytest.fixture
def client(db):
    main.use_database(db)
    main.ensure_indexes(db)
    main.STATE.indexes_ready = True
    yield TestClient(main.app)
    main.STATE.db = None


EMP = {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-05",
}


def make_employee(client, **overrides):
    body = {**EMP, **overrides}
    r = client.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def ist_ms(date_str: str, hhmm: str, seconds: int = 0, millis: int = 0) -> int:
    """Epoch ms of an IST wall-clock time (helper for readable tests)."""
    from datetime import datetime

    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, date_str.split("-"))
    dt = datetime(y, mo, d, h, m, seconds, tzinfo=main.IST)
    return int(dt.timestamp()) * 1000 + millis
