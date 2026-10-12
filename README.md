# HROne Attendance & Analytics API

A FastAPI and MongoDB backend for employee management, attendance tracking, attendance corrections, and workforce analytics.

## Requirements

- Python 3.11+
- MongoDB
- Dependencies listed in `requirements.txt`

## Setup

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Configure a local `.env` file:

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hrone_attendance
```

Replace the MongoDB URI if your database uses a different connection string. Never commit `.env` or credentials.

## Run the API

```powershell
uvicorn app.main:app --reload --port 8000
```

Open the interactive API documentation at http://127.0.0.1:8000/docs.

## Main endpoints

- `GET /health` - health check
- `POST /employees` and `GET /employees` - employee management
- `POST /attendance/punch-in` - record punch-in
- `POST /attendance/punch-out` - record punch-out
- `GET /attendance` - query attendance records
- `GET /attendance/{emp_code}/{date}` - retrieve an employee's attendance
- `PATCH /attendance/{emp_code}/{date}` - correct attendance
- `GET /analytics/employees/{emp_code}/monthly` - monthly employee analytics
- `GET /analytics/departments/summary` - department summary
- `GET /analytics/departments/{department}/trend` - department trend
- `GET /analytics/leaderboard/late` - late-arrival leaderboard
- `GET /admin/explain/{endpoint}` - query execution explanations

See `openapi.yaml` and `/docs` for endpoint schemas and parameters.

## Tests

Run:

```powershell
python -m pytest -q
```

The current automated tests cover date-range validation. MongoDB integration, concurrent requests, analytics results, and query execution plans need additional verification.

## Repository hygiene

Keep `.env`, virtual environments, logs, and local data out of Git. Do not commit database credentials or employee records.
