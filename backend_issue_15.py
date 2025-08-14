# factory_backend.py — Staged Production System with Parallel Machines
# FastAPI + Socket.IO
import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import socketio
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
# ================== CONFIG ==================
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index15.html"   # <-- clearer path
# ================== SOCKET.IO ==================
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Important: Mount static folder so /static/index15.html works
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
# Mount socket.io
app.mount("/socket.io", socketio.ASGIApp(sio))
# Optional: Serve all project files at /by_haya
app.mount("/by_haya", StaticFiles(directory=str(BASE_DIR)), name="by_haya")
# ================== MODELS ==================
class Machine:
    def __init__(self, id: str, base_rate: float):
        self.id = id
        self.base_rate = base_rate
        self.throughput_factor = 1.0
        self.uptime = 1.0
        self.queue = 0.0
        self.status = "on"
        self.total_processed = 0.0
        self.last_change = datetime.now(timezone.utc).isoformat()
    def capacity_this_tick(self, delta_seconds: float, staffing_mod: float) -> float:
        if self.status != "on":
            self.uptime = max(0.0, self.uptime - 0.001 * (delta_seconds / TICK_SECONDS))
            return 0.0
        # Random degradation/repair
        if random.random() < 0.0005:
            self.uptime = max(0.5, self.uptime - random.uniform(0.05, 0.2))
        else:
            self.uptime = min(1.0, self.uptime + 0.0005 * (delta_seconds / TICK_SECONDS))
        ideal = self.base_rate * (delta_seconds / SECONDS_PER_MIN)
        return max(0.0, ideal * self.throughput_factor * staffing_mod * self.uptime)
    def to_dict(self):
        return {
            "id": self.id,
            "base_rate": self.base_rate,
            "throughput_factor": round(self.throughput_factor, 3),
            "uptime": round(self.uptime, 3),
            "queue": round(self.queue, 2),
            "status": self.status,
            "total_processed": int(self.total_processed),
        }
class Stage:
    def __init__(self, id: int, machines: Optional[List[Machine]] = None):
        self.id = id
        self.machines: List[Machine] = machines or []
        self.input_queue = 0.0
    def to_dict(self):
        return {
            "id": self.id,
            "input_queue": round(self.input_queue, 2),
            "machines": [m.to_dict() for m in self.machines],
        }
# ================== INITIAL STATE ==================
stages: List[Stage] = [
    Stage(1, [Machine("M1_cutter", 60)]),
    Stage(2, [Machine("M2_press", 50)]),
    Stage(3, [Machine("M3_paint", 40)]),
    Stage(4, [Machine("M4_inspect", 70)]),
]
twin_state = {
    "staffing_shifts": 1,
    "ambient_temp": 25.0,
    "ambient_humidity": 45.0,
    "total_output": 0,
    "time": datetime.now(timezone.utc).isoformat(),
}
# ================== SIMULATION HELPERS ==================
def staffing_mod(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)
def route_to_shortest_queue(stage: Stage, amount: float):
    if amount <= 0 or not stage.machines:
        return
    chunk = max(0.1, amount / 10.0)
    remaining = amount
    while remaining > 1e-6:
        m = min(stage.machines, key=lambda x: x.queue)
        push = min(chunk, remaining)
        m.queue += push
        remaining -= push
def process_tick(delta_seconds: float):
    smod = staffing_mod(twin_state["staffing_shifts"])
    # Environmental changes
    twin_state["ambient_temp"] = round(
        min(40, max(15, twin_state["ambient_temp"] + random.uniform(-0.05, 0.05))), 2
    )
    twin_state["ambient_humidity"] = round(
        min(80, max(20, twin_state["ambient_humidity"] + random.uniform(-0.1, 0.1))), 2
    )
    # Material arrivals to first stage
    if stages:
        s0 = stages[0]
        ideal = sum(m.base_rate for m in s0.machines) * (delta_seconds / SECONDS_PER_MIN)
        arrivals = max(0.0, random.gauss(ideal * smod, ideal * 0.2))
        s0.input_queue += arrivals
    # Distribute material to machines
    for st in stages:
        if st.input_queue > 0:
            amt = st.input_queue
            st.input_queue = 0.0
            route_to_shortest_queue(st, amt)
    # Process stages
    finished = 0.0
    for idx, st in enumerate(stages):
        processed_sum = 0.0
        for m in st.machines:
            cap = m.capacity_this_tick(delta_seconds, smod)
            take = min(m.queue, cap)
            m.queue -= take
            m.total_processed += take
            processed_sum += take
            m.throughput_factor = min(1.8, max(0.2, m.throughput_factor + random.uniform(-0.001, 0.001)))
        if idx < len(stages) - 1:
            stages[idx + 1].input_queue += processed_sum
        else:
            finished += processed_sum
    twin_state["total_output"] += int(finished)
    twin_state["time"] = datetime.now(timezone.utc).isoformat()
