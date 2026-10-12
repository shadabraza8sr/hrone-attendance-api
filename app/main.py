
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "hrone_attendance")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing. Set it in your .env file.")


UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    tz_aware=True,
    tzinfo=UTC,
)

db = client[MONGO_DB]


def create_indexes():
    """Create indexes required by employee and attendance endpoints."""
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )

    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined_on",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )

async def lifespan(app: FastAPI):
    try:
        client.admin.command("ping")
        create_indexes()
        yield
    finally:
        client.close()


app = FastAPI(
    title="HROne Employee Attendance & Analytics API",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# TIME AND DATE HELPERS
# ============================================================

def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def utc_now_seconds() -> datetime:
    """Return the current UTC time truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_epoch_ms(value: int) -> datetime:
    """Validate epoch milliseconds and return a UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be an integer in epoch milliseconds",
        )

    if not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(
            status_code=422,
            detail="Timestamp is outside the allowed range",
        )

    # Truncate milliseconds to whole seconds.
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def ist_datetime(value: datetime) -> datetime:
    """Convert a datetime to Indian Standard Time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(IST)


def parse_shift_time(value: str) -> time:
    """Parse an HH:MM shift time."""
    return datetime.strptime(value, "%H:%M").time()


def attendance_date_for_punch(
    punched_at: datetime,
    shift_start: str,
    shift_end: str,
) -> date:
    """
    Determine the attendance date in IST.

    For overnight shifts, a punch-in before the shift end belongs
    to the previous attendance date.
    """
    local = ist_datetime(punched_at)
    attendance_day = local.date()

    start = parse_shift_time(shift_start)
    end = parse_shift_time(shift_end)

    if end <= start and local.time().replace(tzinfo=None) < end:
        attendance_day -= timedelta(days=1)

    return attendance_day


def calculate_late_minutes(
    punched_at: datetime,
    attendance_day: date,
    shift_start: str,
) -> int:
    """
    Calculate late minutes using the strict 10-minute grace rule.

    Exactly 10 minutes late means zero late minutes.
    At 10 minutes and 1 second, late minutes equal 10.
    """
    local = ist_datetime(punched_at)

    scheduled_start = datetime.combine(
        attendance_day,
        parse_shift_time(shift_start),
        tzinfo=IST,
    )

    difference_seconds = int(
        (local - scheduled_start).total_seconds()
    )

    if difference_seconds <= 600:
        return 0

    return difference_seconds // 60


def calculate_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate work hours rounded to two decimal places, half-up."""
    seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )

    hours = seconds / Decimal("3600")

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def calculate_overtime_minutes(
    punch_out: datetime,
    attendance_day: date,
    shift_start: str,
    shift_end: str,
) -> int:
    """
    Calculate overtime after the scheduled shift end.

    Overtime is zero unless at least 30 minutes have elapsed
    beyond the scheduled shift end.
    """
    start = parse_shift_time(shift_start)
    end = parse_shift_time(shift_end)

    end_day = attendance_day

    if end <= start:
        end_day += timedelta(days=1)

    scheduled_end = datetime.combine(
        end_day,
        end,
        tzinfo=IST,
    )

    difference_seconds = int(
        (ist_datetime(punch_out) - scheduled_end).total_seconds()
    )

    if difference_seconds < 1800:
        return 0

    return difference_seconds // 60


# ============================================================
# EMPLOYEE MODELS
# ============================================================

class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    joined_on: date
    shift_start: str = "09:30"
    shift_end: str = "18:30"

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email format")

        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM format")

        return value

    @field_validator("shift_end")
    @classmethod
    def validate_different_shift_times(cls, value: str, info) -> str:
        start = info.data.get("shift_start")

        if start is not None and start == value:
            raise ValueError("Shift start and end must differ")

        return value


def employee_response(document: dict) -> dict:
    """Serialize an employee without exposing MongoDB _id."""
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": epoch_ms(document["created_at"]),
    }


# ============================================================
# ATTENDANCE REQUEST MODELS
# ============================================================

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(
                "punched_at must be an integer epoch timestamp"
            )

        return value


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(
                "punched_at must be an integer epoch timestamp"
            )

        return value


