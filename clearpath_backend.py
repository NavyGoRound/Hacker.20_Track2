"""
ClearPath Backend (FastAPI) — Privacy-First, Consent-Gated Scholarship Co-Pilot
Aligned with ClearPath Ideathon Track 2 architecture:
- Ephemeral in-memory sessions (zero disk/DB retention; wiped on receipt/expiry)
- Scheme Playbooks + Official-site check
- Per-task risk-tagged consent ("Allow once" / "Skip" / strictly manual OTP)
- Sandboxed form preparation with hard stop before final submit
- Plain-language Privacy Receipt generation
"""

from datetime import datetime, timezone
import secrets
import time
from typing import Dict, List, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="ClearPath Backend",
    description="Consent-gated, zero-retention co-pilot API for scholarship portals.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_TTL_SECONDS = 900  # 15-minute ephemeral session window

# --- Scheme Playbooks (3 Round-2 MVP Playbooks) ---
SCHEMES = [
    {
        "id": "merit",
        "name": "Merit-cum-Means Scholarship",
        "amt": "₹20,000 / year",
        "due": "Due 31 Dec",
        "note": "For students with strong marks and a family income below the limit",
        "official_domain": "scholarship-portal.example",
        "traps_detected": [
            "Optional newsletter opt-in pre-checked on step 2 (ClearPath leaves unchecked)"
        ],
    },
    {
        "id": "tech",
        "name": "Technical Education Scholarship",
        "amt": "₹30,000 / year",
        "due": "Due 15 Jan",
        "note": "For engineering and diploma students",
        "official_domain": "scholarship-portal.example",
        "traps_detected": [],
    },
    {
        "id": "first",
        "name": "First-Generation Learner Grant",
        "amt": "₹10,000 / year",
        "due": "Due 20 Jan",
        "note": "For the first graduate in a family",
        "official_domain": "scholarship-portal.example",
        "traps_detected": [],
    },
]

# --- Consent-Gated Task Card Definitions ---
TASKS = [
    {
        "id": "profile",
        "title": "Fill personal and course details",
        "reads": "Reads: your saved student profile",
        "risk": "low",
        "manual": False,
        "field": "Personal and course details",
    },
    {
        "id": "income",
        "title": "Attach income certificate",
        "reads": "Reads: one PDF that you pick from your Documents",
        "risk": "medium",
        "manual": False,
        "field": "Income certificate",
    },
    {
        "id": "bank",
        "title": "Fill bank account details",
        "reads": "Reads: account number and IFSC from your saved bank profile",
        "risk": "high",
        "manual": False,
        "field": "Bank account",
    },
    {
        "id": "otp",
        "title": "Aadhaar OTP verification",
        "reads": "Reads: nothing. ClearPath never sees your OTP",
        "risk": "high",
        "manual": True,
        "field": "Aadhaar OTP",
    },
]

# Sample in-memory sandboxed profile (only read per-task if user grants "allowed")
SAMPLE_VAULT = {
    "profile": "Student Name · B.E. Computer Science",
    "income": "income_certificate.pdf attached",
    "bank": "A/C ending 1234 · IFSC filled",
}

# Ephemeral session store (RAM only — never written to disk)
SESSIONS: Dict[str, dict] = {}


class StartSessionRequest(BaseModel):
    scheme_id: str


class ConsentRequest(BaseModel):
    task_id: str
    decision: Literal["allowed", "skipped"]


class PrepareRequest(BaseModel):
    # Optional overrides if user supplies real local values for this session only
    custom_values: Optional[Dict[str, str]] = None


def _purge_expired_sessions() -> None:
    now = time.time()
    expired = [sid for sid, s in SESSIONS.items() if now - s["created_at"] > SESSION_TTL_SECONDS]
    for sid in expired:
        SESSIONS.pop(sid, None)


def _get_session(session_id: str) -> dict:
    _purge_expired_sessions()
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired (zero-retention wipe).")
    return session


@app.get("/api/schemes")
def list_schemes():
    return {"schemes": SCHEMES}


