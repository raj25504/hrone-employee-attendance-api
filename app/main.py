"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Everything lives in this one file, as the assignment requires. Sections:
  1. configuration / database handle / indexes
  2. pure time + attendance rules (R1-R5, R8)  - no I/O, unit-tested
  3. request / response models
  4. endpoints (health, employees, attendance)
  5. analytics pipelines + endpoints
  6. explain endpoint
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority over .env
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database handle, indexes
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000  # below this a number "looks like seconds" -> 422
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")
MAX_PUNCH_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92
MAX_PATCH_RETRIES = 5

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class _State:
    """Holds the (lazily created) client/database so tests can swap in their own."""

    client: Optional[MongoClient] = None
    db: Optional[Database] = None
    owns_client: bool = False
    indexes_ready: bool = False


STATE = _State()


def get_db() -> Database:
    if STATE.db is None:
        uri = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
        STATE.client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
        STATE.db = STATE.client[os.getenv("MONGO_DB") or "attendance_db"]
        STATE.owns_client = True
    return STATE.db


def use_database(db: Database) -> None:
    """Test hook: point the app at an existing database object (not closed on shutdown)."""
    STATE.db = db
    STATE.client = None
    STATE.owns_client = False
    STATE.indexes_ready = False


def ensure_indexes(db: Database) -> None:
    """Create every index the endpoints rely on. create_index is idempotent for an identical spec."""
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


def try_ensure_indexes() -> bool:
    try:
        ensure_indexes(get_db())
        STATE.indexes_ready = True
    except PyMongoError:
        # Never log the URI. The traceback is kept so the failure is visible, not swallowed.
        log.exception("index creation failed; will retry on the next /health call")
        STATE.indexes_ready = False
    return STATE.indexes_ready


