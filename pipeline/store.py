"""SQLite state: proposals + their decisions. The fingerprint makes re-runs idempotent."""
import json
import sqlite3
from datetime import datetime, timezone
from . import config


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None):
    db = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, web_count INT, crm_count INT, summary TEXT);
    CREATE TABLE IF NOT EXISTS proposals(
      fingerprint TEXT PRIMARY KEY, kind TEXT, title TEXT, severity TEXT,
      actions TEXT, evidence TEXT,
      status TEXT DEFAULT 'pending',       -- pending|applied|rejected|failed|obsolete
      first_run INT, last_seen_run INT, created_at TEXT,
      decided_at TEXT, decided_by TEXT, reviewer_note TEXT,
      result TEXT, error TEXT);
    """)
    return db


def record_run(db, web_n, crm_n, summary) -> int:
    cur = db.execute("INSERT INTO runs(ts,web_count,crm_count,summary) VALUES(?,?,?,?)",
                     (now(), web_n, crm_n, json.dumps(summary)))
    db.commit()
    return cur.lastrowid


def upsert_proposals(db, run_id, proposals) -> dict:
    """New fingerprint -> insert as pending. Known fingerprint (any status) -> never re-proposed.
    Pending proposals that disappeared from this run's output -> obsolete (data converged)."""
    seen = {p["fingerprint"] for p in proposals}
    new = skipped = 0
    for p in proposals:
        row = db.execute("SELECT status FROM proposals WHERE fingerprint=?", (p["fingerprint"],)).fetchone()
        if row:
            skipped += 1
            db.execute("UPDATE proposals SET last_seen_run=? WHERE fingerprint=?", (run_id, p["fingerprint"]))
            continue
        db.execute("""INSERT INTO proposals(fingerprint,kind,title,severity,actions,evidence,first_run,last_seen_run,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (p["fingerprint"], p["kind"], p["title"], p["severity"], json.dumps(p["actions"]),
                    json.dumps(p["evidence"]), run_id, run_id, now()))
        new += 1
    obsolete = 0
    for r in db.execute("SELECT fingerprint FROM proposals WHERE status='pending'").fetchall():
        if r["fingerprint"] not in seen:
            db.execute("UPDATE proposals SET status='obsolete' WHERE fingerprint=?", (r["fingerprint"],))
            obsolete += 1
    db.commit()
    return {"new": new, "already_known": skipped, "obsolete": obsolete}


def row_to_dict(r):
    d = dict(r)
    for k in ("actions", "evidence", "result"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d
