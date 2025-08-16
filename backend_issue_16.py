# backend_issue_16.py
import asyncio
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import threading
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

print(">>> ISSUE16 build loaded: backend_issue_16.py")

# -------------------
# CONFIG
# -------------------
DB_PATH = "twin_history.db"
API_KEY = os.environ.get("FACTORY_API_KEY", "secret123")
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# -------------------
# Helpers (time)
# -------------------
def now_utc_iso() -> str:
    # شكل قياسي مع إزاحة +00:00 (بدون Z)
    return datetime.now(timezone.utc).isoformat()

def parse_any_ts(s: str) -> datetime:
    # يقبل ...Z أو ...+00:00 أو Naive (نعتبرها UTC)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# -------------------
# DATABASE (SQLite)
# -------------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        payload TEXT NOT NULL
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        config TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn

db_conn = init_db()
db_lock = threading.Lock()

def save_metrics_snapshot(snapshot: dict):
    """Persist snapshot JSON. Auto-reinit DB if file becomes malformed."""
    try:
        with db_lock:
            cur = db_conn.cursor()
            cur.execute("INSERT INTO metrics (ts, payload) VALUES (?, ?)",
                        (snapshot["time"], json.dumps(snapshot)))
            db_conn.commit()
    except sqlite3.DatabaseError as e:
        print("DB save error:", e)
        try:
            db_conn.close()
        except:
            pass
        try:
            os.replace(DB_PATH, DB_PATH + ".bad")
        except:
            pass
        globals()["db_conn"] = init_db()

def query_history(minutes: int = 60):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    cutoff_iso = cutoff.isoformat()  # نتعامل دائماً بصيغة +00:00
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT ts, payload FROM metrics WHERE ts >= ? ORDER BY ts ASC", (cutoff_iso,))
        rows = cur.fetchall()
    out = []
    for _, payload in rows:
        try:
            out.append(json.loads(payload))
        except:
            pass
    return out

# -------------------
# Twin model
# -------------------
class Machine:
    def __init__(self, id, base_rate, position):
        self.id = id
        self.base_rate = base_rate               # items/min ideal
        self.throughput_factor = 1.0
        self.uptime = 1.0
        self.queue = 0.0
        self.position = position
        self.status = "on"
        self.total_processed = 0.0

    def to_dict(self):
        return {
            "id": self.id,
            "base_rate": self.base_rate,
            "throughput_factor": round(self.throughput_factor, 3),
            "uptime": round(self.uptime, 3),
            "queue": round(self.queue, 2),
            "position": self.position,
            "status": self.status,
            "total_processed": int(self.total_processed),
        }

    def from_dict(self, d: dict):
        self.base_rate = float(d.get("base_rate", self.base_rate))
        self.throughput_factor = float(d.get("throughput_factor", 1.0))
        self.uptime = float(d.get("uptime", 1.0))
        self.queue = float(d.get("queue", 0.0))
        self.position = int(d.get("position", self.position))
        self.status = d.get("status", "on")
        self.total_processed = float(d.get("total_processed", 0.0))

DEFAULT_MACHINES = [
    {"id": "M1_cutter", "base_rate": 60, "position": 1},
    {"id": "M2_press",  "base_rate": 50, "position": 2},
    {"id": "M3_paint",  "base_rate": 40, "position": 3},
    {"id": "M4_inspect","base_rate": 70, "position": 4},
]
machines: List[Machine] = [Machine(m["id"], m["base_rate"], m["position"]) for m in DEFAULT_MACHINES]

INITIAL_TWIN_STATE = {"staffing_shifts": 1, "ambient_temp": 25.0, "ambient_humidity": 45.0, "total_output": 0}
twin_state = INITIAL_TWIN_STATE.copy()
twin_state["time"] = now_utc_iso()

def compute_staffing_modifier(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)

