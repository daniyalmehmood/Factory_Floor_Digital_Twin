# by_haya/backend_issue_16.py
import asyncio, json, random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from collections import deque, defaultdict
from pathlib import Path


from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse


from collections import deque, defaultdict
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import Query, HTTPException

# Simulation & history settings
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0
HISTORY_RETENTION_SECONDS = 6 * 60 * 60  # keep 6 hours of data
HISTORY_MAXLEN = int(HISTORY_RETENTION_SECONDS / max(TICK_SECONDS, 1.0))

def now_utc():
    return datetime.now(timezone.utc)

def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_iso(s: Optional[str], default: Optional[datetime] = None) -> datetime:
    if not s:
        return default if default is not None else now_utc()
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def parse_bucket_seconds(bucket: str) -> int:
    b = bucket.strip().lower()
    unit = b[-1]
    try:
        val = float(b[:-1])
    except Exception:
        raise HTTPException(400, "Invalid bucket format. Examples: 10s, 1m, 5m, 1h")
    if val <= 0:
        raise HTTPException(400, "Bucket must be > 0")
    if unit == "s": return int(val)
    if unit == "m": return int(val * 60)
    if unit == "h": return int(val * 3600)
    raise HTTPException(400, "Bucket unit must be s/m/h")

# =======================
# FastAPI app setup 
# =======================
BASE_DIR   = Path(__file__).resolve().parent      # .../by_haya
ROOT_DIR   = BASE_DIR.parent                      # Project root
STATIC_DIR = ROOT_DIR / "static"                  # .../static  (sibling to by_haya)
INDEX_HTML = BASE_DIR / "index16.html"            # by_haya/index16.html

app = FastAPI(title="Factory Digital Twin (Issue 16)")

# Serve static directory at /static (for public files if needed)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Serve by_haya directory as static files at /by_haya (required)
app.mount("/by_haya", StaticFiles(directory=str(BASE_DIR)), name="by_haya")

# (Optional) If you open / directly, serve index16.html
@app.get("/", include_in_schema=False)
def root_page():
    return FileResponse(str(INDEX_HTML))

# =======================
# Simulation and history settings
# =======================
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0
HISTORY_RETENTION_SECONDS = 6 * 60 * 60
HISTORY_MAXLEN = int(HISTORY_RETENTION_SECONDS / max(TICK_SECONDS, 1.0))

def now_utc(): return datetime.now(timezone.utc)

def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_iso(s: Optional[str], default: Optional[datetime] = None) -> datetime:
    if not s: return default if default is not None else now_utc()
    if s.endswith("Z"): s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def parse_bucket_seconds(bucket: str) -> int:
    b = bucket.strip().lower()
    if b.endswith("ms"):
        raise HTTPException(400, "Bucket in milliseconds not supported; use s/m/h")
    unit = b[-1]
    try:
        val = float(b[:-1])
    except Exception:
        raise HTTPException(400, "Invalid bucket. Examples: 10s, 1m, 5m, 1h")
    if val <= 0: raise HTTPException(400, "Bucket must be > 0")
    if unit == "s": return int(val)
    if unit == "m": return int(val * 60)
    if unit == "h": return int(val * 3600)
    raise HTTPException(400, "Bucket unit must be s/m/h")

# =======================
# Machine model
# =======================
class Machine:
    def __init__(self, id, base_rate, position):
        self.id = id
        self.base_rate = base_rate  # items/min
        self.throughput_factor = 1.0
        self.uptime = 1.0
        self.queue = 0.0
        self.position = position
        self.status = "on"
        self.total_processed = 0.0
        self.last_change = iso_z(now_utc())
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

machines: List[Machine] = [
    Machine("M1_cutter", 60, 1),
    Machine("M2_press",  50, 2),
    Machine("M3_paint",  40, 3),
    Machine("M4_inspect",70, 4),
]

# In-memory history: for each machine, store (ts, queue_size, processed_in_tick)
history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))

