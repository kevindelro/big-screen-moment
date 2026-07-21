"""
Big Screen Moment - backend API (pilot version)

Covers the whole loop except real video capture/detection:
  - create an event
  - mark period boundaries a few times per game (Q1, Q2, ...) instead of
    marking every fan moment
  - ingest a candidate clip (this is the endpoint the auto-detector, or a
    manual fallback, would call the moment it flags something)
  - admin approves/rejects candidates
  - fans fetch the approved gallery for an event, grouped by period

Run it:
    pip install fastapi "uvicorn[standard]" pillow
    python seed_demo_data.py
    uvicorn main:app --reload

Then open:
    http://localhost:8000/app/gallery.html?event=1
    http://localhost:8000/app/admin.html?event=1
"""

import os
import traceback
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Form, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, PlainTextResponse
from pydantic import BaseModel
from typing import Optional

import db

os.makedirs("static", exist_ok=True)
os.makedirs("/data/thumbnails", exist_ok=True)
os.makedirs("/data/clips", exist_ok=True)

app = FastAPI(title="Big Screen Moment API")
db.init_db()

app.mount("/thumbnails", StaticFiles(directory="/data/thumbnails"), name="thumbnails")
app.mount("/clips", StaticFiles(directory="/data/clips"), name="clips")
app.mount("/app", StaticFiles(directory="static", html=True), name="app")


@app.exception_handler(Exception)
async def debug_exception_handler(request, exc):
    """
    TEMPORARY, for getting this pilot working: returns the real Python
    error instead of a generic 'Internal Server Error', so it shows up
    directly wherever the failing request came from (e.g. printed by
    detect_big_screen_moments.py) instead of needing to dig through
    Render's dashboard logs. Take this out before this is a real product
    other people rely on - it can leak internal details.
    """
    tb = traceback.format_exc()
    return PlainTextResponse(f"{type(exc).__name__}: {exc}\n\n{tb}", status_code=500)


class EventCreate(BaseModel):
    name: str
    venue: Optional[str] = None


class PeriodCreate(BaseModel):
    label: str


class ClipCreate(BaseModel):
    timestamp: Optional[str] = None
    duration: float = 4.0
    thumbnail_path: Optional[str] = None
    video_path: Optional[str] = None
    auto_approve: bool = False


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@app.post("/api/events")
def create_event(payload: EventCreate):
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO events (name, venue, created_at) VALUES (?, ?, ?)",
        (payload.name, payload.venue, now_iso()),
    )
    conn.commit()
    event_id = cur.lastrowid
    conn.close()
    return {"id": event_id, "name": payload.name, "venue": payload.venue}


@app.post("/api/events/{event_id}/periods")
def mark_period(event_id: int, payload: PeriodCreate):
    """Call this a handful of times per event: 'start of Q2', etc. Closes
    whatever period was open and starts the new one at the current time."""
    ts = now_iso()
    conn = db.get_conn()
    conn.execute(
        "UPDATE periods SET end_time = ? WHERE event_id = ? AND end_time IS NULL",
        (ts, event_id),
    )
    cur = conn.execute(
        "INSERT INTO periods (event_id, label, start_time) VALUES (?, ?, ?)",
        (event_id, payload.label, ts),
    )
    conn.commit()
    period_id = cur.lastrowid
    conn.close()
    return {"id": period_id, "label": payload.label, "start_time": ts}


def find_period_for_timestamp(conn, event_id, timestamp):
    rows = conn.execute(
        "SELECT id, start_time, end_time FROM periods WHERE event_id = ? ORDER BY start_time",
        (event_id,),
    ).fetchall()
    for row in rows:
        if row["start_time"] <= timestamp and (row["end_time"] is None or timestamp < row["end_time"]):
            return row["id"]
    return None


@app.post("/api/events/{event_id}/clips")
def ingest_clip(event_id: int, payload: ClipCreate):
    """What the auto-detector (or a manual fallback button) calls the
    moment it flags a candidate. New clips always start as 'candidate' -
    nothing reaches the fan gallery until it's approved.

    NOTE: this endpoint only stores PATHS, not files - it only works when
    the caller and this server share the same filesystem (e.g. testing
    locally on one machine). Once the app is hosted somewhere else (like
    Render), use /api/events/{event_id}/clips/upload instead, which
    actually sends the file bytes."""
    timestamp = payload.timestamp or now_iso()
    status = "approved" if payload.auto_approve else "candidate"
    conn = db.get_conn()
    period_id = find_period_for_timestamp(conn, event_id, timestamp)
    cur = conn.execute(
        "INSERT INTO clips (event_id, period_id, timestamp, duration, thumbnail_path, "
        "video_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, period_id, timestamp, payload.duration, payload.thumbnail_path,
         payload.video_path, status, now_iso()),
    )
    conn.commit()
    clip_id = cur.lastrowid
    conn.close()
    return {"id": clip_id, "status": status, "period_id": period_id}


