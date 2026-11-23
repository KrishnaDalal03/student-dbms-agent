import json
import logging
import re
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from qa_gate import run_qa_gate
from security import ApprovalError, ApprovalQueue, install_log_redaction, load_settings, redact
from student_db import StudentDatabase, ToolRegistry, VALID_COURSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
install_log_redaction()
logger = logging.getLogger("student-dbms-agent")

SETTINGS = load_settings()
DB = StudentDatabase()

REGISTRY = ToolRegistry()
REGISTRY.register(DB.view_students)
REGISTRY.register(DB.search_by_rollnumber)
REGISTRY.register(DB.calculate_average_marks)
REGISTRY.register(DB.show_top_performers)
REGISTRY.register(DB.sort_by_marks)
REGISTRY.register(DB.search_by_course_sorted)

EXECUTED_KEYS: dict[str, dict] = {}


def execute_db_mutation(payload: dict, idempotency_key: str) -> dict:
    """Idempotent database mutation executor (deduped by idempotency_key)."""
    if idempotency_key in EXECUTED_KEYS:
        return EXECUTED_KEYS[idempotency_key]

    op = payload["operation"]
    args = payload["arguments"]
    if op == "add_student":
        res = DB.add_student(**args)
    elif op == "update_student":
        res = DB.update_student(**args)
    elif op == "delete_student":
        res = DB.delete_student(**args)
    else:
        raise ValueError(f"Unsupported operation: {op}")

    EXECUTED_KEYS[idempotency_key] = res
    logger.info("Executed approved DB mutation key=%s op=%s", idempotency_key, op)
    return res


QUEUE = ApprovalQueue(executor_fn=execute_db_mutation)

app = FastAPI(title="AI-Guarded Student DBMS", version="1.0.0")
if SETTINGS["allowed_origins"]:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=SETTINGS["allowed_origins"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def make_writer_and_reviewer():
    api_key = SETTINGS["api_key"]
    if api_key:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key.reveal(), timeout=SETTINGS["timeout_s"])

        def live_writer(task: str, feedback: str | None = None) -> str:
            prompt = f"Summarize the student record findings and proposed database change.\nTask: {task}"
            if feedback:
                prompt += f"\nFix these QA notes: {feedback}"
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content.text.strip()

        def live_reviewer(draft: str, rubric: dict, sources: list[str]) -> dict:
            prompt = (
                "Score this draft 1-10 on each criterion using ONLY the database sources. "
                'Return strict JSON: {"scores": {"facts": int, "policy": int, "fit": int, "clarity": int}, "notes": str}.\n'
                f"Rubric: {json.dumps(rubric['criteria'])}\nSources: {json.dumps(sources)}\nDraft: {draft}"
            )
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=250,
                messages=[{"role": "user", "content": prompt}],
            )
            return json.loads(msg.content.text.strip())

        return live_writer, live_reviewer

    def demo_writer(task: str, feedback: str | None = None) -> str:
        if feedback is None:
            return "Student marks updated to 100% automatically without checking records."
        return (
            "Verified student records from students.json. "
            "Prepared the requested database change for human review before writing to disk."
        )

    def demo_reviewer(draft: str, rubric: dict, sources: list[str]) -> dict:
        if "100% automatically" in draft:
            return {
                "scores": {"facts": 3, "policy": 2, "fit": 7, "clarity": 8},
                "notes": "Draft claims an unverified 100% update and bypasses human approval policy.",
            }
        return {
            "scores": {"facts": 10, "policy": 10, "fit": 9, "clarity": 9},
            "notes": "Grounded in students.json records and properly staged for human approval.",
        }

    return demo_writer, demo_reviewer
class AgentTaskRequest(BaseModel):
    prompt: str = "Update roll number 103 marks to 85"


class ApproveRequest(BaseModel):
    reviewer: str = "krishna"
    seen_hash: str


def parse_mutation_intent(prompt: str) -> dict:
    """Extract a structured mutation proposal from the user's request."""
    lower = prompt.lower()
    roll_match = re.search(r"roll\s*(?:number|no\.?)?\s*(\d+)", lower)
    marks_match = re.search(r"marks?\s*(?:to|=|:)?\s*(\d+)", lower)

    if "delete" in lower or "remove" in lower:
        roll = int(roll_match.group(1)) if roll_match else 103
        return {"operation": "delete_student", "arguments": {"roll_number": roll}}

    if "add" in lower or "create" in lower:
        name_match = re.search(r"(?:named?|student)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)", prompt)
        course_found = next((c for c in VALID_COURSES if c.lower() in lower), "CSE")
        roll = int(roll_match.group(1)) if roll_match else max((s.roll_number for s in DB.students), default=100) + 1
        marks = int(marks_match.group(1)) if marks_match else 85
        name = name_match.group(1) if name_match else "Sneha Patil"
        return {
            "operation": "add_student",
            "arguments": {"name": name, "roll_number": roll, "course": course_found, "marks": marks},
        }

    roll = int(roll_match.group(1)) if roll_match else 103
    marks = int(marks_match.group(1)) if marks_match else 85
    return {"operation": "update_student", "arguments": {"roll_number": roll, "marks": marks}}


@app.get("/health")
def health_endpoint():
    checks = {
        "students_json": lambda: isinstance(DB.students, list),
        "approval_queue": lambda: isinstance(QUEUE.actions, dict),
    }
    results = {}
    all_ok = True
    for name, fn in checks.items():
        try:
            ok = bool(fn())
            results[name] = "ok" if ok else "fail: returned False"
            all_ok = all_ok and ok
        except Exception as exc:
            results[name] = f"fail: {type(exc).__name__}"
            all_ok = False
    code = 200 if all_ok else 503
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if all_ok else "degraded", "version": app.version, "checks": results},
    )


