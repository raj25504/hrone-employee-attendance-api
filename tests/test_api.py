"""API behaviour tests: health, employees, punch-in/out, listing, regularization.

Run on a real MongoDB when TEST_MONGO_URI is set, otherwise on mongomock (basic CRUD semantics only).
"""
import pytest
from pymongo.errors import PyMongoError

from app import main as m
from tests.conftest import EMP, ist_ms, make_employee


# ---- health ---------------------------------------------------------------------------------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_503_when_mongo_does_not_answer(client, monkeypatch):
    class Broken:
        def command(self, *a, **k):
            raise PyMongoError("down")

    monkeypatch.setattr(m, "get_db", lambda: Broken())
    r = client.get("/health")
    assert r.status_code == 503 and "detail" in r.json()


# ---- employees ------------------------------------------------------------------------------
def test_create_employee_defaults_and_shape(client):
    body = {k: v for k, v in EMP.items() if k not in ("shift_start", "shift_end")}
    r = client.post("/employees", json=body)
    assert r.status_code == 201
    j = r.json()
    assert j["shift_start"] == "09:30" and j["shift_end"] == "18:30"
    assert isinstance(j["created_at"], int) and j["created_at"] > m.MIN_EPOCH_MS
    assert "_id" not in j and "id" not in j


def test_create_employee_duplicate_is_409_and_not_stored_twice(client, db):
    make_employee(client)
    assert client.post("/employees", json=EMP).status_code == 409
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
        {"name": ""}, {"name": "x" * 101}, {"email": "nope"}, {"email": "a@b"}, {"department": ""},
        {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_end": "18:60"},
        {"shift_start": "10:00", "shift_end": "10:00"}, {"shift_start": "18:30"},  # equals default end
        {"joined_on": "2026-02-30"}, {"joined_on": "20260105"}, {"joined_on": "2026-1-5"},
        {"emp_code": 1234},
    ],
)
def test_create_employee_validation_422(client, patch):
    r = client.post("/employees", json={**EMP, **patch})
    assert r.status_code == 422, r.text


def test_create_employee_missing_fields_422(client):
    assert client.post("/employees", json={"emp_code": "EMP0001"}).status_code == 422


