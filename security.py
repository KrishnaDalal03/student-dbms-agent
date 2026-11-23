import copy
import hashlib
import json
import logging
import math
import os
import re
import time


class StartupError(Exception):
    """Raised when production startup invariants fail."""


class ApprovalError(Exception):
    """Raised when an unapproved, tampered, or expired mutation is executed."""


class Secret:
    """Masks sensitive values in logs and tracebacks."""

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('********')"

    __str__ = __repr__


_REDACT_PATTERN = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{8,}|(?i:bearer)\s+[A-Za-z0-9._-]{16,}"
)


def redact(text: str) -> str:
    return _REDACT_PATTERN.sub("[redacted]", str(text))


class RedactingFilter(logging.Filter):
    """Attaches to every log handler at startup to scrub key-shaped strings."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def install_log_redaction():
    root = logging.getLogger()
    redactor = RedactingFilter()
    for handler in root.handlers:
        handler.addFilter(redactor)


def load_settings(env: dict | None = None) -> dict:
    env = dict(os.environ) if env is None else env
    errors = []

    app_env = (env.get("APP_ENV") or "development").strip()
    if app_env not in ("development", "production"):
        errors.append("APP_ENV: must be 'development' or 'production'")

    raw_debug = (env.get("DEBUG") or "false").strip().lower()
    if raw_debug in ("true", "1", "yes"):
        debug = True
    elif raw_debug in ("false", "0", "no", ""):
        debug = False
    else:
        debug = False
        errors.append("DEBUG: must be true/false/1/0")

    raw_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    api_key = Secret(raw_key) if raw_key else None

    raw_token = (env.get("MCP_AUTH_TOKEN") or "").strip()
    mcp_token = Secret(raw_token) if raw_token else None

    allowed_origins = (env.get("ALLOWED_ORIGINS") or "").strip()

    if app_env == "production":
        if debug:
            errors.append("DEBUG: must be off in production")
        if not api_key:
            errors.append("ANTHROPIC_API_KEY: required in production")
        if not mcp_token:
            errors.append("MCP_AUTH_TOKEN: required in production")
        if not allowed_origins or "*" in allowed_origins:
            errors.append("ALLOWED_ORIGINS: required in production without '*'")

    raw_timeout = (env.get("REQUEST_TIMEOUT_S") or "30.0").strip()
    try:
        timeout_s = float(raw_timeout)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            errors.append("REQUEST_TIMEOUT_S: must be > 0")
    except ValueError:
        timeout_s = 30.0
        errors.append("REQUEST_TIMEOUT_S: must be a valid number")

    if errors and app_env != "development":
        raise StartupError("Startup blocked:\n- " + "\n- ".join(errors))

    return {
        "app_env": app_env,
        "debug": debug,
        "api_key": api_key,
        "mcp_token": mcp_token,
        "allowed_origins": [o.strip() for o in allowed_origins.split(",") if o.strip()],
        "timeout_s": timeout_s,
        "warnings": errors,
    }


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ApprovalQueue:
    """Hash-bound human approval queue with idempotent execution and audit trail."""

    def __init__(self, executor_fn, clock=time.time, ttl: float = 86400):
        self.executor_fn = executor_fn
        self.clock = clock
        self.ttl = ttl
        self.actions: dict[str, dict] = {}
        self.audit: list[dict] = []

    def propose(self, payload: dict, by: str = "dbms-agent") -> str:
        action_id = f"a{len(self.actions) + 1}"
        payload_copy = copy.deepcopy(payload)
        self.actions[action_id] = {
            "id": action_id,
            "payload": payload_copy,
            "hash": digest(payload_copy),
            "status": "proposed",
            "approved_hash": None,
            "approved_at": None,
            "result": None,
        }
        self.audit.append({"action": action_id, "event": "proposed", "by": by})
        return action_id

    def approve(self, action_id: str, by: str, seen_hash: str) -> dict:
        action = self.actions.get(action_id)
        if not action:
            raise ApprovalError(f"Unknown action: {action_id}")
        if action["status"] != "proposed":
            raise ApprovalError(f"Action is '{action['status']}', expected 'proposed'")
        if seen_hash != digest(action["payload"]):
            raise ApprovalError("Payload changed after reviewer inspected it")

        action["status"] = "approved"
        action["approved_hash"] = seen_hash
        action["approved_at"] = self.clock()
        self.audit.append({"action": action_id, "event": "approved", "by": by})
        return action

    def reject(self, action_id: str, by: str, reason: str) -> dict:
        action = self.actions.get(action_id)
        if not action:
            raise ApprovalError(f"Unknown action: {action_id}")
        if action["status"] == "done":
            raise ApprovalError("Cannot reject an already executed action")
        action["status"] = "rejected"
        self.audit.append({"action": action_id, "event": "rejected", "by": by, "reason": reason})
        return action

    def execute(self, action_id: str, by: str = "executor"):
        action = self.actions.get(action_id)
        if not action:
            raise ApprovalError(f"Unknown action: {action_id}")
        if action["status"] == "done":
            return action["result"]
        if action["status"] not in ("approved", "failed"):
            raise ApprovalError(f"Action '{action_id}' is not approved")
        if action["approved_at"] is None or (self.clock() - action["approved_at"]) > self.ttl:
            action["status"] = "expired"
            raise ApprovalError("Approval expired")
        if digest(action["payload"]) != action["approved_hash"]:
            self.audit.append({"action": action_id, "event": "blocked", "by": by})
            raise ApprovalError("Payload tampered after approval")

        try:
            result = self.executor_fn(copy.deepcopy(action["payload"]), idempotency_key=action_id)
        except Exception:
            action["status"] = "failed"
            self.audit.append({"action": action_id, "event": "failed", "by": by})
            raise

        action["status"] = "done"
        action["result"] = result
        self.audit.append({"action": action_id, "event": "executed", "by": by})
        return result
