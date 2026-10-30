"""VHF contact tracing backend (hackathon MVP). SYNTHETIC DATA ONLY.
Run: uvicorn main:app --reload
Gemma 4 only extracts/structures notes. Triage is hardcoded; humans decide.
"""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

load_dotenv()
MODEL = "gemma-4-26b-a4b-it"  # exact model ID; document in README

app = FastAPI(title="VHF Tracer MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_key = os.environ.get("GEMINI_API_KEY", "").strip()
client = genai.Client(api_key=_key, http_options=types.HttpOptions(timeout=60000)) if _key else None

DISPATCH_QUEUE: list[dict] = []  # simulated ambulance queue
WET_SYMPTOMS = {"vomiting", "diarrhea", "diarrhoea", "bleeding", "unexplained bleeding"}
FEVER_C = 38.0


# ---------- Deterministic triage (NO AI) ----------
class Visit(BaseModel):
    contact_id: str
    temp_c: float = Field(ge=30, le=45)
    symptoms: list[str] = []


@app.post("/triage")
def triage(v: Visit):
    syms = {s.strip().lower() for s in v.symptoms}
    reasons = []
    if v.temp_c >= FEVER_C:
        reasons.append(f"temp {v.temp_c} C >= {FEVER_C}")
    if syms & WET_SYMPTOMS:
        reasons.append("wet symptoms: " + ", ".join(sorted(syms & WET_SYMPTOMS)))
    if not reasons:
        return {"alert": False, "status": "contact"}
    event = {
        "contact_id": v.contact_id,
        "reasons": reasons,
        "time": datetime.now(timezone.utc).isoformat(),
        "simulated": True,
    }
    DISPATCH_QUEUE.append(event)
    return {
        "alert": True,
        "status": "suspected_case",
        "action": "STOP interview. Keep >= 1 m distance. Ambulance alert queued (simulated).",
        "event": event,
    }


@app.get("/dispatch")
def dispatch():
    return DISPATCH_QUEUE


# ---------- 21-day countdown ----------
@app.get("/countdown")
def countdown(last_contact: date):
    today = date.today()
    if last_contact > today:
        raise HTTPException(422, "last_contact is in the future")
    day = min((today - last_contact).days, 21)
    return {
        "day": day,
        "of": 21,
        "discharge_date": (last_contact + timedelta(days=21)).isoformat(),
        "complete": day >= 21,
    }


# ---------- Gemma 4 extraction ----------
class Extraction(BaseModel):
    names: list[str] = []
    visit_date: str | None = None
    exposures: list[str] = []
    needs_review: bool = True
    uncertain: list[str] = []


PROMPT = """Extract structured data from this field note. Output ONLY one JSON object, no markdown.
Schema: {{"names": [str], "visit_date": "YYYY-MM-DD" or null, "exposures": [str],
"needs_review": bool, "uncertain": [str]}}
Exposures are events like funeral attendance or caring for a sick relative.
Never guess. If a name, date or event is unclear, put it in "uncertain" and set needs_review true.
Do not give medical advice.
Today is {today}.
NOTE: {note}"""


class Note(BaseModel):
    text: str = Field(min_length=3, max_length=4000)


def parse_json(raw: str) -> dict:
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(raw[start : end + 1])


@app.post("/extract")
def extract(note: Note):
    if client is None:
        raise HTTPException(500, "GEMINI_API_KEY not set")
    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=PROMPT.format(today=date.today().isoformat(), note=note.text),
        )
    except errors.APIError as e:
        raise HTTPException(502, f"Gemma API error {e.code}; note queued for manual entry")
    except Exception:
        raise HTTPException(502, "Network/timeout; note queued for manual entry")
    if not resp.text or not resp.text.strip():
        raise HTTPException(502, "Empty model output; enter manually")
    try:
        return Extraction(**parse_json(resp.text)).model_dump()
    except (ValueError, ValidationError):
        # Malformed output is a failure, not a pass: force human review.
        return Extraction(needs_review=True, uncertain=["model output unparseable"]).model_dump()