@asynccontextmanager
async def lifespan(_: FastAPI):
    try_ensure_indexes()
    yield
    if STATE.owns_client and STATE.client is not None:
        STATE.client.close()
    STATE.client, STATE.db, STATE.owns_client, STATE.indexes_ready = None, None, False, False


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    log.error("database error: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Pure rules: instants, attendance date, late / overtime / work hours
# --------------------------------------------------------------------------- #
def now_ms() -> int:
    """Current time in epoch ms. Isolated so tests can patch the clock."""
    return int(time.time() * 1000)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """PyMongo may hand back naive datetimes; they are UTC by definition."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    dt = as_utc(dt)
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def trunc_ms(ms: int) -> int:
    """R1: instants are truncated to whole seconds."""
    return ms - ms % 1000


def trunc(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts: earlier than shift_end -> previous day."""
    ist = trunc(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = _hhmm(shift_end)
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    h, m = _hhmm(shift_start)
    return datetime.combine(date.fromisoformat(date_str), datetime.min.time(), IST).replace(hour=h, minute=m)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = _hhmm(shift_end)
    end = datetime.combine(date.fromisoformat(date_str), datetime.min.time(), IST).replace(hour=h, minute=m)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor(minutes since shift start)."""
    delta = int((trunc(punch_in) - shift_start_at(date_str, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes between shift end and punch-out), only if >= 30."""
    delta = int((trunc(punch_out) - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = delta // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up. Decimal keeps this exact (no float rounding)."""
    seconds = int((trunc(punch_out) - trunc(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def is_half_day(work_hours: Decimal) -> bool:
    """R5: based on the already-rounded work_hours."""
    return work_hours < HALF_DAY_LIMIT


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    date_str: str,
    shift_start: str,
    shift_end: str,
) -> dict[str, Any]:
    """All four server-computed fields for a record, per the table in DATA_MODEL.md."""
    if status in NON_PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, date_str, shift_start, shift_end),
        "half_day": is_half_day(hours),
    }


def month_bounds(month: str) -> tuple[str, str, list[str]]:
    """First day, last day and the Sat/Sun dates of a YYYY-MM month."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise ValueError("year out of range")
    last = calendar.monthrange(year, mon)[1]
    weekends = [
        date(year, mon, d).isoformat() for d in range(1, last + 1) if date(year, mon, d).weekday() >= 5
    ]
    return f"{month}-01", f"{month}-{last:02d}", weekends


def working_days(start: str, end: str, joined_on: str) -> int:
    """R7: Mon-Fri between max(start, joined_on) and end, inclusive."""
    first = date.fromisoformat(max(start, joined_on))
    last = date.fromisoformat(end)
    count, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# 3. Validation helpers and models
# --------------------------------------------------------------------------- #
def bad_param(loc: tuple, msg: str, value: Any = None) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def parse_date(value: str, loc: tuple) -> str:
    """Strict YYYY-MM-DD that is a real calendar date."""
    if not re.fullmatch(DATE_RE, value):
        raise bad_param(loc, "date must be YYYY-MM-DD", value)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise bad_param(loc, "not a valid calendar date", value) from None
    return value


def parse_month(value: str, loc: tuple) -> tuple[str, str, list[str]]:
    try:
        return month_bounds(value)
    except ValueError:
        raise bad_param(loc, "not a valid month", value) from None


def require(name: str, value: Any) -> Any:
    if value is None:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None}]
        )
    return value


EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
PageQ = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]
DateQ = Annotated[str, Query(pattern=DATE_RE)]
MonthOptQ = Annotated[Optional[str], Query(pattern=MONTH_RE)]
DateOptQ = Annotated[Optional[str], Query(pattern=DATE_RE)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("not a valid calendar date") from None
        return v

    @model_validator(mode="after")
    def _shift_differs(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Only these fields can be corrected. Derived fields in a body are ignored (extra = ignore)."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self) -> "RegularizeRequest":
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class HistoryEntryOut(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryOut]


class AttendancePage(BaseModel):
    items: list[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class HealthOut(BaseModel):
    status: Literal["ok"]


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendRow(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendOut(BaseModel):
    department: str
    items: list[TrendRow]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


# --------------------------------------------------------------------------- #
# Serialisation of stored documents -> API shape
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc["created_at"]),
    }


def _history_value(field: str, value: Any) -> Any:
    return to_ms(value) if field in ("punch_in", "punch_out") and isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    changes = {
        field: {"from": _history_value(field, ch.get("from")), "to": _history_value(field, ch.get("to"))}
        for field, ch in (entry.get("changes") or {}).items()
    }
    return {"at": to_ms(entry["at"]), "by": entry.get("by", ""), "reason": entry.get("reason", ""), "changes": changes}


def record_out(doc: dict) -> dict:
    """Missing legacy fields (half_day, history, late_minutes...) take their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def count_matching(col: Any, query: dict) -> int:
    """Exact count for a filtered query; O(1) collection metadata for the unfiltered case (a full
    count_documents({}) would scan every document on a 100k-record collection)."""
    return col.count_documents(query) if query else col.estimated_document_count()


def find_employee(emp_code: str) -> dict:
    emp = get_db().employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthOut, tags=["system"])
def health() -> dict:
    """200 only when MongoDB answers a ping; 503 otherwise."""
    try:
        get_db().command("ping")
    except PyMongoError:
        log.error("health check: MongoDB did not answer ping")
        raise HTTPException(503, "MongoDB unavailable") from None
    if not STATE.indexes_ready:
        try_ensure_indexes()  # startup may have run while MongoDB was still down
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=EmployeeOut, tags=["employees"])
def create_employee(body: EmployeeCreate) -> dict:
    doc = body.model_dump()
    doc["created_at"] = from_ms(now_ms())
    try:
        # Uniqueness is enforced by the unique index on emp_code, not by a prior find_one.
        get_db().employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20) -> dict:
    query = {"department": department} if department is not None else {}
    col = get_db().employees
    total = count_matching(col, query)
    cursor = col.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut, tags=["attendance"])
def punch_in(body: PunchInRequest) -> dict:
    emp = find_employee(body.emp_code)
    ms = trunc_ms(body.punched_at if body.punched_at is not None else now_ms())
    ts = from_ms(ms)
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, day, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        # One atomic insert guarded by the unique (emp_code, date) index: concurrent twins -> one 201, rest 409.
        get_db().attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecordOut, tags=["attendance"])