twin_state = {
    "staffing_shifts": 1,
    "ambient_temp": 25.0,
    "ambient_humidity": 45.0,
    "last_output_total": 0,
    "total_output": 0,
    "time": iso_z(now_utc()),
}

def compute_staffing_modifier(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)

def process_production_tick(delta_seconds: float):
    global twin_state, machines, history
    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])

    # Environment
    twin_state["ambient_temp"] += random.uniform(-0.05, 0.05)
    twin_state["ambient_humidity"] += random.uniform(-0.1, 0.1)
    twin_state["ambient_temp"] = round(max(15, min(40, twin_state["ambient_temp"])), 2)
    twin_state["ambient_humidity"] = round(max(20, min(80, twin_state["ambient_humidity"])), 2)

    machines_sorted = sorted(machines, key=lambda m: m.position)
    new_output = 0.0
    tick_ts = now_utc()

    for i, m in enumerate(machines_sorted):
        if m.status != "on":
            m.uptime = max(0.0, m.uptime - 0.001 * (delta_seconds / TICK_SECONDS))
            history[m.id].append((tick_ts, float(m.queue), 0.0))
            continue

        if random.random() < 0.0005:
            m.uptime = max(0.5, m.uptime - random.uniform(0.05, 0.2))
        else:
            m.uptime = min(1.0, m.uptime + 0.0005 * (delta_seconds / TICK_SECONDS))

        ideal_per_tick = m.base_rate * (delta_seconds / SECONDS_PER_MIN)
        effective_rate = ideal_per_tick * m.throughput_factor * staffing_mod * m.uptime

        if i == 0:
            arrivals = max(0.0, random.gauss(ideal_per_tick * staffing_mod, ideal_per_tick * 0.2))
            m.queue += arrivals

        processed = min(m.queue, max(0.0, effective_rate))
        m.queue -= processed
        m.total_processed += processed

        if i < len(machines_sorted) - 1:
            machines_sorted[i + 1].queue += processed
        else:
            new_output += processed

        m.throughput_factor += random.uniform(-0.001, 0.001)
        m.throughput_factor = max(0.2, min(1.8, m.throughput_factor))

        history[m.id].append((tick_ts, float(m.queue), float(processed)))

    twin_state["total_output"] += int(new_output)
    twin_state["time"] = iso_z(now_utc())

# =======================
# WebSocket
# =======================
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept(); self.active.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active: self.active.remove(websocket)
    async def broadcast(self, message: Dict):
        data = json.dumps(message); living = []
        for ws in list(self.active):
            try: await ws.send_text(data); living.append(ws)
            except Exception: pass
        self.active = living

manager = ConnectionManager()

