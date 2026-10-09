from __future__ import annotations

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

load_dotenv()  # Does not override real environment variables.
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENT_STATUSES, "ABSENT", "LEAVE")
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

client = MongoClient(
os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
attendance = db["attendance_logs"].with_options(write_concern=WriteConcern("majority"))
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


def valid_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid calendar date") from exc
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    return value


def epoch_ms(value: int) -> datetime:
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    # Truncate milliseconds before calculations/storage.
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def now_second() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def public_employee(doc: dict) -> dict:
    return {k: doc[k] for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")} | {"created_at": to_ms(doc["created_at"])}


def public_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")), "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)), "half_day": bool(doc.get("half_day", False)),
        "history": [{
            "at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"],
            "changes": {k: {"from": to_ms(v.get("from")) if k in ("punch_in", "punch_out") else v.get("from"),
                             "to": to_ms(v.get("to")) if k in ("punch_in", "punch_out") else v.get("to")}
                        for k, v in h.get("changes", {}).items()}
        } for h in doc.get("history", [])]
    }


def shift_datetimes(emp: dict, punch_in: datetime) -> tuple[datetime, datetime, date]:
    local = punch_in.astimezone(IST)
    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])
    overnight = shift_end <= shift_start
    attendance_day = local.date()
    # For overnight shifts, an early-morning punch belongs to the previous shift day.
    if overnight and local.timetz().replace(tzinfo=None) < shift_end:
        attendance_day -= timedelta(days=1)
    start_day = attendance_day
    end_day = start_day + timedelta(days=1) if overnight else start_day
    start_local = datetime.combine(start_day, shift_start, tzinfo=IST)
    end_local = datetime.combine(end_day, shift_end, tzinfo=IST)
    return start_local.astimezone(UTC), end_local.astimezone(UTC), attendance_day


