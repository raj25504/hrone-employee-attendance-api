# Employee Attendance & Analytics API (FastAPI + MongoDB)

HROne engineering assignment. All application code is in `app/main.py` (one file, as required).
Review of the starter is in `REVIEW.md`; design answers are in `DECISIONS.md`.

## What is implemented
Every route in `openapi.yaml`: `GET /health` (real MongoDB ping, 503 when down), `POST|GET /employees`,
`POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
(recomputes derived fields, appends one audit entry), `GET /analytics/employees/{emp_code}/monthly`,
`GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`,
`GET /analytics/departments/{department}/trend`, `GET /admin/explain/{endpoint}`.
Rules R1-R10 are implemented as pure, unit-tested functions; the four analytics endpoints are MongoDB aggregation pipelines
(`$lookup`, `$facet`, `$setWindowFields`, `$rank`); nothing is computed by looping over documents in Python.
Nothing from the contract is left out.

## Setup
Requires Python 3.11+ and MongoDB 6.0+ (the grader uses 7.0).
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB; real environment variables override .env
docker run -d -p 27017:27017 mongo:7        # or use Atlas / a local mongod
python sample_seed.py       # optional: loads the 9 sample documents
uvicorn app.main:app --port 8000
```
Interactive docs: http://localhost:8000/docs . Indexes are created idempotently at every startup (no manual step).
If MongoDB is down at startup the app still starts, `/health` returns 503, and indexes are created on the first healthy `/health`.

```bash
curl -X POST localhost:8000/employees -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0001","name":"Asha Rao","email":"asha@example.com","department":"Engineering","joined_on":"2026-01-05"}'
curl -X POST localhost:8000/attendance/punch-in -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0001","punched_at":1783312500000}'
curl "localhost:8000/analytics/leaderboard/late?month=2026-07&limit=5"
curl "localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31"
```

## Tests

Install the development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the test suite without a real MongoDB server:

```bash
pytest
```

Run the complete test suite with a real MongoDB server:

```bash
```

Set the MongoDB test URI before running the tests in PowerShell:

```powershell
$env:TEST_MONGO_URI="mongodb://localhost:27017"
python -m pytest -q
```

When `TEST_MONGO_URI` is set, tests use a temporary database with a randomly generated name, which is dropped after the test run.

Tests marked `real_mongo` are skipped when `TEST_MONGO_URI` is not configured.

## Verification Status

- **164 tests passed, 0 failed** in the local Windows environment with a real MongoDB server configured.
- Verified API endpoints, validation, attendance rules, punch-in/punch-out workflows, attendance listing, manual corrections, and audit trails.
- Verified boundary conditions, overnight shifts, work-hour rounding, and injected race conditions.
- Verified index creation and the OpenAPI contract, including routes, parameters, status codes, and field names.
- Verified MongoDB-backed analytics, concurrency behavior, and explain-plan tests through the local test suite.

## Known Limitations / Assumptions

- `moving_avg_7d` averages daily attendance rates rounded to four decimal places, following the mean of the non-null `attendance_rate` values.
- A correction that leaves `status`, `punch_in`, and `punch_out` unchanged returns HTTP 422.
- `GET /admin/explain/{endpoint}` returns MongoDB's raw explain output, which may include the database server's hostname.
- Concurrent corrections are retried up to five times using the latest state. If conflicts persist, the API returns HTTP 409.