def test_list_employees_pagination_filter_sort_total(client):
    for i in range(1, 8):
        make_employee(client, emp_code=f"EMP{i:04d}", department="Eng" if i % 2 else "Sales")
    r = client.get("/employees", params={"page": 1, "page_size": 3}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002", "EMP0003"]
    assert r["total"] == 7 and r["page"] == 1 and r["page_size"] == 3
    r = client.get("/employees", params={"page": 3, "page_size": 3}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0007"]
    r = client.get("/employees", params={"department": "Eng"}).json()
    assert r["total"] == 4 and [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0003", "EMP0005", "EMP0007"]
    assert client.get("/employees", params={"department": "eng"}).json()["total"] == 0  # case-sensitive
    assert client.get("/employees", params={"page": 99}).json()["items"] == []


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_employees_bad_paging_422(client, params):
    assert client.get("/employees", params=params).status_code == 422


# ---- punch-in -------------------------------------------------------------------------------
def punch_in(client, ms, emp="EMP0001", **extra):
    return client.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": ms, **extra})


def test_punch_in_creates_record_with_derived_fields(client):
    make_employee(client)
    r = punch_in(client, ist_ms("2026-07-06", "10:05", 0, 750))
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["date"] == "2026-07-06" and j["status"] == "PRESENT"
    assert j["punch_in"] == ist_ms("2026-07-06", "10:05")  # truncated to the second
    assert j["punch_out"] is None and j["work_hours"] is None
    assert j["late_minutes"] == 35 and j["overtime_minutes"] == 0 and j["half_day"] is False and j["history"] == []
    assert "id" not in j and "_id" not in j


def test_punch_in_grace_boundary(client):
    make_employee(client)
    assert punch_in(client, ist_ms("2026-07-06", "09:40")).json()["late_minutes"] == 0
    assert punch_in(client, ist_ms("2026-07-07", "09:40", 1)).json()["late_minutes"] == 10


def test_punch_in_status_and_validation(client):
    make_employee(client)
    assert punch_in(client, ist_ms("2026-07-06", "09:00"), status="WFH").json()["status"] == "WFH"
    for bad in ("ABSENT", "LEAVE", "XYZ", None):
        assert punch_in(client, ist_ms("2026-07-07", "09:00"), status=bad).status_code == 422
    for bad_ts in (1783312500, 1.5, 1783312500000.0, "1783312500000", -5, 0, 4102444800001, True):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad_ts})
        assert r.status_code == 422, bad_ts
    assert client.post("/attendance/punch-in", json={}).status_code == 422


def test_punch_in_unknown_employee_404_and_duplicate_409(client):
    assert punch_in(client, ist_ms("2026-07-06", "09:00"), emp="EMP9999").status_code == 404
    make_employee(client)
    assert punch_in(client, ist_ms("2026-07-06", "09:00")).status_code == 201
    assert punch_in(client, ist_ms("2026-07-06", "11:00")).status_code == 409
    assert punch_in(client, ist_ms("2026-07-07", "09:00")).status_code == 201


def test_punch_in_defaults_to_now(client, monkeypatch):
    make_employee(client)
    monkeypatch.setattr(m, "now_ms", lambda: ist_ms("2026-07-06", "09:31", 5, 999))
    j = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"}).json()
    assert j["punch_in"] == ist_ms("2026-07-06", "09:31", 5) and j["date"] == "2026-07-06"


def test_punch_in_stores_bson_date_utc(client, db):
    make_employee(client)
    punch_in(client, ist_ms("2026-07-06", "09:28"))
    doc = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert m.to_ms(doc["punch_in"]) == ist_ms("2026-07-06", "09:28")
    assert m.as_utc(doc["punch_in"]).hour == 3 and m.as_utc(doc["punch_in"]).minute == 58  # 09:28 IST = 03:58 UTC


# ---- punch-out ------------------------------------------------------------------------------
def punch_out(client, ms, emp="EMP0001"):
    return client.post("/attendance/punch-out", json={"emp_code": emp, "punched_at": ms})


def test_punch_out_computes_everything(client):
    make_employee(client)
    punch_in(client, ist_ms("2026-07-06", "10:05"))
    r = punch_out(client, ist_ms("2026-07-06", "19:10"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["work_hours"], j["late_minutes"], j["overtime_minutes"], j["half_day"]) == (9.08, 35, 40, False)
    assert j["punch_out"] == ist_ms("2026-07-06", "19:10") and j["history"] == []


def test_punch_out_half_day_and_no_overtime_under_threshold(client):
    make_employee(client)
    punch_in(client, ist_ms("2026-07-06", "09:30"), status="WFH")
    j = punch_out(client, ist_ms("2026-07-06", "13:00")).json()
    assert j["work_hours"] == 3.5 and j["half_day"] is True and j["overtime_minutes"] == 0
    punch_in(client, ist_ms("2026-07-07", "09:30"))
    j = punch_out(client, ist_ms("2026-07-07", "18:59", 59)).json()
    assert j["overtime_minutes"] == 0


def test_punch_out_overnight_shift_across_midnight(client):
    make_employee(client, emp_code="EMP0005", shift_start="22:00", shift_end="06:00")
    r = punch_in(client, ist_ms("2026-07-06", "21:55"), emp="EMP0005")
    assert r.json()["date"] == "2026-07-06"
    j = punch_out(client, ist_ms("2026-07-07", "06:40"), emp="EMP0005").json()
    assert (j["date"], j["work_hours"], j["overtime_minutes"], j["half_day"]) == ("2026-07-06", 8.75, 40, False)


def test_punch_in_after_midnight_on_overnight_shift_belongs_to_previous_day(client):
    make_employee(client, emp_code="EMP0005", shift_start="22:00", shift_end="06:00")
    r = punch_in(client, ist_ms("2026-07-07", "01:30"), emp="EMP0005")
    assert r.json()["date"] == "2026-07-06"


def test_punch_out_errors(client):
    make_employee(client)
    assert punch_out(client, ist_ms("2026-07-06", "18:00"), emp="EMP9999").status_code == 404  # unknown employee
    assert punch_out(client, ist_ms("2026-07-06", "18:00")).status_code == 404  # no punch-in at all
    punch_in(client, ist_ms("2026-07-06", "09:30"))
    assert punch_out(client, ist_ms("2026-07-06", "09:00")).status_code == 404  # punch-in is after punched_at
    assert punch_out(client, ist_ms("2026-07-06", "09:30")).status_code == 422  # equal to punch_in
    assert punch_out(client, ist_ms("2026-07-07", "09:30", 1)).status_code == 422  # > 24h after
    assert punch_out(client, ist_ms("2026-07-06", "18:30")).status_code == 200
    assert punch_out(client, ist_ms("2026-07-06", "19:30")).status_code == 409  # already closed


def test_punch_out_exactly_24h_is_allowed(client):
    make_employee(client)
    punch_in(client, ist_ms("2026-07-06", "09:30"))
    assert punch_out(client, ist_ms("2026-07-07", "09:30")).status_code == 200


# ---- listing --------------------------------------------------------------------------------
def seed_logs(client, db):
    make_employee(client, emp_code="EMP0001")
    make_employee(client, emp_code="EMP0002", department="Sales")
    for emp in ("EMP0001", "EMP0002"):
        for day in ("06", "07", "08"):
            punch_in(client, ist_ms(f"2026-07-{day}", "09:30"), emp=emp)
    db.attendance_logs.update_one({"emp_code": "EMP0001", "date": "2026-07-08"}, {"$set": {"status": "LEAVE"}})


def test_list_attendance_sort_filter_pagination(client, db):
    seed_logs(client, db)
    r = client.get("/attendance").json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"),
        ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    r = client.get("/attendance", params={"page": 2, "page_size": 4}).json()
    assert r["total"] == 6 and len(r["items"]) == 2
    r = client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-08"}).json()
    assert r["total"] == 2 and [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07"]
    r = client.get("/attendance", params={"status": "LEAVE"}).json()
    assert r["total"] == 1 and r["items"][0]["emp_code"] == "EMP0001"
    r = client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 2  # inclusive bounds


@pytest.mark.parametrize(
    "params",
    [{"date_from": "2026-07-09", "date_to": "2026-07-08"}, {"date_from": "2026-13-01"}, {"date_to": "07/08/2026"},
     {"status": "BOGUS"}, {"page": 0}, {"page_size": 101}],
)
def test_list_attendance_validation_422(client, params):
    assert client.get("/attendance", params=params).status_code == 422


def test_legacy_documents_get_defaults(client, db):
    make_employee(client)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": m.from_ms(ist_ms("2026-07-14", "09:32")), "punch_out": m.from_ms(ist_ms("2026-07-14", "18:31")),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    j = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert j["half_day"] is False and j["history"] == []


# ---- regularization ---------------------------------------------------------------------------
def patch(client, day, **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return client.patch(f"/attendance/EMP0001/{day}", json=body)


def open_day(client, day="2026-07-07", hhmm="10:05"):
    make_employee(client)
    punch_in(client, ist_ms(day, hhmm))


def test_regularize_punch_in_recomputes_and_audits(client):
    open_day(client)
    punch_out(client, ist_ms("2026-07-07", "18:30"))
    r = patch(client, "2026-07-07", punch_in=ist_ms("2026-07-07", "09:28"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03 and j["punch_in"] == ist_ms("2026-07-07", "09:28")
    assert len(j["history"]) == 1
    h = j["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {
        "punch_in": {"from": ist_ms("2026-07-07", "10:05"), "to": ist_ms("2026-07-07", "09:28")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }


def test_regularize_history_is_appended_never_overwritten(client):
    open_day(client)
    punch_out(client, ist_ms("2026-07-07", "18:30"))
    patch(client, "2026-07-07", punch_in=ist_ms("2026-07-07", "09:28"))
    j = patch(client, "2026-07-07", punch_out=ist_ms("2026-07-07", "19:15"), reason="late sitting", regularized_by="boss").json()
    assert [h["by"] for h in j["history"]] == ["hr.admin", "boss"]
    assert set(j["history"][1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
    assert j["overtime_minutes"] == 45


def test_regularize_status_changes(client):
    open_day(client)
    punch_out(client, ist_ms("2026-07-07", "18:30"))
    j = patch(client, "2026-07-07", status="LEAVE").json()
    assert (j["status"], j["punch_in"], j["punch_out"], j["work_hours"], j["late_minutes"]) == ("LEAVE", None, None, None, 0)
    ch = j["history"][0]["changes"]
    assert ch["status"] == {"from": "PRESENT", "to": "LEAVE"} and ch["punch_in"]["to"] is None
    # back to a presence status needs a punch_in
    assert patch(client, "2026-07-07", status="WFH").status_code == 422
    j = patch(client, "2026-07-07", status="WFH", punch_in=ist_ms("2026-07-07", "09:30")).json()
    assert j["status"] == "WFH" and j["punch_out"] is None and j["late_minutes"] == 0 and len(j["history"]) == 2


def test_regularize_status_only_change_keeps_times(client):
    open_day(client)
    j = patch(client, "2026-07-07", status="ON_DUTY").json()
    assert j["status"] == "ON_DUTY" and list(j["history"][0]["changes"]) == ["status"]


def test_regularize_rules_422(client):
    open_day(client)
    d = "2026-07-07"
    assert patch(client, d, status="ABSENT", punch_in=ist_ms(d, "09:00")).status_code == 422
    assert patch(client, d, status="LEAVE", punch_out=ist_ms(d, "18:00")).status_code == 422
    assert patch(client, d, punch_in=ist_ms("2026-07-08", "09:00")).status_code == 422  # leaves the attendance date
    assert patch(client, d, punch_out=ist_ms(d, "10:05")).status_code == 422  # not after punch_in
    assert patch(client, d, punch_out=ist_ms(d, "09:00")).status_code == 422
    assert patch(client, d, punch_out=ist_ms("2026-07-08", "10:06")).status_code == 422  # > 24 h
    assert patch(client, d, punch_in=ist_ms(d, "10:05")).status_code == 422  # no-op
    assert patch(client, d, status="PRESENT").status_code == 422  # no-op
    assert patch(client, d).status_code == 422  # nothing supplied
    assert patch(client, d, reason="abc").status_code == 422  # reason too short
    assert patch(client, d, regularized_by="").status_code == 422
    assert patch(client, d, punch_in=1783312500).status_code == 422  # seconds, not ms
    assert patch(client, d, status="BOGUS").status_code == 422
    assert patch(client, d, punch_in=None).status_code == 422  # explicit null
    r = client.patch(f"/attendance/EMP0001/{d}", json={"status": "WFH"})
    assert r.status_code == 422  # reason / regularized_by are required
    # none of the rejected requests wrote anything
    assert client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"] == []


def test_regularize_404_and_bad_path_date(client):
    open_day(client)
    assert patch(client, "2026-07-09", status="LEAVE").status_code == 404  # no record that date
    assert client.patch("/attendance/EMP9999/2026-07-07", json={"status": "LEAVE", "reason": "valid reason", "regularized_by": "x"}).status_code == 404
    assert patch(client, "2026-02-30", status="LEAVE").status_code == 422
    assert patch(client, "not-a-date", status="LEAVE").status_code == 422


def test_regularize_ignores_client_supplied_derived_fields(client):
    open_day(client)
    j = patch(client, "2026-07-07", status="WFH", late_minutes=0, work_hours=99, overtime_minutes=500, half_day=True).json()
    assert j["late_minutes"] == 35 and j["work_hours"] is None and j["half_day"] is False and j["overtime_minutes"] == 0


def test_regularize_overnight_date_rule(client):
    make_employee(client, emp_code="EMP0005", shift_start="22:00", shift_end="06:00")
    punch_in(client, ist_ms("2026-07-06", "21:55"), emp="EMP0005")
    ok = client.patch("/attendance/EMP0005/2026-07-06", json={"punch_in": ist_ms("2026-07-07", "01:00"), "reason": "moved later", "regularized_by": "hr"})
    assert ok.status_code == 200  # 01:00 on the 7th is still the shift that started on the 6th
    bad = client.patch("/attendance/EMP0005/2026-07-06", json={"punch_in": ist_ms("2026-07-07", "07:00"), "reason": "moved later", "regularized_by": "hr"})
    assert bad.status_code == 422


def test_regularize_legacy_record_without_history_or_half_day(client, db):
    make_employee(client)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": m.from_ms(ist_ms("2026-07-14", "09:32")), "punch_out": m.from_ms(ist_ms("2026-07-14", "18:31")),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    j = patch(client, "2026-07-14", punch_out=ist_ms("2026-07-14", "13:00")).json()
    assert j["work_hours"] == 3.47 and j["half_day"] is True and len(j["history"]) == 1
    assert j["history"][0]["changes"]["half_day"] == {"from": False, "to": True}


# ---- indexes ------------------------------------------------------------------------------------
def test_ensure_indexes_is_idempotent_and_creates_required_unique_indexes(db):
    m.ensure_indexes(db)
    m.ensure_indexes(db)
    emp = db.employees.index_information()
    att = db.attendance_logs.index_information()
    assert any(v.get("unique") and v["key"] == [("emp_code", 1)] for v in emp.values())
    assert any(v.get("unique") and v["key"] == [("emp_code", 1), ("date", 1)] for v in att.values())
    assert any(v["key"] == [("date", -1), ("emp_code", 1)] for v in att.values())


def test_unique_index_really_blocks_duplicates_at_the_database(db):
    m.ensure_indexes(db)
    doc = {"emp_code": "EMP0001", "date": "2026-07-06"}
    db.attendance_logs.insert_one(dict(doc))
    from pymongo.errors import DuplicateKeyError
    with pytest.raises(DuplicateKeyError):
        db.attendance_logs.insert_one(dict(doc))


# ---- deterministic race injection (works on mongomock and real MongoDB) -----------------------------
def test_regularize_replans_when_a_concurrent_correction_lands_first(client, db, monkeypatch):
    """A second correction slips in between our read and our write: ours must not overwrite it or lose its audit entry."""
    open_day(client)
    punch_out(client, ist_ms("2026-07-07", "18:30"))
    real_plan = m.plan_regularization
    calls = {"n": 0}

    def plan_then_race(emp, rec, req):
        calls["n"] += 1
        result = real_plan(emp, rec, req)
        if calls["n"] == 1:  # simulate the other writer
            db.attendance_logs.update_one(
                {"_id": rec["_id"]},
                {"$set": {"status": "WFH"}, "$push": {"history": {"at": m.from_ms(1783312500000), "by": "other", "reason": "raced in", "changes": {"status": {"from": "PRESENT", "to": "WFH"}}}}},
            )
        return result

    monkeypatch.setattr(m, "plan_regularization", plan_then_race)
    r = patch(client, "2026-07-07", punch_in=ist_ms("2026-07-07", "09:28"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert calls["n"] == 2  # first attempt was rejected by the guard, second re-planned on fresh state
    assert [h["by"] for h in j["history"]] == ["other", "hr.admin"]  # nothing lost
    assert j["status"] == "WFH"  # the other writer's change survived (not overwritten with stale data)
    assert j["punch_in"] == ist_ms("2026-07-07", "09:28")


def test_regularize_gives_409_if_the_record_keeps_changing(client, db, monkeypatch):
    open_day(client)
    real_plan = m.plan_regularization

    def always_race(emp, rec, req):
        result = real_plan(emp, rec, req)
        db.attendance_logs.update_one({"_id": rec["_id"]}, {"$push": {"history": {"at": m.from_ms(1783312500000), "by": "x", "reason": "r", "changes": {}}}})
        return result

    monkeypatch.setattr(m, "plan_regularization", always_race)
    assert patch(client, "2026-07-07", status="WFH").status_code == 409


def test_punch_out_loses_race_to_another_punch_out_and_gets_409(client, db, monkeypatch):
    open_day(client, hhmm="09:30")
    real_derive = m.derive_fields
    calls = {"n": 0}

    def derive_then_race(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # another request closes the record first
            db.attendance_logs.update_one(
                {"emp_code": "EMP0001", "date": "2026-07-07"}, {"$set": {"punch_out": m.from_ms(ist_ms("2026-07-07", "17:00"))}})
        return real_derive(*a, **k)

    monkeypatch.setattr(m, "derive_fields", derive_then_race)
    assert punch_out(client, ist_ms("2026-07-07", "18:30")).status_code == 409
    doc = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert m.to_ms(doc["punch_out"]) == ist_ms("2026-07-07", "17:00")  # the winner's value was not overwritten
