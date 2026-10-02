"""Review UI. Run:  uvicorn review_app.app:app --port 8000"""
import threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline import store, run as pipeline_run
from pipeline.apply import decide
from pipeline.crm import CRM

app = FastAPI(title="Bellhaven ownership review")
db = store.connect()
crm = CRM()
lock = threading.Lock()   # one shared sqlite connection; also serializes CRM writes
HERE = Path(__file__).parent


class Decision(BaseModel):
    note: str = ""
    reviewer: str = "reviewer"


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/api/proposals")
def proposals(status: str = "", kind: str = ""):
    q, args = "SELECT * FROM proposals WHERE 1=1", []
    if status: q += " AND status=?"; args.append(status)
    if kind: q += " AND kind=?"; args.append(kind)
    q += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, kind, title"
    with lock:
        return [store.row_to_dict(r) for r in db.execute(q, args)]


@app.get("/api/stats")
def stats():
    with lock:
        by = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}
        run = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"by_status": by, "last_run": dict(run) if run else None}


@app.post("/api/proposals/{fp}/approve")
def approve(fp: str, d: Decision = Decision()):
    try:
        with lock:
            return decide(db, crm, fp, "approve", d.reviewer, d.note)
    except KeyError:
        raise HTTPException(404, "unknown proposal")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/proposals/{fp}/reject")
def reject(fp: str, d: Decision = Decision()):
    try:
        with lock:
            return decide(db, crm, fp, "reject", d.reviewer, d.note)
    except KeyError:
        raise HTTPException(404, "unknown proposal")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/run")
def run_now():
    """Re-run the pipeline (read-only against the CRM; only creates new pending proposals)."""
    try:
        return pipeline_run.main()
    except SystemExit as e:  # a safety guard refused the run; report it instead of killing the server
        raise HTTPException(409, str(e))