async def simulator_loop():
    while True:
        process_production_tick(TICK_SECONDS)
        machines_snapshot = [m.to_dict() for m in sorted(machines, key=lambda x: x.position)]
        bottlenecks = [m["id"] for m in machines_snapshot
                       if m["queue"] > max(2.0, 0.5 * (m["base_rate"]/SECONDS_PER_MIN) * TICK_SECONDS)]
        metrics = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": machines_snapshot,
            "bottlenecks": bottlenecks,
            "staffing_shifts": twin_state["staffing_shifts"],
        }
        await manager.broadcast({"type": "metrics", "payload": metrics})
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.get_event_loop().create_task(simulator_loop())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
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
            obj = json.loads(await websocket.receive_text())
            action = obj.get("action")
            if action == "add_shift":
                twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)
            elif action == "remove_shift":
                twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)
            elif action == "move_equipment":
                mid = obj.get("machine_id"); new_pos = obj.get("new_position")
                for m in machines:
                    if m.id == mid:
                        m.position = int(new_pos); m.last_change = iso_z(now_utc()); break
                for idx, m in enumerate(sorted(machines, key=lambda x: x.position), start=1):
                    m.position = idx
            elif action == "toggle_machine":
                mid = obj.get("machine_id")
                for m in machines:
                    if m.id == mid:
                        m.status = "off" if m.status == "on" else "on"
                        m.last_change = iso_z(now_utc()); break
            elif action == "set_throughput":
                mid = obj.get("machine_id"); val = float(obj.get("value", 1.0))
                for m in machines:
                    if m.id == mid:
                        m.throughput_factor = max(0.2, min(2.0, val))
                        m.last_change = iso_z(now_utc()); break
            elif action == "add_machine":
                new_id = obj.get("machine_id", f"M_new_{len(machines)+1}")
                pos = int(obj.get("position", len(machines)+1))
                base_rate = float(obj.get("base_rate", 50))
                machines.append(Machine(new_id, base_rate, pos))
                for idx, m in enumerate(sorted(machines, key=lambda x: x.position), start=1):
                    m.position = idx
                _ = history[new_id]  # Initialize buffer
            else:
                await websocket.send_text(json.dumps({"type":"error","payload":"unknown action"}))
                continue

            await manager.broadcast({"type": "metrics", "payload": {
                "time": twin_state["time"],
                "ambient_temp": twin_state["ambient_temp"],
                "ambient_humidity": twin_state["ambient_humidity"],
                "total_output": twin_state["total_output"],
                "machines": [m.to_dict() for m in sorted(machines, key=lambda x: x.position)],
                "bottlenecks": [m.id for m in machines if m.queue > 5],
                "staffing_shifts": twin_state["staffing_shifts"],
            }})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

# # =======================
# # API: Queue vs Throughput (Issue 16)
# # =======================
# @app.get("/api/machines/{machine_id}/queue-vs-throughput")
# def queue_vs_throughput(
#     machine_id: str,
#     from_: Optional[str] = Query(None, alias="from", description="ISO8601 start (UTC). Default: now-1h"),
#     to: Optional[str]   = Query(None, description="ISO8601 end (UTC). Default: now"),
#     bucket: str = Query("1m", description="Bucket size, e.g., 10s, 1m, 5m, 1h")
# ):
#     if not any(m.id == machine_id for m in machines):
#         raise HTTPException(404, "machine not found")

#     t_end = parse_iso(to, default=now_utc())
#     t_start = parse_iso(from_, default=t_end - timedelta(hours=1))
#     if t_start >= t_end:
#         raise HTTPException(400, "from must be < to")

#     bucket_sec = parse_bucket_seconds(bucket)
#     buf = history.get(machine_id, None)
#     if buf is None or len(buf) == 0:
#         return {"machine_id": machine_id, "from_": iso_z(t_start), "to": iso_z(t_end),
#                 "bucket": bucket, "points": [],
#                 "units": {"queue_size": "items", "actual_throughput": "items/min"}}

#     rows = [r for r in buf if (r[0] >= t_start and r[0] <= t_end)]
#     if not rows:
#         return {"machine_id": machine_id, "from_": iso_z(t_start), "to": iso_z(t_end),
#                 "bucket": bucket, "points": [],
#                 "units": {"queue_size": "items", "actual_throughput": "items/min"}}

#     # Aggregate by bucket
#     bins: Dict[int, Dict[str, float]] = {}
#     counts: Dict[int, int] = {}
#     for ts, qsize, processed in rows:
#         idx = int((ts - t_start).total_seconds() // bucket_sec)
#         b = bins.setdefault(idx, {"queue_sum": 0.0, "processed_sum": 0.0})
#         b["queue_sum"] += float(qsize)
#         b["processed_sum"] += float(processed)
#         counts[idx] = counts.get(idx, 0) + 1

#     total_bins = int((t_end - t_start).total_seconds() // bucket_sec) + 1
#     points = []
#     for i in range(total_bins):
#         bucket_start = t_start + timedelta(seconds=i*bucket_sec)
#         if i in bins:
#             avg_queue = bins[i]["queue_sum"] / max(1, counts[i])
#             th = bins[i]["processed_sum"] * 60.0 / float(bucket_sec)
#             points.append({"t": iso_z(bucket_start),
#                            "queue_size": int(round(avg_queue)),
#                            "actual_throughput": round(th, 3)})
#         else:
#             points.append({"t": iso_z(bucket_start), "queue_size": None, "actual_throughput": None})