def punch_out(body: PunchOutRequest) -> dict:
    emp = find_employee(body.emp_code)
    ts = from_ms(trunc_ms(body.punched_at if body.punched_at is not None else now_ms()))
    logs = get_db().attendance_logs
    for _ in range(MAX_PATCH_RETRIES):
        rec = logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
        )
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        started = trunc(rec["punch_in"])
        if ts <= started:
            raise HTTPException(422, "punched_at must be after punch_in")
        if ts - started > MAX_PUNCH_SPAN:
            raise HTTPException(422, "punch-out is more than 24 hours after punch-in")
        derived = derive_fields(rec["status"], rec["punch_in"], ts, rec["date"], emp["shift_start"], emp["shift_end"])
        # Atomic conditional update: only succeeds while the record is still open and unchanged.
        updated = logs.find_one_and_update(
            {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
            {"$set": {"punch_out": ts, **{k: v for k, v in derived.items() if k != "late_minutes"}}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
        # Lost a race (another punch-out or a correction): loop re-reads and decides 409 / retry.
    raise HTTPException(409, "record changed concurrently, please retry")


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng: dict[str, str] = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        query["date"] = rng
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def check_attendance_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from is not None:
        parse_date(date_from, ("query", "date_from"))
    if date_to is not None:
        parse_date(date_to, ("query", "date_to"))
    if date_from is not None and date_to is not None and date_from > date_to:
        raise bad_param(("query", "date_from"), "date_from must not be after date_to", date_from)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: DateOptQ = None,
    date_to: DateOptQ = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
) -> dict:
    check_attendance_dates(date_from, date_to)
    query = attendance_filter(emp_code, date_from, date_to, status)
    col = get_db().attendance_logs
    total = count_matching(col, query)
    cursor = col.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def plan_regularization(emp: dict, rec: dict, req: RegularizeRequest) -> tuple[dict, dict]:
    """Pure function: validate a correction against the stored record and compute the new field values.

    Returns (fields_to_set, changes). Raises HTTPException(422) for rule violations.
    """
    cur_status = rec["status"]
    cur_in, cur_out = as_utc(rec.get("punch_in")), as_utc(rec.get("punch_out"))
    status = req.status if req.status is not None else cur_status

    if status in NON_PRESENCE:
        if req.punch_in is not None or req.punch_out is not None:
            raise HTTPException(422, f"status {status} clears punch times; do not supply punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(trunc_ms(req.punch_in)) if req.punch_in is not None else cur_in
        new_out = from_ms(trunc_ms(req.punch_out)) if req.punch_out is not None else cur_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if trunc(new_out) <= trunc(new_in):
                raise HTTPException(422, "punch_out must be after punch_in")
            if trunc(new_out) - trunc(new_in) > MAX_PUNCH_SPAN:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if (status, new_in, new_out) == (cur_status, cur_in, cur_out):
        raise HTTPException(422, "the request changes nothing")

    new_values = {
        "status": status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive_fields(status, new_in, new_out, rec["date"], emp["shift_start"], emp["shift_end"]),
    }
    old_values = {
        "status": cur_status,
        "punch_in": cur_in,
        "punch_out": cur_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {k: {"from": old_values[k], "to": v} for k, v in new_values.items() if old_values[k] != v}
    return new_values, changes


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut, tags=["attendance"])
def regularize_attendance(
    emp_code: str, date: Annotated[str, Path(pattern=DATE_RE)], body: RegularizeRequest
) -> dict:
    day = parse_date(date, ("path", "date"))
    emp = find_employee(emp_code)
    logs = get_db().attendance_logs
    for _ in range(MAX_PATCH_RETRIES):
        rec = logs.find_one({"emp_code": emp_code, "date": day})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new_values, changes = plan_regularization(emp, rec, body)
        entry = {"at": from_ms(now_ms()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        n_hist = len(rec.get("history") or [])
        # Optimistic concurrency: apply only if nothing relevant changed since we read the record.
        # History is append-only, so "no entry at index n_hist yet" means its length is still n_hist.
        # $set and $push happen in one atomic update, so an audit entry can never be lost or orphaned.
        updated = logs.find_one_and_update(
            {
                "_id": rec["_id"],
                "status": rec["status"],
                "punch_in": rec.get("punch_in"),
                "punch_out": rec.get("punch_out"),
                f"history.{n_hist}": {"$exists": False},
            },
            {"$set": new_values, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
        # Someone else modified the record: re-read and re-plan against the newer state.
    raise HTTPException(409, "record changed concurrently, please retry")


# --------------------------------------------------------------------------- #
# 5. Analytics: MongoDB aggregation pipelines
#
# Stored derived values are authoritative (DATA_MODEL.md), so pipelines only read them.
# Rounding is done with integer arithmetic inside the pipeline so that "half-up" is exact:
#   half_up(a / b) = floor((2a + b) / (2b))  for non-negative integers a, b.
# $round in MongoDB is banker's rounding and float x*100 is inexact, so neither is used for reporting.
# --------------------------------------------------------------------------- #
def _halves_expr(weekend_dates: Optional[list[str]]) -> dict:
    """Present-day contribution of one record in half-days: presence status -> 2 (1 if half_day), else 0.
    With weekend_dates given, weekend records contribute 0 (R7: present_days counts Mon-Fri only)."""
    cond: dict = {"$in": ["$status", PRESENCE]}
    if weekend_dates is not None:
        cond = {"$and": [cond, {"$not": [{"$in": ["$date", weekend_dates]}]}]}
    return {"$cond": [cond, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]}


def _late_flag() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def _half_up_div(numerator: Any, denominator: Any) -> dict:
    """Expression for floor(numerator / denominator + 0.5), exact for non-negative integers."""
    return {
        "$floor": {
            "$divide": [
                {"$add": [{"$multiply": [2, numerator]}, denominator]},
                {"$multiply": [2, denominator]},
            ]
        }
    }


def build_monthly_pipeline(emp_code: str, month: str, start: str, end: str, weekends: list[str]) -> list[dict]:
    return [
        # Served by the unique (emp_code, date) index: equality on emp_code + range on date.
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_halves": {"$sum": _halves_expr(weekends)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag()},
                "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(start: str, end: str, weekends: list[str], department: Optional[str]) -> list[dict]:
    match: dict[str, Any] = {"joined_on": {"$lte": end}}  # R9: joined on/before the last day of the month
    if department is not None:
        match["department"] = department
    return [
        # Start from EMPLOYEES so people with zero logs are still counted in headcount.
        {"$match": match},
        # Per-employee roll-up of that month's logs, done inside the join (indexed on emp_code + date).
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_halves": {"$sum": _halves_expr(weekends)},
                            "late_count": {"$sum": _late_flag()},
                            "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                            # work hours in integer hundredths so the later sum is exact
                            "wh_cents": {
                                "$sum": {
                                    "$cond": [
                                        {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]},
                                        {"$round": [{"$multiply": ["$work_hours", 100]}, 0]},
                                        0,
                                    ]
                                }
                            },
                            "wh_n": {
                                "$sum": {
                                    "$cond": [
                                        {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "s",
            }
        },
        {"$set": {"s": {"$arrayElemAt": ["$s", 0]}}},  # employee without logs -> s missing -> zeros below
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_halves": {"$sum": {"$ifNull": ["$s.present_halves", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
                "total_late": {"$sum": {"$ifNull": ["$s.total_late", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
                "wh_cents": {"$sum": {"$ifNull": ["$s.wh_cents", 0]}},
                "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$divide": ["$present_halves", 2]},
                # average over records (sum of all hours / number of records), not mean of means
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$wh_n", 0]},
                        None,
                        {"$divide": [_half_up_div("$wh_cents", "$wh_n"), 100]},
                    ]
                },
                "late_count": 1,
                "total_late_minutes": "$total_late",
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def build_leaderboard_pipeline(start: str, end: str, department: Optional[str], limit: int) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "cnt": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4): ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit applies AFTER ranking, so ties at the cutoff are all kept.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": "$total",
                "late_count": "$cnt",
            }
        },
    ]
    return pipeline


def build_trend_pipeline(department: str, d_from: str, d_to: str) -> list[dict]:
    n_days = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
    start_dt = datetime.combine(date.fromisoformat(d_from), datetime.min.time(), UTC)
    return [
        # Employees of the department who joined on/before the last day (R9). Index: (department, joined_on).
        {"$match": {"department": department, "joined_on": {"$lte": d_to}}},
        {
            "$facet": {
                # branch 1: how many employees joined on each date (for the per-day headcount)
                "hc": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
                # branch 2: per-date presence / late numbers from the logs of those employees
                "st": [
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                                {"$project": {"_id": 0, "date": 1, "ph": _halves_expr(None), "late": _late_flag()}},
                            ],
                            "as": "days",
                        }
                    },
                    {"$unwind": "$days"},
                    {"$group": {"_id": "$days.date", "ph": {"$sum": "$days.ph"}, "late": {"$sum": "$days.late"}}},
                ],
            }
        },
        # Gap filling: generate exactly one row per calendar day in [from, to] (weekends and empty days included).
        {
            "$project": {
                "_id": 0,
                "hc": 1,
                "st": 1,
                "rows": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}},
                    }
                },
            }
        },
        {"$unwind": "$rows"},
        {
            "$project": {
                "hc": 1,
                "st": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$rows"}},
                "dow": {"$isoDayOfWeek": "$rows"},  # 1 = Monday .. 7 = Sunday
            }
        },
        {
            "$set": {
                "headcount": {
                    "$reduce": {
                        "input": "$hc",
                        "initialValue": 0,
                        "in": {"$cond": [{"$lte": ["$$this._id", "$date"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]},
                    }
                },
                "s": {
                    "$arrayElemAt": [
                        {"$filter": {"input": "$st", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]}}},
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "ph": {"$ifNull": ["$s.ph", 0]},
                "late": {"$ifNull": ["$s.late", 0]},
                "working": {"$and": [{"$gte": ["$dow", 1]}, {"$lte": ["$dow", 5]}]},
            }
        },
        {
            "$set": {
                # attendance_rate x 10000, half-up, as an exact integer; null off working days / zero headcount
                "rate_scaled": {
                    "$cond": [
                        {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                        _half_up_div({"$multiply": [5000, "$ph"]}, "$headcount"),
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    # current row + up to 6 preceding rows, all inside the requested range; $sum skips nulls
                    "rs_sum": {"$sum": "$rate_scaled", "window": {"documents": [-6, 0]}},
                    "rs_n": {
                        "$sum": {"$cond": [{"$isNumber": "$rate_scaled"}, 1, 0]},
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": "$working",
                "headcount": 1,
                "present_count": {"$divide": ["$ph", 2]},
                "late_count": "$late",
                "attendance_rate": {
                    "$cond": [{"$isNumber": "$rate_scaled"}, {"$divide": ["$rate_scaled", 10000]}, None]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$rs_n", 0]},
                        None,
                        {"$divide": [_half_up_div("$rs_sum", "$rs_n"), 10000]},
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut, tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthQ) -> dict:
    start, end, weekends = parse_month(month, ("query", "month"))
    emp = find_employee(emp_code)
    rows = list(get_db().attendance_logs.aggregate(build_monthly_pipeline(emp_code, month, start, end, weekends)))
    agg = rows[0] if rows else {}
    halves = int(agg.get("present_halves", 0))
    wdays = working_days(start, end, emp["joined_on"])
    pct = None
    if wdays > 0:
        # present/working*100 = 50*halves/wdays; half-up to 2 decimals in exact integer arithmetic
        pct = ((10000 * halves + wdays) // (2 * wdays)) / 100
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wdays,
        "present_days": halves / 2,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late", 0)),
        "total_overtime_minutes": int(agg.get("total_ot", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut, tags=["analytics"])
def department_summary(month: MonthQ, department: Optional[str] = None) -> dict:
    start, end, weekends = parse_month(month, ("query", "month"))
    pipeline = build_department_summary_pipeline(start, end, weekends, department)
    items = []
    for r in get_db().employees.aggregate(pipeline):
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": None if r["avg_work_hours"] is None else float(r["avg_work_hours"]),
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut, tags=["analytics"])
def late_leaderboard(
    month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
) -> dict:
    start, end, _ = parse_month(month, ("query", "month"))
    rows = get_db().attendance_logs.aggregate(build_leaderboard_pipeline(start, end, department, limit))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def check_trend_range(d_from: str, d_to: str) -> None:
    parse_date(d_from, ("query", "from"))
    parse_date(d_to, ("query", "to"))
    if d_to < d_from:
        raise bad_param(("query", "to"), "to must not be before from", d_to)
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1 > MAX_TREND_DAYS:
        raise bad_param(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days", d_to)


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut, tags=["analytics"])
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_RE)],
    to: DateQ,
) -> dict:
    check_trend_range(from_, to)
    db = get_db()
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = db.employees.aggregate(build_trend_pipeline(department, from_, to))
    items = [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": r["attendance_rate"],
            "moving_avg_7d": r["moving_avg_7d"],
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain endpoint (admin)
#
# Only the five whitelisted endpoint names are accepted, and every parameter goes through the same
# validators / builders as the real endpoint, so the caller can never inject a command or collection.
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _jsonable(doc: Any) -> Any:
    """BSON explain output -> plain JSON (dates, Int64, ObjectId... converted)."""
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut, tags=["admin"])
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: MonthOptQ = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: DateOptQ = None,
    date_to: DateOptQ = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_RE)] = None,
    to: DateOptQ = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
) -> dict:
    db = get_db()
    if endpoint == "attendance_list":
        check_attendance_dates(date_from, date_to)
        collection = "attendance_logs"
        command: dict[str, Any] = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require("emp_code", emp_code)
            require("month", month)
            start, end, weekends = parse_month(month, ("query", "month"))
            collection, pipeline = "attendance_logs", build_monthly_pipeline(emp_code, month, start, end, weekends)
        elif endpoint == "department_summary":
            require("month", month)
            start, end, weekends = parse_month(month, ("query", "month"))
            collection, pipeline = "employees", build_department_summary_pipeline(start, end, weekends, department)
        elif endpoint == "late_leaderboard":
            require("month", month)
            start, end, _ = parse_month(month, ("query", "month"))
            collection, pipeline = "attendance_logs", build_leaderboard_pipeline(start, end, department, limit)
        else:  # department_trend
            require("department", department)
            require("from", from_)
            require("to", to)
            check_trend_range(from_, to)
            collection, pipeline = "employees", build_trend_pipeline(department, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(result)}