# ============================================================
# ATTENDANCE SERIALIZATION
# ============================================================

def attendance_response(document: dict) -> dict:
    """Serialize attendance without exposing MongoDB internal IDs."""
    history = []

    for entry in document.get("history", []):
        changes = {}

        for field, values in entry.get("changes", {}).items():
            values = dict(values)

            for value_key in ("from", "to"):
                value = values.get(value_key)
                if field in ("punch_in", "punch_out") and value is not None:
                    values[value_key] = epoch_ms(value)

            changes[field] = values

        history.append({
            "at": epoch_ms(entry["at"]) if entry.get("at") is not None else None,
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": epoch_ms(document.get("punch_in")),
        "punch_out": epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": history,
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )


# ============================================================
# EMPLOYEE ENDPOINTS
# ============================================================

@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeCreate):
    now = utc_now_seconds()

    document = {
        "emp_code": payload.emp_code,
        "name": payload.name,
        "email": payload.email,
        "department": payload.department,
        "shift_start": payload.shift_start,
        "shift_end": payload.shift_end,
        "joined_on": payload.joined_on.isoformat(),
        "created_at": now,
    }

    try:
        db.employees.insert_one(document)

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Employee code already exists",
        )

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

    return employee_response(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    filters = {}

    if department is not None:
        filters["department"] = department

    try:
        total = db.employees.count_documents(filters)

        cursor = (
            db.employees.find(filters, {"_id": 0})
            .sort("emp_code", ASCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        items = [employee_response(document) for document in cursor]

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ============================================================
# ATTENDANCE PUNCH-IN
# ============================================================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(payload: PunchInRequest):
    try:
        employee = db.employees.find_one(
            {"emp_code": payload.emp_code},
            {"_id": 0},
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        punched_at = (
            parse_epoch_ms(payload.punched_at)
            if payload.punched_at is not None
            else utc_now_seconds()
        )

        attendance_day = attendance_date_for_punch(
            punched_at,
            employee["shift_start"],
            employee["shift_end"],
        )

        late_minutes = calculate_late_minutes(
            punched_at,
            attendance_day,
            employee["shift_start"],
        )

        document = {
            "emp_code": payload.emp_code,
            "date": attendance_day.isoformat(),
            "status": payload.status,
            "punch_in": punched_at,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        db.attendance_logs.insert_one(document)

        return attendance_response(document)

    except HTTPException:
        raise

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        )

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )


# ============================================================
# ATTENDANCE PUNCH-OUT
# ============================================================

@app.post("/attendance/punch-out")
def punch_out(payload: PunchOutRequest):
    try:
        employee = db.employees.find_one(
            {"emp_code": payload.emp_code},
            {"_id": 0},
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        punched_at = (
            parse_epoch_ms(payload.punched_at)
            if payload.punched_at is not None
            else utc_now_seconds()
        )

        # Find the latest open punch-in not later than punch-out.
        record = db.attendance_logs.find_one(
            {
                "emp_code": payload.emp_code,
                "punch_in": {"$lte": punched_at},
                "punch_out": None,
                "status": {"$in": list(PRESENCE_STATUSES)},
            },
            sort=[("punch_in", DESCENDING)],
        )

        if record is None:
            raise HTTPException(
                status_code=404,
                detail="No open punch-in record found",
            )

        punch_in_time = record["punch_in"]

        if punched_at <= punch_in_time:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in",
            )

        elapsed_seconds = (
            punched_at - punch_in_time
        ).total_seconds()

        if elapsed_seconds > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="Punch-out cannot be more than 24 hours after punch-in",
            )

        work_hours = calculate_work_hours(
            punch_in_time,
            punched_at,
        )

        overtime_minutes = calculate_overtime_minutes(
            punched_at,
            date.fromisoformat(record["date"]),
            employee["shift_start"],
            employee["shift_end"],
        )

        # The conditional update ensures concurrent requests cannot
        # successfully close the same record twice.
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": record["_id"],
                "punch_out": None,
            },
            {
                "$set": {
                    "punch_out": punched_at,
                    "work_hours": work_hours,
                    "overtime_minutes": overtime_minutes,
                    "half_day": work_hours < 4.50,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise HTTPException(
                status_code=409,
                detail="Attendance record has already been punched out",
            )

        return attendance_response(updated)

    except HTTPException:
        raise

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# ATTENDANCE LIST WITH FILTERING AND PAGINATION
# ============================================================

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None:
        if date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from cannot be after date_to",
            )

    filters = {}

    if emp_code is not None:
        filters["emp_code"] = emp_code

    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status",
            )
        filters["status"] = status

    if date_from is not None or date_to is not None:
        date_filter = {}

        if date_from is not None:
            date_filter["$gte"] = date_from.isoformat()

        if date_to is not None:
            date_filter["$lte"] = date_to.isoformat()

        filters["date"] = date_filter

    try:
        total = db.attendance_logs.count_documents(filters)

        cursor = (
            db.attendance_logs.find(filters)
            .sort([
                ("date", DESCENDING),
                ("emp_code", ASCENDING),
            ])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        items = [
            attendance_response(record)
            for record in cursor
        ]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# ATTENDANCE CORRECTION REQUEST
# ============================================================

class AttendanceCorrectionRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_correction_timestamp(cls, value):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(
                "Punch timestamps must be integer epoch milliseconds"
            )
        return value

# ============================================================
# ATTENDANCE CORRECTION WITH AUDIT HISTORY
# ============================================================

@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: date,
    payload: AttendanceCorrectionRequest,
):
    try:
        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        query = {
            "emp_code": emp_code,
            "date": date.isoformat(),
        }

        record = db.attendance_logs.find_one(query)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail="Attendance record not found",
            )

        changes = {}
        supplied = payload.model_fields_set

        if "status" in supplied and payload.status is not None:
            changes["status"] = payload.status

        for field in ("punch_in", "punch_out"):
            if field in supplied:
                value = getattr(payload, field)
                changes[field] = (
                    parse_epoch_ms(value) if value is not None else None
                )

        if not changes:
            raise HTTPException(
                status_code=422,
                detail="No attendance fields supplied for correction",
            )

        new_status = changes.get("status", record.get("status"))
        new_punch_in = changes.get("punch_in", record.get("punch_in"))
        new_punch_out = changes.get("punch_out", record.get("punch_out"))

        if new_status in ("ABSENT", "LEAVE"):
            if (
                ("punch_in" in changes and changes["punch_in"] is not None)
                or ("punch_out" in changes and changes["punch_out"] is not None)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="ABSENT and LEAVE cannot have punch timestamps",
                )
            new_punch_in = None
            new_punch_out = None
            changes["punch_in"] = None
            changes["punch_out"] = None
        elif new_status in ("PRESENT", "WFH", "ON_DUTY"):
            if new_punch_in is None:
                raise HTTPException(
                    status_code=422,
                    detail="Presence status requires punch_in",
                )

        if new_punch_in is not None:
            local_punch_in = new_punch_in.astimezone(IST)
            expected_day = attendance_date_for_punch(
                new_punch_in,
                employee["shift_start"],
                employee["shift_end"],
            )
            if expected_day != date:
                raise HTTPException(
                    status_code=422,
                    detail="Punch-in must belong to the attendance date",
                )

        if new_punch_out is not None:
            if new_punch_in is None or new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="Punch-out must be after punch-in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="Punch-out cannot be more than 24 hours after punch-in",
                )

        if not any(record.get(key) != value for key, value in changes.items()):
            raise HTTPException(
                status_code=422,
                detail="Correction does not change the attendance record",
            )

        updated_fields = {}
        for key, value in changes.items():
            if record.get(key) != value:
                updated_fields[key] = value

        effective_status = new_status
        if effective_status in ("ABSENT", "LEAVE"):
            updated_fields.update({
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            })
        else:
            updated_fields["late_minutes"] = (
                calculate_late_minutes(
                    new_punch_in,
                    date,
                    employee["shift_start"],
                ) if new_punch_in is not None else 0
            )

            if new_punch_in is not None and new_punch_out is not None:
                work_hours = calculate_work_hours(new_punch_in, new_punch_out)
                updated_fields["work_hours"] = work_hours
                updated_fields["half_day"] = work_hours < 4.50
                updated_fields["overtime_minutes"] = calculate_overtime_minutes(
                    new_punch_out,
                    date,
                    employee["shift_start"],
                    employee["shift_end"],
                )
            else:
                updated_fields["work_hours"] = None
                updated_fields["half_day"] = False
                updated_fields["overtime_minutes"] = 0

        history_entry = {
            "at": utc_now_seconds(),
            "by": payload.regularized_by,
            "reason": payload.reason,
            "changes": {
                key: {
                    "from": record.get(key),
                    "to": updated_fields.get(key, record.get(key)),
                }
                for key in updated_fields
                if record.get(key) != updated_fields.get(key, record.get(key))
            },
        }

        if not history_entry["changes"]:
            raise HTTPException(
                status_code=422,
                detail="Correction does not change the attendance record",
            )

        # A single atomic update appends history and applies the correction.
        updated = db.attendance_logs.find_one_and_update(
            {
                **query,
                **{
                    key: record.get(key)
                    for key in ("status", "punch_in", "punch_out")
                    if key in record
                },
            },
            {
                "$set": updated_fields,
                "$push": {"history": history_entry},
            },
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise HTTPException(
                status_code=409,
                detail="Attendance changed concurrently; retry the correction",
            )

        return attendance_response(updated)

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# EMPLOYEE MONTHLY ANALYTICS
# ============================================================


def build_employee_monthly_pipeline(emp_code, month_start, next_month):
    """Build the exact aggregation used by monthly employee analytics."""
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    try:
        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        year, month_number = map(int, month.split("-"))
        if not 1 <= month_number <= 12:
            raise HTTPException(
                status_code=422,
                detail="month must be a valid YYYY-MM value",
            )

        month_start = date(year, month_number, 1)
        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)

        month_end = next_month - timedelta(days=1)

        joined_on = date.fromisoformat(employee["joined_on"])
        effective_start = max(month_start, joined_on)

        working_days = 0
        current_day = effective_start
        while current_day <= month_end:
            if current_day.weekday() < 5:
                working_days += 1
            current_day += timedelta(days=1)

        pipeline = build_employee_monthly_pipeline(
            emp_code, month_start, next_month
        )

        results = list(db.attendance_logs.aggregate(pipeline))
        metrics = results[0] if results else {}

        present_days = float(metrics.get("present_days", 0.0))
        attendance_pct = (
            float(
                (
                    Decimal(str(present_days * 100 / working_days))
                    .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                )
            )
            if working_days > 0
            else None
        )

        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": present_days,
            "leave_days": metrics.get("leave_days", 0),
            "late_count": metrics.get("late_count", 0),
            "total_late_minutes": metrics.get("total_late_minutes", 0),
            "total_overtime_minutes": metrics.get("total_overtime_minutes", 0),
            "attendance_pct": attendance_pct,
        }

    except HTTPException:
        raise
    except (ValueError, OverflowError):
        raise HTTPException(
            status_code=422,
            detail="month must be a valid YYYY-MM value",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# DEPARTMENT MONTHLY SUMMARY
# ============================================================


def build_department_summary_pipeline(month_start, next_month, department):
    """Build the aggregation used by department summary and explain."""
    month_end = next_month - timedelta(days=1)
    employee_filter = {
        "joined_on": {"$lte": month_end.isoformat()}
    }
    if department is not None:
        employee_filter["department"] = department

    pipeline = [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance_summary",
            }
        },
        {
            "$unwind": {
                "path": "$attendance_summary",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$ifNull": ["$attendance_summary.present_days", 0]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$ifNull": ["$attendance_summary.work_hours_sum", 0]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$ifNull": ["$attendance_summary.work_hours_count", 0]
                    }
                },
                "late_count": {
                    "$sum": {"$ifNull": ["$attendance_summary.late_count", 0]}
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$attendance_summary.total_late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {"$ifNull": ["$attendance_summary.leave_count", 0]}
                },
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$attendance_summary.on_duty_count", 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    try:
        year, month_number = map(int, month.split("-"))

        if not 1 <= month_number <= 12:
            raise HTTPException(
                status_code=422,
                detail="month must be a valid YYYY-MM value",
            )

        month_start = date(year, month_number, 1)
        next_month = (
            date(year + 1, 1, 1)
            if month_number == 12
            else date(year, month_number + 1, 1)
        )
        month_end = next_month - timedelta(days=1)

        pipeline = build_department_summary_pipeline(
            month_start, next_month, department
        )

        results = list(db.employees.aggregate(pipeline))
        items = []

        for row in results:
            hours_count = row["work_hours_count"]
            average_hours = None

            if hours_count:
                average_hours = float(
                    (
                        Decimal(str(row["work_hours_sum"] / hours_count))
                        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    )
                )

            items.append({
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": float(row["present_days"]),
                "avg_work_hours": average_hours,
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            })

        return {"month": month, "items": items}

    except HTTPException:
        raise
    except (ValueError, OverflowError):
        raise HTTPException(
            status_code=422,
            detail="month must be a valid YYYY-MM value",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# LATE ARRIVAL LEADERBOARD
# ============================================================

@app.get("/analytics/leaderboard/late")
def late_arrival_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1),
    department: Optional[str] = None,
):
    try:
        year, month_number = map(int, month.split("-"))

        if not 1 <= month_number <= 12:
            raise HTTPException(
                status_code=422,
                detail="month must be a valid YYYY-MM value",
            )

        month_start = date(year, month_number, 1)
        next_month = (
            date(year + 1, 1, 1)
            if month_number == 12
            else date(year, month_number + 1, 1)
        )

        employee_filter = {}
        if department is not None:
            employee_filter["department"] = department

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if employee_filter:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        items = list(db.attendance_logs.aggregate(pipeline))

        return {
            "month": month,
            "limit": limit,
            "items": items,
        }

    except HTTPException:
        raise
    except (ValueError, OverflowError):
        raise HTTPException(
            status_code=422,
            detail="month must be a valid YYYY-MM value",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# DEPARTMENT DAILY ATTENDANCE TREND
# ============================================================


def build_department_trend_pipeline(department, from_date, to_date):
    """Build the shared department trend aggregation pipeline."""
    start_text = from_date.isoformat()
    end_text = to_date.isoformat()

    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": end_text},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_text]},
                                    {"$lte": ["$date", end_text]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {"$unwind": "$daily"},
        {
            "$group": {
                "_id": "$daily._id",
                "present_count": {"$sum": "$daily.present_count"},
                "late_count": {"$sum": "$daily.late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$set": {
                "date_value": {
                    "$dateFromString": {"dateString": "$date"}
                }
            }
        },
        {
            "$densify": {
                "field": "date_value",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(from_date, time.min),
                        datetime.combine(to_date, time.min) + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_value",
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_value"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_value"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    # Seed the range so densify still works when no logs exist.
    pipeline.insert(5, {
        "$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$match": {"department": department, "joined_on": {"$lte": end_text}}},
                {"$limit": 1},
                {"$project": {
                    "_id": 0,
                    "date": {"$literal": start_text},
                    "present_count": {"$literal": 0},
                    "late_count": {"$literal": 0},
                }},
            ],
        }
    })
    pipeline.insert(6, {
        "$group": {
            "_id": "$date",
            "present_count": {"$sum": "$present_count"},
            "late_count": {"$sum": "$late_count"},
        }
    })
    pipeline.insert(7, {"$project": {
        "_id": 0, "date": "$_id", "present_count": 1, "late_count": 1
    }})
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    try:
        if to_date < from_date:
            raise HTTPException(
                status_code=422,
                detail="'to' cannot be before 'from'",
            )

        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if not db.employees.find_one({"department": department}):
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        start_text = from_date.isoformat()
        end_text = to_date.isoformat()

        pipeline = build_department_trend_pipeline(
            department, from_date, to_date
        )

        items = list(db.employees.aggregate(pipeline))

        return {
            "department": department,
            "from": start_text,
            "to": end_text,
            "items": items,
        }

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

# ============================================================
# ADMIN QUERY-PLAN DIAGNOSTICS
# ============================================================

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    try:
        collection_name = "attendance_logs"
        explain_command = None

        if endpoint == "attendance_list":
            filters = {}

            if emp_code is not None:
                filters["emp_code"] = emp_code

            if status is not None:
                if status not in ALL_STATUSES:
                    raise HTTPException(
                        status_code=422,
                        detail="Invalid attendance status",
                    )
                filters["status"] = status

            if date_from is not None and date_to is not None:
                if date_from > date_to:
                    raise HTTPException(
                        status_code=422,
                        detail="'date_from' cannot be after 'date_to'",
                    )

            date_filter = {}
            if date_from is not None:
                date_filter["$gte"] = date_from.isoformat()
            if date_to is not None:
                date_filter["$lte"] = date_to.isoformat()
            if date_filter:
                filters["date"] = date_filter

            explain_command = {
                "find": "attendance_logs",
                "filter": filters,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            }

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(
                    status_code=422,
                    detail="'emp_code' and 'month' are required",
                )

            try:
                year, month_number = map(int, month.split("-"))
                if not 1 <= month_number <= 12 or len(month) != 7:
                    raise ValueError()
                month_start = date(year, month_number, 1)
                next_month = (
                    date(year + 1, 1, 1)
                    if month_number == 12
                    else date(year, month_number + 1, 1)
                )
            except (ValueError, OverflowError):
                raise HTTPException(
                    status_code=422,
                    detail="'month' must use YYYY-MM format",
                )

            pipeline = build_employee_monthly_pipeline(
                emp_code, month_start, next_month
            )

            explain_command = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(
                    status_code=422,
                    detail="'month' is required",
                )

            try:
                year, month_number = map(int, month.split("-"))
                if not 1 <= month_number <= 12 or len(month) != 7:
                    raise ValueError()
                month_start = date(year, month_number, 1)
                next_month = (
                    date(year + 1, 1, 1)
                    if month_number == 12
                    else date(year, month_number + 1, 1)
                )
            except (ValueError, OverflowError):
                raise HTTPException(
                    status_code=422,
                    detail="'month' must use YYYY-MM format",
                )

            pipeline = build_department_summary_pipeline(
                month_start, next_month, department
            )
            collection_name = "employees"
            explain_command = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(
                    status_code=422,
                    detail="'month' is required",
                )

            try:
                year, month_number = map(int, month.split("-"))
                if not 1 <= month_number <= 12 or len(month) != 7:
                    raise ValueError()
                month_start = date(year, month_number, 1)
                next_month = (
                    date(year + 1, 1, 1)
                    if month_number == 12
                    else date(year, month_number + 1, 1)
                )
            except (ValueError, OverflowError):
                raise HTTPException(
                    status_code=422,
                    detail="'month' must use YYYY-MM format",
                )

            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": month_start.isoformat(),
                            "$lt": next_month.isoformat(),
                        },
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
            ]

            if department is not None:
                pipeline.append(
                    {"$match": {"employee.department": department}}
                )

            pipeline.extend([
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
            ])

            explain_command = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }

        elif endpoint == "department_trend":
            if not department or from_date is None or to_date is None:
                raise HTTPException(
                    status_code=422,
                    detail="'department', 'from', and 'to' are required",
                )

            if to_date < from_date:
                raise HTTPException(
                    status_code=422,
                    detail="'to' cannot be before 'from'",
                )

            if (to_date - from_date).days + 1 > 92:
                raise HTTPException(
                    status_code=422,
                    detail="Date range cannot exceed 92 days",
                )

            pipeline = build_department_trend_pipeline(
                department, from_date, to_date
            )

            collection_name = "employees"
            explain_command = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }

        result = db.command(
            "explain",
            explain_command,
            verbosity="executionStats",
        )

        from bson.json_util import dumps as bson_dumps
        import json
        return json.loads(
            bson_dumps({
                "endpoint": endpoint,
                "collection": collection_name,
                "explain": result,
            })
        )

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Unable to obtain MongoDB execution statistics",
        )