def process_production_tick(delta_seconds: float):
    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])

    # ambient drift
    twin_state["ambient_temp"] += random.uniform(-0.05, 0.05)
    twin_state["ambient_humidity"] += random.uniform(-0.1, 0.1)
    twin_state["ambient_temp"] = round(max(15, min(40, twin_state["ambient_temp"])), 2)
    twin_state["ambient_humidity"] = round(max(20, min(80, twin_state["ambient_humidity"])), 2)

    ms = sorted(machines, key=lambda m: m.position)
    new_output = 0.0
    for i, m in enumerate(ms):
        if m.status != "on":
            # degrade uptime slightly when off
            m.uptime = max(0.0, m.uptime - 0.002 * (delta_seconds / TICK_SECONDS))
            continue

        # gentle uptime drift
        if random.random() < 0.0005:
            m.uptime = max(0.5, m.uptime - random.uniform(0.05, 0.2))
        else:
            m.uptime = min(1.0, m.uptime + 0.0005 * (delta_seconds / TICK_SECONDS))

        ideal_per_tick = m.base_rate * (delta_seconds / SECONDS_PER_MIN)
        effective_rate = max(0.0, ideal_per_tick * m.throughput_factor * staffing_mod * m.uptime)

        # Upstream arrivals for first machine
        if i == 0:
            arrivals = max(0.0, random.gauss(ideal_per_tick * staffing_mod, ideal_per_tick * 0.2))
            m.queue += arrivals

        processed = min(m.queue, effective_rate)
        m.queue -= processed
        m.total_processed += processed

        if i < len(ms) - 1:
            ms[i + 1].queue += processed
        else:
            new_output += processed

        # tiny random walk for throughput_factor
        m.throughput_factor = max(0.2, min(1.8, m.throughput_factor + random.uniform(-0.001, 0.001)))

    twin_state["total_output"] += int(new_output)
    twin_state["time"] = now_utc_iso()

# -------------------
# WS manager + loop
# -------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: Dict):
        data = json.dumps(msg)
        alive = []
        for ws in list(self.active):
            try:
                await ws.send_text(data)
                alive.append(ws)
            except:
                pass
        self.active = alive

manager = ConnectionManager()

async def simulator_loop():
    # يولّد لقطة جديدة ويحفظها كل TICK_SECONDS
    while True:
        process_production_tick(TICK_SECONDS)
        snap_machines = [m.to_dict() for m in sorted(machines, key=lambda x: x.position)]
        bottlenecks = [m["id"] for m in snap_machines if m["queue"] > 5.0]
        metrics = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": snap_machines,
            "bottlenecks": bottlenecks,
            "staffing_shifts": twin_state["staffing_shifts"],
        }
        await manager.broadcast({"type": "metrics", "payload": metrics})
        save_metrics_snapshot(metrics)
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.get_event_loop().create_task(simulator_loop())

# -------------------
# WebSocket
# -------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # send initial snapshot
        await websocket.send_text(json.dumps({"type": "metrics", "payload": {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": [m.to_dict() for m in sorted(machines, key=lambda x: x.position)],
            "bottlenecks": [],
            "staffing_shifts": twin_state["staffing_shifts"],
        }}))
        while True:
            await websocket.receive_text()  # not used now
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# -------------------
# HTTP
# -------------------
@app.get("/")
async def index():
    return FileResponse("static/index16.html")

@app.get("/api/history")
async def api_history(minutes: int = 60):
    rows = query_history(minutes)
    return {"minutes": minutes, "count": len(rows), "snapshots": rows}

@app.get("/api/latest")
async def api_latest():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT payload FROM metrics ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
    if not r:
        # رجّع حالة فورية حتى لو ما فيه تاريخ محفوظ بعد
        return {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": [m.to_dict() for m in sorted(machines, key=lambda x: x.position)],
            "bottlenecks": [],
            "staffing_shifts": twin_state["staffing_shifts"],
        }
    return json.loads(r[0])

# ---------- NEW: Queue vs Throughput endpoint ----------
def _extract_machine(snapshot: dict, machine_id: str) -> Optional[dict]:
    for m in snapshot.get("machines", []):
        if m.get("id") == machine_id:
            return m
    return None

