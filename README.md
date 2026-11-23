# AI-Guarded Student Database Management System

A JSON-backed Student Database Management System wrapped with strict JSON Schema tool validation, blind rubric QA gating, SHA-256 hash-bound human approvals, and idempotent database writes.

## Architecture

1. **Student DBMS & Tool Registry (`student_db.py`)**: Stores student records (`name`, `roll_number`, `course`, `marks`) in `students.json`, validates course codes and `0..100` mark bounds, and exposes read queries through a typed `ToolRegistry`.
2. **Blind QA Rubric Gate (`qa_gate.py`)**: Evaluates agent-generated summaries against `students.json` sources across four criteria (`facts`, `policy`, `fit`, `clarity`) using an independent reviewer with one targeted rewrite.
3. **Hash-Bound Approval & Security (`security.py`)**: Enforces fail-fast `StartupError` checks in production, scrubs API keys and bearer tokens across all log handlers, and requires human approval matching the SHA-256 payload digest before any `add_student`, `update_student`, or `delete_student` mutation executes.

## Run locally

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    uvicorn app:app --reload

Visit `http://localhost:8000` for the web UI or `http://localhost:8000/health` for the readiness check.

## Configuration

| Variable | Required in Production | Default | Description |
| :--- | :--- | :--- | :--- |
| `APP_ENV` | Yes (`production`) | `development` | Enforces production startup checks when set to `production` |
| `DEBUG` | Must be `false` | `false` | Blocked when `APP_ENV=production` |
| `ANTHROPIC_API_KEY` | Yes | *(empty -> demo mode)* | Anthropic API key for live writer & reviewer models |
| `MCP_AUTH_TOKEN` | Yes | *(empty)* | Authentication token required in production |
| `ALLOWED_ORIGINS` | Yes (no `*`) | *(empty)* | Comma-separated allowed CORS origins |
| `REQUEST_TIMEOUT_S` | No | `30.0` | Upstream API timeout in seconds |

## Demo

Live interactive demo: https://student-dbms-agent.onrender.com

1. Inspect live student records, average marks, and top performers loaded from `students.json`.
2. Click **Step 1: Run Tool Lookup & QA Gate** to watch the blind reviewer catch an unverified draft, trigger one rewrite, and stage the database mutation with a SHA-256 hash.
3. Click **Step 2: Approve Proposal (Hash-Bound)** and **Step 3: Execute Write Once** to apply the change idempotently.

## License

MIT

