"""Pure unit tests for R1-R5, rounding, time conversion and month helpers. No database needed."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import main as m
from tests.conftest import ist_ms


def dt(date_str, hhmm, sec=0):
    return m.from_ms(ist_ms(date_str, hhmm, sec))


# ---- R1: attendance date, time zones, overnight ---------------------------------------------
def test_ms_roundtrip_and_truncation():
    assert m.to_ms(m.from_ms(1783312500123)) == 1783312500123
    assert m.trunc_ms(1783312500999) == 1783312500000
    naive = datetime(2026, 7, 6, 3, 58)  # naive means UTC
    assert m.to_ms(naive) == m.to_ms(naive.replace(tzinfo=timezone.utc))


def test_date_is_ist_date_not_utc_date():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th -> attendance date is the 7th
    assert m.attendance_date(dt("2026-07-07", "00:30"), "09:30", "18:30") == "2026-07-07"
    # 23:50 IST on the 6th is 18:20 UTC on the 6th
    assert m.attendance_date(dt("2026-07-06", "23:50"), "09:30", "18:30") == "2026-07-06"


def test_overnight_punch_in_before_shift_end_belongs_to_previous_day():
    assert m.attendance_date(dt("2026-07-07", "05:59", 59), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(dt("2026-07-07", "06:00"), "22:00", "06:00") == "2026-07-07"  # not earlier
    assert m.attendance_date(dt("2026-07-06", "21:55"), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(dt("2026-07-06", "22:00"), "22:00", "06:00") == "2026-07-06"


def test_overnight_applies_when_end_equals_start_only_by_contract_rule():
    assert m.is_overnight("22:00", "06:00")
    assert m.is_overnight("09:00", "09:00")  # <= means overnight (the API rejects equal shifts at creation)
    assert not m.is_overnight("09:30", "18:30")


def test_sub_second_is_truncated_before_date_calculation():
    # 05:59:59.900 -> counts as 05:59:59, still before 06:00
    ms = ist_ms("2026-07-07", "05:59", 59, 900)
    assert m.attendance_date(m.from_ms(ms), "22:00", "06:00") == "2026-07-06"


# ---- R2: late ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hhmm,sec,expected",
    [
        ("09:20", 0, 0),  # early
        ("09:30", 0, 0),
        ("09:40", 0, 0),  # exactly 10:00 after -> not late
        ("09:40", 1, 10),  # 10:01 after -> late, 10 whole minutes since shift start
        ("09:41", 0, 11),
        ("10:15", 59, 45),
        ("10:05", 0, 35),
    ],
)
def test_late_minutes_boundaries(hhmm, sec, expected):
    assert m.compute_late_minutes(dt("2026-07-06", hhmm, sec), "2026-07-06", "09:30") == expected


def test_late_ignores_sub_second():
    # 09:40:00.900 counts as 09:40:00 -> 0
    p = m.from_ms(ist_ms("2026-07-06", "09:40", 0, 900))
    assert m.compute_late_minutes(p, "2026-07-06", "09:30") == 0


def test_late_for_overnight_shift_measured_from_previous_day_start():
    # shift 22:00-06:00, punch 22:11 -> 11 late
    assert m.compute_late_minutes(dt("2026-07-06", "22:11"), "2026-07-06", "22:00") == 11
    # punch 21:55 -> early -> 0
    assert m.compute_late_minutes(dt("2026-07-06", "21:55"), "2026-07-06", "22:00") == 0


# ---- R3: overtime --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hhmm,sec,expected",
    [("18:30", 0, 0), ("18:35", 0, 0), ("18:59", 59, 0), ("19:00", 0, 30), ("19:10", 0, 40), ("19:30", 59, 60), ("17:00", 0, 0)],
)
def test_overtime_threshold_and_floor(hhmm, sec, expected):
    assert m.compute_overtime_minutes(dt("2026-07-06", hhmm, sec), "2026-07-06", "09:30", "18:30") == expected


def test_overnight_overtime_uses_next_day_shift_end():
    # sample 7: shift 22:00-06:00, out 06:40 next day -> 40 min overtime
    assert m.compute_overtime_minutes(dt("2026-07-07", "06:40"), "2026-07-06", "22:00", "06:00") == 40
    assert m.compute_overtime_minutes(dt("2026-07-07", "06:29", 59), "2026-07-06", "22:00", "06:00") == 0


# ---- R4 / R5: work hours + half day ---------------------------------------------------------
def test_work_hours_samples_from_data_model():
    assert m.compute_work_hours(dt("2026-07-06", "09:28"), dt("2026-07-06", "18:35")) == Decimal("9.12")
    assert m.compute_work_hours(dt("2026-07-06", "10:05"), dt("2026-07-06", "19:10")) == Decimal("9.08")
    assert m.compute_work_hours(dt("2026-07-06", "21:55"), dt("2026-07-07", "06:40")) == Decimal("8.75")


def test_work_hours_half_up_not_bankers_not_float():
    base = dt("2026-07-06", "09:00")
    add = lambda secs: m.from_ms(m.to_ms(base) + secs * 1000)
    # 3 h 90 s = 3.025 h exactly -> half-up 3.03 (banker's / Python round() give 3.02)
    assert m.compute_work_hours(base, add(3 * 3600 + 90)) == Decimal("3.03")
    # 9630 s = 2.675 h exactly -> half-up 2.68 (the classic float case: round(2.675, 2) == 2.67)
    assert m.compute_work_hours(base, add(9630)) == Decimal("2.68")
    # 18 s = 0.005 h exactly -> 0.01
    assert m.compute_work_hours(base, add(18)) == Decimal("0.01")
    assert round(9630 / 3600, 2) == 2.67  # documents why the starter's round() was wrong


def test_half_day_uses_rounded_hours():
    base = dt("2026-07-06", "09:00")
    # 4.495 h rounds to 4.50 -> NOT a half day ; 4.494 h rounds to 4.49 -> half day
    sec_4495 = int(4.495 * 3600)  # 16182 s -> 4.495 exactly
    h = m.compute_work_hours(base, m.from_ms(m.to_ms(base) + sec_4495 * 1000))
    assert h == Decimal("4.50") and not m.is_half_day(h)
    h2 = m.compute_work_hours(base, m.from_ms(m.to_ms(base) + (sec_4495 - 36) * 1000))
    assert h2 == Decimal("4.49") and m.is_half_day(h2)
    assert m.is_half_day(Decimal("3.50"))


def test_derive_fields_table_from_data_model():
    d = m.derive_fields("PRESENT", dt("2026-07-06", "10:05"), None, "2026-07-06", "09:30", "18:30")
    assert d == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    d = m.derive_fields("PRESENT", dt("2026-07-06", "10:05"), dt("2026-07-06", "19:10"), "2026-07-06", "09:30", "18:30")
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    d = m.derive_fields("WFH", dt("2026-07-07", "09:30"), dt("2026-07-07", "13:00"), "2026-07-07", "09:30", "18:30")
    assert d["half_day"] is True and d["work_hours"] == 3.5
    assert m.derive_fields("LEAVE", None, None, "2026-07-08", "09:30", "18:30") == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---- calendar helpers ----------------------------------------------------------------------
def test_month_bounds_and_weekends():
    start, end, weekends = m.month_bounds("2026-07")
    assert (start, end) == ("2026-07-01", "2026-07-31")
    assert weekends == ["2026-07-04", "2026-07-05", "2026-07-11", "2026-07-12", "2026-07-18", "2026-07-19", "2026-07-25", "2026-07-26"]
    assert m.month_bounds("2028-02")[1] == "2028-02-29"
    with pytest.raises(ValueError):
        m.month_bounds("0000-05")


def test_working_days_full_month_and_mid_month_joiner():
    assert m.working_days("2026-07-01", "2026-07-31", "2026-01-05") == 23
    assert m.working_days("2026-07-01", "2026-07-31", "2026-07-13") == 15  # Mon 13th .. Fri 31st
    assert m.working_days("2026-07-01", "2026-07-31", "2026-07-11") == 15  # joined on a Saturday
    assert m.working_days("2026-07-01", "2026-07-31", "2026-08-01") == 0  # joins after the month


def test_half_up_helper():
    assert m.round_half_up(Decimal("0.125"), 2) == Decimal("0.13")
    assert m.round_half_up(Decimal("2.5"), 0) == Decimal("3")


# ---- pipelines are valid BSON and use the expected stages ---------------------------------------
def test_pipelines_encode_to_bson_and_use_expected_stages():
    import bson

    start, end, weekends = m.month_bounds("2026-07")
    p_trend = m.build_trend_pipeline("Engineering", "2026-07-01", "2026-07-31")
    p_lead = m.build_leaderboard_pipeline(start, end, None, 10)
    p_sum = m.build_department_summary_pipeline(start, end, weekends, None)
    p_mon = m.build_monthly_pipeline("EMP0001", "2026-07", start, end, weekends)
    for p in (p_trend, p_lead, p_sum, p_mon):
        bson.encode({"pipeline": p})
    stages = lambda p: {k for s in p for k in s}
    assert "$setWindowFields" in stages(p_trend) and "$facet" in stages(p_trend)
    assert "$setWindowFields" in stages(p_lead) and "$lookup" in stages(p_lead)
    assert "$lookup" in stages(p_sum)
    assert "$group" in stages(p_mon)
