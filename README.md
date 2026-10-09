# HROne Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the Employee Attendance & Analytics assignment.

## Run locally

1. Use Python 3.11+ and MongoDB 6.0+.
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   # Windows PowerShell: .venv\\Scripts\\Activate.ps1
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Set `MONGO_URI` and `MONGO_DB` in your shell (or a local `.env` file, which must not be committed).
4. Start the service from the repository root:
   ```bash
   uvicorn app.main:app --port 8000
   ```
5. Check `GET http://localhost:8000/health` and open `/docs` for interactive API docs.

Indexes are created idempotently at startup. The app uses only MongoDB for external service access.

## Important submission note

The supplied upload did not include the original `app/main.py` starter, `sample_data/` JSON files, or a complete candidate-kit directory. The API implementation was built against the uploaded OpenAPI contract and data model. The starter-specific code review in `REVIEW.md` therefore remains incomplete until the original starter is reviewed. Run the automated tests against MongoDB 7.0 and check every endpoint before submitting.
