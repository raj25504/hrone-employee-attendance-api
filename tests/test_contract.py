"""The running app must expose exactly the routes/params/status codes in openapi.yaml."""
import pathlib

import pytest
import yaml

from app import main as m

SPEC = yaml.safe_load((pathlib.Path(__file__).parent.parent / "openapi.yaml").read_text())
METHODS = {"get", "post", "patch", "put", "delete"}


def spec_operations():
    return {(p, mth): op for p, item in SPEC["paths"].items() for mth, op in item.items() if mth in METHODS}


def app_operations():
    return {(p, mth): op for p, item in m.app.openapi()["paths"].items() for mth, op in item.items() if mth in METHODS}


def test_same_routes_and_methods():
    assert set(app_operations()) == set(spec_operations())


def _param_names(op, spec_root):
    names = set()
    for p in op.get("parameters", []):
        if "$ref" in p:
            p = SPEC["components"]["parameters"][p["$ref"].split("/")[-1]]
        names.add((p["in"], p["name"]))
    return names


@pytest.mark.parametrize("key", sorted(spec_operations()))
def test_parameters_match(key):
    want = _param_names(spec_operations()[key], SPEC)
    have = _param_names(app_operations()[key], None)
    assert have == want, f"{key}: missing {want - have}, extra {have - want}"


@pytest.mark.parametrize("key", sorted(spec_operations()))
def test_success_status_code_matches(key):
    want = {c for c in spec_operations()[key]["responses"] if c.startswith("2")}
    have = {c for c in app_operations()[key]["responses"] if c.startswith("2")}
    assert have == want


def test_required_request_fields_match():
    schemas = m.app.openapi()["components"]["schemas"]
    for name, model in (("EmployeeCreate", "EmployeeCreate"), ("PunchInRequest", "PunchInRequest"),
                        ("PunchOutRequest", "PunchOutRequest"), ("RegularizeRequest", "RegularizeRequest")):
        assert set(schemas[model].get("required", [])) == set(SPEC["components"]["schemas"][name]["required"]), name


def test_response_field_names_match_spec():
    schemas = m.app.openapi()["components"]["schemas"]
    pairs = {"AttendanceRecordOut": "AttendanceRecord", "EmployeeOut": "Employee", "EmployeeMonthlyOut": "EmployeeMonthly",
             "TrendRow": None, "LeaderboardItem": None, "DepartmentSummaryItem": None}
    for mine, theirs in pairs.items():
        if theirs:
            assert set(schemas[mine]["properties"]) == set(SPEC["components"]["schemas"][theirs]["properties"]), mine
    sp = SPEC["components"]["schemas"]
    assert set(schemas["TrendRow"]["properties"]) == set(sp["Trend"]["properties"]["items"]["items"]["properties"])
    assert set(schemas["LeaderboardItem"]["properties"]) == set(sp["Leaderboard"]["properties"]["items"]["items"]["properties"])
    assert set(schemas["DepartmentSummaryItem"]["properties"]) == set(sp["DepartmentSummary"]["properties"]["items"]["items"]["properties"])


def test_explain_endpoint_enum_matches():
    want = next(p for p in SPEC["paths"]["/admin/explain/{endpoint}"]["get"]["parameters"] if p["name"] == "endpoint")["schema"]["enum"]
    have = next(p for p in m.app.openapi()["paths"]["/admin/explain/{endpoint}"]["get"]["parameters"] if p["name"] == "endpoint")["schema"]["enum"]
    assert have == want


def test_unknown_explain_name_is_422(client):
    assert client.get("/admin/explain/drop_database").status_code == 422