@app.post("/api/events/{event_id}/clips/upload")
async def ingest_clip_upload(
    event_id: int,
    thumbnail: UploadFile = File(...),
    clip: UploadFile = File(...),
    duration: float = Form(4.0),
    auto_approve: bool = Form(False),
):
    """Same idea as /clips above, but actually receives and saves the real
    thumbnail + video FILES - this is what the detector scripts use once
    they're running on a different machine than this server (the normal
    case once this is hosted somewhere like Render)."""
    os.makedirs("/data/thumbnails", exist_ok=True)
    os.makedirs("/data/clips", exist_ok=True)

    thumb_name = f"{uuid.uuid4().hex}.jpg"
    clip_name = f"{uuid.uuid4().hex}.mp4"
    with open(os.path.join("/data/thumbnails", thumb_name), "wb") as f:
        f.write(await thumbnail.read())
    with open(os.path.join("/data/clips", clip_name), "wb") as f:
        f.write(await clip.read())

    timestamp = now_iso()
    status = "approved" if auto_approve else "candidate"
    conn = db.get_conn()
    period_id = find_period_for_timestamp(conn, event_id, timestamp)
    cur = conn.execute(
        "INSERT INTO clips (event_id, period_id, timestamp, duration, thumbnail_path, "
        "video_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, period_id, timestamp, duration, f"/thumbnails/{thumb_name}",
         f"/clips/{clip_name}", status, timestamp),
    )
    conn.commit()
    clip_id = cur.lastrowid
    conn.close()
    return {"id": clip_id, "status": status, "period_id": period_id}


@app.post("/api/clips/{clip_id}/approve")
def approve_clip(clip_id: int):
    conn = db.get_conn()
    conn.execute("UPDATE clips SET status = 'approved' WHERE id = ?", (clip_id,))
    conn.commit()
    conn.close()
    return {"id": clip_id, "status": "approved"}


@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: int):
    conn = db.get_conn()
    conn.execute("UPDATE clips SET status = 'rejected' WHERE id = ?", (clip_id,))
    conn.commit()
    conn.close()
    return {"id": clip_id, "status": "rejected"}


def _clips_by_status(event_id: int, status: str):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT clips.*, periods.label AS period_label FROM clips "
        "LEFT JOIN periods ON clips.period_id = periods.id "
        "WHERE clips.event_id = ? AND clips.status = ? ORDER BY clips.timestamp",
        (event_id, status),
    ).fetchall()
    conn.close()
    grouped = {}
    for row in rows:
        label = row["period_label"] or "Ungrouped"
        grouped.setdefault(label, []).append(dict(row))
    return grouped


@app.get("/api/events/{event_id}/clips")
def list_clips(event_id: int, status: str = "approved"):
    """Fan-facing gallery data: clips for this event, grouped by period."""
    return _clips_by_status(event_id, status)


@app.get("/api/events/{event_id}/candidates")
def list_candidates(event_id: int):
    """Admin review queue."""
    return _clips_by_status(event_id, "candidate")


# --- Text delivery -----------------------------------------------------
# PUBLIC_BASE_URL must be set to wherever this app is actually reachable
# from a fan's phone - e.g. an ngrok URL for testing, or your real domain
# once this is hosted for real. It can't be 127.0.0.1/localhost, since
# that only means "this computer" to whoever receives the text.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# Which event a text should point to. Hardcoded to 1 for the pilot - a
# real multi-event system would map this by phone number, keyword, or
# whichever event is currently live.
ACTIVE_EVENT_ID = 1


@app.post("/api/sms/inbound")
async def sms_inbound(Body: str = Form(default=""), From: str = Form(default="")):
    """
    This is the URL you point Twilio's "A message comes in" webhook at.
    Whatever the fan actually texted doesn't matter for the pilot - any
    text to the number gets the current event's gallery link back.
    """
    gallery_url = f"{PUBLIC_BASE_URL}/app/gallery.html?event={ACTIVE_EVENT_ID}"
    message = f"You made the BIG screen! \U0001F389 Click here to download your Big Screen Moment: {gallery_url}"

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Message>" + message + "</Message></Response>"
    )
    return Response(content=twiml, media_type="application/xml")