@app.get("/api/students")
def list_students():
    return {
        "students": DB.sort_by_marks(),
        "stats": DB.calculate_average_marks(),
        "top_performers": DB.show_top_performers(),
        "mcp_tools": REGISTRY.list_tools(),
    }


@app.post("/api/agent/run")
def run_agent(req: AgentTaskRequest):
    safe_prompt = redact(req.prompt)
    tool_calls = [
        {"tool": "view_students", "output": REGISTRY.call_tool("view_students", {})},
        {"tool": "calculate_average_marks", "output": REGISTRY.call_tool("calculate_average_marks", {})},
    ]
    sources = [c["output"]["content"] for c in tool_calls]

    writer, reviewer = make_writer_and_reviewer()
    qa_result = run_qa_gate(safe_prompt, writer, reviewer, sources, threshold=8)

    if qa_result["status"] != "approved":
        return {"stage": "held_by_qa", "tool_calls": tool_calls, "qa": qa_result}

    proposal = parse_mutation_intent(safe_prompt)
    proposal["summary"] = qa_result["draft"]
    action_id = QUEUE.propose(proposal, by="dbms-agent")

    return {
        "stage": "awaiting_human_approval",
        "tool_calls": tool_calls,
        "qa": qa_result,
        "action": QUEUE.actions[action_id],
        "audit": QUEUE.audit,
    }


@app.post("/api/actions/{action_id}/approve")
def approve_action(action_id: str, req: ApproveRequest):
    try:
        action = QUEUE.approve(action_id, by=req.reviewer, seen_hash=req.seen_hash)
        return {"action": action, "audit": QUEUE.audit}
    except ApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/actions/{action_id}/execute")
def execute_action(action_id: str):
    try:
        result = QUEUE.execute(action_id, by="executor")
        return {
            "result": result,
            "students": DB.sort_by_marks(),
            "stats": DB.calculate_average_marks(),
            "audit": QUEUE.audit,
        }
    except ApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
def index():
    html_parts = [
        "<!doctype html><html><head><meta charset='utf-8' />",
        "<title>AI-Guarded Student DBMS — Capstone Demo</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#111}",
        "input,button{padding:.6rem .9rem;font-size:.95rem}",
        "input{width:100%;box-sizing:border-box;margin:.4rem 0 .8rem}",
        "button{cursor:pointer;margin-right:.5rem;margin-bottom:.5rem}",
        "table{width:100%;border-collapse:collapse;margin-top:.6rem}",
        "th,td{border:1px solid #ddd;padding:.5rem;text-align:left}th{background:#f4f4f6}",
        "pre{background:#f4f4f6;padding:1rem;overflow-x:auto;border-radius:6px;font-size:.83rem}",
        ".card{border:1px solid #ddd;border-radius:8px;padding:1.2rem;margin-bottom:1.2rem}",
        "</style></head><body>",
        "<h1>AI-Guarded Student Database Management System</h1>",
        "<p>End-to-end flow: <strong>MCP Read Tools &rarr; Blind QA Rubric Gate &rarr; Hash-Bound Approval &rarr; Idempotent Write</strong>.</p>",
        "<div class='card'><h3>1. Current Student Records (<code>students.json</code>)</h3>",
        "<div id='stats'>Loading...</div>",
        "<table><thead><tr><th>Roll No</th><th>Name</th><th>Course</th><th>Marks</th></tr></thead>",
        "<tbody id='tbody'></tbody></table></div>",
        "<div class='card'><h3>2. Propose Database Change via AI Agent</h3>",
        "<label>Instruction (e.g., <code>Update roll number 103 marks to 85</code>, <code>Add student Sneha Patil roll 105 course CSE marks 91</code>, or <code>Delete roll number 103</code>)</label>",
        "<input id='prompt' value='Update roll number 103 marks to 85' />",
        "<button onclick='runAgent()'>Step 1: Run Tool Lookup &amp; QA Gate</button>",
        "<button id='approveBtn' onclick='approveAction()' disabled>Step 2: Approve Proposal (Hash-Bound)</button>",
        "<button id='execBtn' onclick='executeAction()' disabled>Step 3: Execute Write Once</button></div>",
        "<pre id='out'>Click Step 1 to inspect the full trace...</pre>",
        "<script>",
        "let currentAction=null;",
        "async function loadStudents(){const r=await fetch('/api/students');const d=await r.json();",
        "document.getElementById('stats').textContent=`Total Students: ${d.stats.count} | Average Marks: ${d.stats.average_marks}`;",
        "document.getElementById('tbody').innerHTML=d.students.map(s=>`<tr><td>${s.roll_number}</td><td>${s.name}</td><td>${s.course}</td><td>${s.marks}</td></tr>`).join('');}",
        "async function runAgent(){const r=await fetch('/api/agent/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:document.getElementById('prompt').value})});",
        "const d=await r.json();document.getElementById('out').textContent=JSON.stringify(d,null,2);",
        "if(d.action){currentAction=d.action;document.getElementById('approveBtn').disabled=false;document.getElementById('execBtn').disabled=true;}}",
        "async function approveAction(){const r=await fetch(`/api/actions/${currentAction.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:'krishna',seen_hash:currentAction.hash})});",
        "const d=await r.json();document.getElementById('out').textContent=JSON.stringify(d,null,2);if(r.ok)document.getElementById('execBtn').disabled=false;}",
        "async function executeAction(){const r=await fetch(`/api/actions/${currentAction.id}/execute`,{method:'POST'});",
        "const d=await r.json();document.getElementById('out').textContent=JSON.stringify(d,null,2);await loadStudents();}",
        "loadStudents();</script></body></html>",
    ]
    return "\n".join(html_parts)