@app.get("/api/check-site")
def check_official_site(url: str):
    """Official-site check & dark-pattern warning before any task begins."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    official_hosts = {s["official_domain"] for s in SCHEMES}
    is_official = host in official_hosts
    return {
        "url": url,
        "domain": host,
        "is_official": is_official,
        "warning": None if is_official else "Unverified domain. Do not enter Aadhaar or bank details.",
    }


@app.post("/api/session/start")
def start_session(req: StartSessionRequest):
    _purge_expired_sessions()
    scheme = next((s for s in SCHEMES if s["id"] == req.scheme_id), None)
    if not scheme:
        raise HTTPException(status_code=404, detail="Unknown scheme_id")

    session_id = "sess_" + secrets.token_hex(6)
    SESSIONS[session_id] = {
        "session_id": session_id,
        "created_at": time.time(),
        "scheme": scheme,
        "decisions": {"otp": "manual"},  # OTP is strictly manual by design
        "prepared_fields": None,
        "receipt": None,
    }
    return {
        "session_id": session_id,
        "scheme": scheme,
        "tasks": TASKS,
        "decisions": SESSIONS[session_id]["decisions"],
    }


@app.post("/api/session/{session_id}/consent")
def set_task_consent(session_id: str, req: ConsentRequest):
    session = _get_session(session_id)
    task = next((t for t in TASKS if t["id"] == req.task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Unknown task_id")
    if task["manual"]:
        raise HTTPException(
            status_code=400,
            detail="Manual tasks (like Aadhaar OTP) cannot be delegated to ClearPath.",
        )

    session["decisions"][req.task_id] = req.decision
    all_decided = all(t["id"] in session["decisions"] for t in TASKS)
    return {
        "session_id": session_id,
        "decisions": session["decisions"],
        "ready_to_prepare": all_decided,
    }


@app.post("/api/session/{session_id}/prepare")
def prepare_application(session_id: str, req: Optional[PrepareRequest] = None):
    """Prepares only explicitly allowed fields and enforces a hard stop before submit."""
    session = _get_session(session_id)
    if not all(t["id"] in session["decisions"] for t in TASKS):
        raise HTTPException(status_code=400, detail="All tasks must be reviewed before preparation.")

    custom = (req.custom_values if req and req.custom_values else {})
    prepared: List[dict] = []

    for task in TASKS:
        tid = task["id"]
        decision = session["decisions"].get(tid)
        if task["manual"]:
            prepared.append({
                "task_id": tid,
                "field": task["field"],
                "status": "manual",
                "value": "You will enter the OTP here",
            })
        elif decision == "allowed":
            val = custom.get(tid, SAMPLE_VAULT.get(tid, "Prepared"))
            prepared.append({
                "task_id": tid,
                "field": task["field"],
                "status": "filled",
                "value": val,
            })
        else:
            prepared.append({
                "task_id": tid,
                "field": task["field"],
                "status": "empty",
                "value": "Empty. Waiting for you",
            })

    session["prepared_fields"] = prepared
    return {
        "session_id": session_id,
        "scheme": session["scheme"]["name"],
        "portal_url": f"https://{session['scheme']['official_domain']}/apply",
        "fields": prepared,
        "stopped_before_submit": True,
        "notice": "ClearPath has stopped here. Nothing is sent to the portal until you press Submit yourself.",
    }


@app.post("/api/session/{session_id}/submit")
def record_user_submit_and_wipe(session_id: str):
    """
    Called when the user presses Submit themselves.
    Generates the Privacy Receipt and immediately wipes sensitive prepared data from RAM.
    """
    session = _get_session(session_id)
    receipt_id = "CP-" + secrets.token_hex(3).upper()
    timestamp = datetime.now(timezone.utc).isoformat()

    allowed = [
        f"{t['field']} ({t['reads'].replace('Reads: ', '')})"
        for t in TASKS
        if session["decisions"].get(t["id"]) == "allowed"
    ]
    not_touched = [
        f"{t['field']}. Done by you" if t["manual"] else f"{t['field']}. You skipped this"
        for t in TASKS
        if session["decisions"].get(t["id"]) != "allowed"
    ]

    receipt = {
        "receipt_id": receipt_id,
        "timestamp": timestamp,
        "scheme": session["scheme"]["name"],
        "accessed_with_permission": allowed or ["Nothing"],
        "not_touched": not_touched or ["Nothing"],
        "final_submit": "Pressed by you, not by ClearPath",
        "kept_after_session": "Nothing",
    }

    # Zero-retention wipe: clear prepared form data from memory
    session["prepared_fields"] = None
    session["receipt"] = receipt
    return receipt


@app.delete("/api/session/{session_id}")
def wipe_session(session_id: str):
    SESSIONS.pop(session_id, None)
    return {"status": "wiped", "kept_after_session": "Nothing"}