def late_minutes(emp: dict, punch_in: Optional[datetime], attendance_day: str | date) -> int:
    if punch_in is None:
        return 0
    if isinstance(attendance_day, str):
        attendance_day = date.fromisoformat(attendance_day)
    local = punch_in.astimezone(IST)
    shift_start = time.fromisoformat(emp["shift_start"])
    start = datetime.combine(attendance_day, shift_start, tzinfo=IST).astimezone(UTC)
    elapsed = int((punch_in - start).total_seconds())
    if elapsed > 600:
        return max(0, elapsed // 60)
    return 0


def derive_fields(emp: dict, record: dict) -> dict:
    pin, pout = record.get("punch_in"), record.get("punch_out")
    status = record.get("status")
    if status not in PRESENT_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    day = date.fromisoformat(record["date"])
    result = {"late_minutes": late_minutes(emp, pin, day), "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if pin is not None and pout is not None:
        seconds = int((pout - pin).total_seconds())
        hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        result["work_hours"] = float(hours)
        _, shift_end, _ = shift_datetimes(emp, pin)
        # shift_datetimes uses the punch-in date to select the appropriate shift day.
        overtime = int((pout - shift_end).total_seconds() // 60)
        result["overtime_minutes"] = overtime if overtime >= 30 else 0
        result["half_day"] = hours < Decimal("4.50")
    return result


def month_bounds(month: str) -> tuple[str, str]:
    valid_month(month)
    year, mon = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def round2(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round4(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def working_days(start: str, end: str) -> int:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    count = 0
    while a <= b:
        if a.weekday() < 5:
            count += 1
        a += timedelta(days=1)
    return count


class EmployeeCreate(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def date_format(cls, v): return valid_date(v)
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v


class PunchOutRequest(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v


class RegularizeRequest(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamps(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v


@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING), ("status", ASCENDING)], name="attendance_emp_date_status")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_emp")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_late_emp")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeCreate):
    doc = payload.model_dump()
    doc["created_at"] = now_second()
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    filt = {"department": department} if department is not None else {}
    total = employees.count_documents(filt)
    docs = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [public_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(payload: PunchInRequest):
    emp = employees.find_one({"emp_code": payload.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    punched = epoch_ms(payload.punched_at) if payload.punched_at is not None else now_second()
    _, _, day = shift_datetimes(emp, punched)
    record = {"emp_code": payload.emp_code, "date": day.isoformat(), "status": payload.status,
              "punch_in": punched, "punch_out": None, "work_hours": None,
              "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    record.update(derive_fields(emp, record))
    try: attendance.insert_one(record)
    except DuplicateKeyError: raise HTTPException(409, "attendance already exists for this date")
    return public_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(payload: PunchOutRequest):
    emp = employees.find_one({"emp_code": payload.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    punched = epoch_ms(payload.punched_at) if payload.punched_at is not None else now_second()
    existing = attendance.find_one({"emp_code": payload.emp_code, "punch_in": {"$lte": punched}, "punch_out": None}, sort=[("punch_in", DESCENDING)])
    if not existing:
        already = attendance.find_one({"emp_code": payload.emp_code, "punch_in": {"$lte": punched}, "punch_out": {"$ne": None}}, sort=[("punch_in", DESCENDING)])
        if already: raise HTTPException(409, "attendance record already punched out")
        raise HTTPException(404, "no open punch-in found")
    seconds = int((punched - existing["punch_in"]).total_seconds())
    if seconds <= 0 or seconds > 24*3600: raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = derive_fields(emp, existing | {"punch_out": punched})
    updated = attendance.find_one_and_update(
        {"emp_code": payload.emp_code, "date": existing["date"], "punch_out": None, "punch_in": existing["punch_in"]},
        {"$set": {"punch_out": punched, **derived}, "$setOnInsert": {}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "attendance record already punched out")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    try:
        if date_from is not None: valid_date(date_from)
        if date_to is not None: valid_date(date_to)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    filt: dict[str, Any] = {}
    if emp_code is not None: filt["emp_code"] = emp_code
    if status is not None: filt["status"] = status
    if date_from or date_to:
        filt["date"] = {}
        if date_from: filt["date"]["$gte"] = date_from
        if date_to: filt["date"]["$lte"] = date_to
    total = attendance.count_documents(filt)
    docs = attendance.find(filt, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [public_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, payload: RegularizeRequest):
    try: valid_date(date)
    except ValueError as exc: raise HTTPException(422, str(exc))
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, "attendance record not found")
    changes_in = payload.model_dump(exclude_unset=True)
    changes_in.pop("reason", None); changes_in.pop("regularized_by", None)
    proposed = dict(old)
    if "status" in changes_in: proposed["status"] = changes_in["status"]
    if proposed["status"] in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_in or "punch_out" in changes_in:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        proposed["punch_in"] = proposed["punch_out"] = None
    else:
        for field in ("punch_in", "punch_out"):
            if field in changes_in:
                proposed[field] = epoch_ms(changes_in[field]) if changes_in[field] is not None else None
        if proposed.get("punch_in") is None: raise HTTPException(422, "presence status requires punch_in")
        pin_local = proposed["punch_in"].astimezone(IST)
        _, _, computed_day = shift_datetimes(emp, proposed["punch_in"])
        if computed_day.isoformat() != date: raise HTTPException(422, "punch_in must belong to the record attendance date")
        pout = proposed.get("punch_out")
        if pout is not None:
            seconds = (pout - proposed["punch_in"]).total_seconds()
            if seconds <= 0 or seconds > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derive_fields(emp, proposed)
    proposed.update(derived)
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    diff = {}
    for key in tracked:
        before, after = old.get(key), proposed.get(key)
        if key == "late_minutes": before = int(before or 0); after = int(after or 0)
        if key == "overtime_minutes": before = int(before or 0); after = int(after or 0)
        if key == "half_day": before = bool(before or False); after = bool(after or False)
        if before != after: diff[key] = {"from": before, "to": after}
    if not diff: raise HTTPException(422, "request does not change the record")
    entry = {"at": now_second(), "by": payload.regularized_by, "reason": payload.reason, "changes": diff}
    # Compare-and-swap protects concurrent corrections and preserves each successful history append.
    compare_filter = {"_id": old["_id"], "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    if "history" in old:
        compare_filter["history"] = old["history"]
    else:
        compare_filter["history"] = {"$exists": False}
    updated = attendance.find_one_and_update(
        compare_filter,
        {"$set": {k: proposed.get(k) for k in tracked}, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry regularization")
    return public_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    valid_month(month); start, end = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    effective_start = max(start, emp["joined_on"])
    wd = working_days(effective_start, end) if effective_start <= end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    agg = list(attendance.aggregate(pipeline, hint="attendance_emp_date_status"))
    row = agg[0] if agg else {}
    pct = round4((row.get("present_days", 0) / wd) * 100) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": round2(row.get("present_days", 0)),
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    valid_month(month); start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}
            ]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0
            ]}},
            "work_hours_n": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_n": {"$sum": "$work_hours_n"},
            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_n", 0]}, {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_n"]}, 2]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    rows = list(employees.aggregate(pipeline, hint="department_joined"))
    return {"month": month, "items": [{**r, "present_days": round2(r.get("present_days", 0)), "avg_work_hours": round2(r.get("avg_work_hours"))} for r in rows if r["headcount"] > 0]}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    valid_month(month); start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    rows = list(attendance.aggregate(pipeline, hint="attendance_date_late_emp"))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    try: valid_date(from_date); valid_date(to_date)
    except ValueError as exc: raise HTTPException(422, str(exc))
    first, last = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if last < first: raise HTTPException(422, "to must be >= from")
    if (last-first).days+1 > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    # Calendar rows and rolling averages are generated inside MongoDB, not in a Python date loop.
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {
            "_id": 0,
            "days": {"$range": [0, (last - first).days + 1]},
            "start": {"$dateFromString": {"dateString": from_date}},
        }},
        {"$unwind": "$days"},
        {"$project": {"date_obj": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$days"}}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$date_obj", "format": "%Y-%m-%d"}},
            "weekday": {"$dayOfWeek": "$date_obj"},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}
            ]}}}],
            "as": "joined_employees",
        }},
        {"$set": {"headcount": {"$size": "$joined_employees"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date", "codes": {"$map": {"input": "$joined_employees", "as": "e", "in": "$$e.emp_code"}}},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}
            ]}}}],
            "as": "logs",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [
                    {"$in": ["$$l.status", list(PRESENT_STATUSES)]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]},
                    0,
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
            }}},
            "is_working_day": {"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date_obj": 1},
            "output": {"moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$round": ["$present_count", 1]}, "late_count": 1,
            "attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]}, {"$round": ["$attendance_rate", 4]}, None]},
            "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_raw", None]}, {"$round": ["$moving_avg_raw", 4]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]
    rows = list(employees.aggregate(pipeline, hint="department_joined"))
    # MongoDB $round is ties-to-even; contract requires half-up. Round output values at the API boundary.
    for row in rows:
        row["attendance_rate"] = round4(row["attendance_rate"])
        row["moving_avg_7d"] = round4(row["moving_avg_7d"])
    return {"department": department, "items": rows}