#     return {
#         "machine_id": machine_id,
#         "from_": iso_z(t_start),
#         "to": iso_z(t_end),
#         "bucket": bucket,
#         "points": points,
#         "units": {"queue_size": "items", "actual_throughput": "items/min"}
#     }

# =======================
# Function 1: Data Processing Logic
# =======================
def process_queue_throughput_data(
    machine_id: str,
    t_start: datetime,
    t_end: datetime,
    bucket_sec: int,
    history_buffer
) -> dict:
    """
    Process historical data to calculate queue size and throughput metrics
    """
    buf = history_buffer.get(machine_id, None)
    if buf is None or len(buf) == 0:
        return {
            "points": [],
            "units": {"queue_size": "items", "actual_throughput": "items/min"}
        }
    
    # Filter data within time range
    rows = [r for r in buf if (r[0] >= t_start and r[0] <= t_end)]
    if not rows:
        return {
            "points": [],
            "units": {"queue_size": "items", "actual_throughput": "items/min"}
        }
    
    # Aggregate data by bucket
    bins: Dict[int, Dict[str, float]] = {}
    counts: Dict[int, int] = {}
    
    for ts, qsize, processed in rows:
        idx = int((ts - t_start).total_seconds() // bucket_sec)
        b = bins.setdefault(idx, {"queue_sum": 0.0, "processed_sum": 0.0})
        b["queue_sum"] += float(qsize)
        b["processed_sum"] += float(processed)
        counts[idx] = counts.get(idx, 0) + 1
    
    # Generate points for all time buckets
    total_bins = int((t_end - t_start).total_seconds() // bucket_sec) + 1
    points = []
    
    for i in range(total_bins):
        bucket_start = t_start + timedelta(seconds=i*bucket_sec)
        if i in bins:
            avg_queue = bins[i]["queue_sum"] / max(1, counts[i])
            th = bins[i]["processed_sum"] * 60.0 / float(bucket_sec)
            points.append({
                "t": iso_z(bucket_start),
                "queue_size": int(round(avg_queue)),
                "actual_throughput": round(th, 3)
            })
        else:
            points.append({
                "t": iso_z(bucket_start), 
                "queue_size": None, 
                "actual_throughput": None
            })
    
    return {
        "points": points,
        "units": {"queue_size": "items", "actual_throughput": "items/min"}
    }


# =======================
# Function 2: API Endpoint Handler
# =======================
@app.get("/api/machines/{machine_id}/queue-vs-throughput")
def queue_vs_throughput(
    machine_id: str,
    from_: Optional[str] = Query(None, alias="from", description="ISO8601 start (UTC). Default: now-1h"),
    to: Optional[str] = Query(None, description="ISO8601 end (UTC). Default: now"),
    bucket: str = Query("1m", description="Bucket size, e.g., 10s, 1m, 5m, 1h")
):
    """
    API endpoint to get queue size vs throughput data for a specific machine
    """
    # Validate machine exists
    if not any(m.id == machine_id for m in machines):
        raise HTTPException(404, "machine not found")
    
    # Parse and validate time parameters
    t_end = parse_iso(to, default=now_utc())
    t_start = parse_iso(from_, default=t_end - timedelta(hours=1))
    
    if t_start >= t_end:
        raise HTTPException(400, "from must be < to")
    
    # Parse bucket size
    bucket_sec = parse_bucket_seconds(bucket)
    
    # Process the data using the separated logic
    processed_data = process_queue_throughput_data(
        machine_id, t_start, t_end, bucket_sec, history
    )
    
    # Return formatted response
    return {
        "machine_id": machine_id,
        "from_": iso_z(t_start),
        "to": iso_z(t_end),
        "bucket": bucket,
        **processed_data
    }