@app.get("/api/machine_timeseries")
async def api_machine_timeseries(
    machine_id: str = Query(..., description="Machine ID, e.g., M1_cutter"),
    minutes: int = Query(60, ge=1, le=24*60, description="Lookback minutes"),
    bucket_seconds: int = Query(10, ge=1, le=3600, description="Downsampling bucket (sec)")
):
    """
    Returns aligned points for:
      - queue (items)
      - throughput_ipm (items/min) computed from delta of total_processed
    """
    snapshots = query_history(minutes)
    if not snapshots:
        return {"machine_id": machine_id, "points": []}

    points = []
    prev_tp: Optional[float] = None
    prev_ts: Optional[datetime] = None

    for s in snapshots:
        mm = _extract_machine(s, machine_id)
        if not mm:
            continue
        ts = parse_any_ts(s["time"])
        tp = float(mm.get("total_processed", 0.0))
        q = float(mm.get("queue", 0.0))

        rate = None
        if prev_tp is not None and prev_ts is not None:
            dt = (ts - prev_ts).total_seconds()
            if dt > 0:
                rate = (tp - prev_tp) / (dt / 60.0)
                if rate < 0:
                    rate = None

        points.append({"ts": ts.isoformat(), "queue": q, "throughput_ipm": rate})
        prev_tp, prev_ts = tp, ts

    # downsample to bucket_seconds
    if bucket_seconds and points:
        bucketed = []
        b_start = parse_any_ts(points[0]["ts"])
        acc_q, acc_r = [], []
        last_ts = b_start
        for p in points:
            t = parse_any_ts(p["ts"])
            if (t - b_start).total_seconds() < bucket_seconds:
                acc_q.append(p["queue"])
                if p["throughput_ipm"] is not None:
                    acc_r.append(p["throughput_ipm"])
                last_ts = t
            else:
                bucketed.append({
                    "ts": last_ts.isoformat(),
                    "queue": sum(acc_q)/len(acc_q) if acc_q else None,
                    "throughput_ipm": sum(acc_r)/len(acc_r) if acc_r else None
                })
                b_start = t
                acc_q = [p["queue"]]
                acc_r = [p["throughput_ipm"]] if p["throughput_ipm"] is not None else []
                last_ts = t
        bucketed.append({
            "ts": last_ts.isoformat(),
            "queue": sum(acc_q)/len(acc_q) if acc_q else None,
            "throughput_ipm": sum(acc_r)/len(acc_r) if acc_r else None
        })
        points = bucketed

    return {
        "machine_id": machine_id,
        "minutes": minutes,
        "bucket_seconds": bucket_seconds,
        "count": len(points),
        "points": points
    }

# -------------------
# (اختياري) Presets CRUD مبسّط
# -------------------
def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

@app.post("/api/presets")
async def save_preset(payload: dict, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    config = {"twin_state": twin_state.copy(), "machines": [m.to_dict() for m in machines]}
    created_at = now_utc_iso()
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("INSERT INTO presets (name, description, config, created_at) VALUES (?, ?, ?, ?)",
                    (name, "", json.dumps(config), created_at))
        db_conn.commit()
        pid = cur.lastrowid
    return {"ok": True, "id": pid}

@app.get("/api/presets")
async def list_presets():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT id, name, description, created_at FROM presets ORDER BY id DESC")
        rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "description": r[2] or "", "created_at": r[3]} for r in rows]

@app.post("/api/presets/{preset_id}/load")
async def load_preset(preset_id: int, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT config FROM presets WHERE id = ?", (preset_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    cfg = json.loads(row[0])
    new_machines = []
    for md in cfg.get("machines", []):
        m = Machine(md["id"], float(md.get("base_rate", 50)), int(md.get("position", 1)))
        m.from_dict(md)
        new_machines.append(m)
    machines.clear(); machines.extend(new_machines)
    saved_ts = cfg.get("twin_state", INITIAL_TWIN_STATE.copy())
    twin_state.clear(); twin_state.update(saved_ts)
    twin_state["time"] = now_utc_iso()
    return {"ok": True}