def explain_command(collection, pipeline, hint=None):
    command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    if hint: command["hint"] = hint
    return db.command({"explain": command, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        filt: dict[str, Any] = {}
        if emp_code: filt["emp_code"] = emp_code
        if status: filt["status"] = status
        if date_from or date_to:
            filt["date"] = {}
            try:
                if date_from: filt["date"]["$gte"] = valid_date(date_from)
                if date_to: filt["date"]["$lte"] = valid_date(date_to)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        exp = db.command({"explain": {"find": "attendance_logs", "filter": filt, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size, "hint": "attendance_date_emp"}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard") and not month: raise HTTPException(422, "month is required")
    if month: valid_month(month)
    if endpoint == "employee_monthly":
        if not emp_code: raise HTTPException(422, "emp_code is required")
        start, end = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
        exp = explain_command(attendance, pipeline, "attendance_emp_date_status"); coll = "attendance_logs"
    elif endpoint == "department_summary":
        start, end = month_bounds(month); filt = {"joined_on": {"$lte": end}}
        if department is not None: filt["department"] = department
        pipeline = [{"$match": filt}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}, {"$limit": 100000}]
        exp = explain_command(employees, pipeline, "department_joined"); coll = "employees"
    elif endpoint == "late_leaderboard":
        start, end = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}]
        exp = explain_command(attendance, pipeline, "attendance_date_late_emp"); coll = "attendance_logs"
    else:
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        valid_date(from_date); valid_date(to_date)
        pipeline = [{"$match": {"department": department, "joined_on": {"$lte": to_date}}}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}]
        exp = explain_command(employees, pipeline, "department_joined"); coll = "employees"
    return {"endpoint": endpoint, "collection": coll, "explain": exp}