def payload():
    machines_flat = []
    for st in stages:
        for m in st.machines:
            md = m.to_dict()
            md["stage_id"] = st.id
            machines_flat.append(md)
    bottlenecks = []
    for st in stages:
        if st.input_queue > 5:
            bottlenecks.append(f"Stage{st.id}:input")
        for m in st.machines:
            if m.queue > 5:
                bottlenecks.append(m.id)
    return {
        "time": twin_state["time"],
        "ambient_temp": twin_state["ambient_temp"],
        "ambient_humidity": twin_state["ambient_humidity"],
        "total_output": twin_state["total_output"],
        "stages": [s.to_dict() for s in stages],
        "machines": machines_flat,
        "bottlenecks": bottlenecks,
        "staffing_shifts": twin_state["staffing_shifts"],
    }
# ================== BACKGROUND SIMULATOR ==================
async def simulator_loop():
    while True:
        process_tick(TICK_SECONDS)
        await sio.emit("metrics", {"type": "metrics", "payload": payload()})
        await asyncio.sleep(TICK_SECONDS)
@app.on_event("startup")
async def on_start():
    asyncio.get_event_loop().create_task(simulator_loop())
# ================== SOCKET.IO EVENTS ==================
@sio.event
async def connect(sid, environ):
    await sio.emit("metrics", {"type": "metrics", "payload": payload()}, to=sid)
@sio.event
async def disconnect(sid):
    pass
@sio.event
async def action(sid, data):
    # ... (as is, logic unchanged)
    try:
        act = data.get("action")
        if act == "add_shift":
            twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)
        elif act == "remove_shift":
            twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)
        elif act == "toggle_machine":
            mid = data.get("machine_id")
            for st in stages:
                for m in st.machines:
                    if m.id == mid:
                        m.status = "off" if m.status == "on" else "on"
                        m.last_change = datetime.now(timezone.utc).isoformat()
        elif act == "set_throughput":
            mid = data.get("machine_id")
            val = float(data.get("value", 1.0))
            for st in stages:
                for m in st.machines:
                    if m.id == mid:
                        m.throughput_factor = max(0.2, min(2.0, val))
                        m.last_change = datetime.now(timezone.utc).isoformat()
        elif act == "add_parallel_machine":
            stage_index = int(data.get("stage_index"))
            new_id = data.get("machine_id") or f"M_new_{stage_index}_{random.randint(100,999)}"
            base_rate = float(data.get("base_rate", 50))
            st = next((s for s in stages if s.id == stage_index), None)
            if not st:
                await sio.emit("error", {"payload": "stage not found"}, to=sid)
                return
            if any(m.id == new_id for m in st.machines):
                await sio.emit("error", {"payload": "machine id exists"}, to=sid)
                return
            st.machines.append(Machine(new_id, base_rate))
        elif act == "delete_machine":
            mid = data.get("machine_id")
            found = False
            for st in stages:
                for i, m in enumerate(list(st.machines)):
                    if m.id == mid:
                        st.input_queue += m.queue
                        del st.machines[i]
                        found = True
                        break
                if found: break
            if not found:
                await sio.emit("error", {"payload": "machine not found"}, to=sid)
                return
        elif act == "add_machine":
            new_id = data.get("machine_id", f"M_new_{len(stages)+1}")
            base_rate = float(data.get("base_rate", 50))
            stages.append(Stage(len(stages)+1, [Machine(new_id, base_rate)]))
        else:
            await sio.emit("error", {"payload": "unknown action"}, to=sid)
            return
        await sio.emit("metrics", {"type": "metrics", "payload": payload()})
    except Exception as e:
        await sio.emit("error", {"payload": str(e)}, to=sid)
# ================== REST API ==================
@app.get("/")
async def index():
    return FileResponse(INDEX_FILE)
@app.get("/api/stages")
async def api_stages():
    return {"stages": [s.to_dict() for s in stages]}
@app.get("/api/machines")
async def api_machines():
    ms = []
    for st in stages:
        for m in st.machines:
            md = m.to_dict()
            md["stage_id"] = st.id
            ms.append(md)
    return {"machines": ms}