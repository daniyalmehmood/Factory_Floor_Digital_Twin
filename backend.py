# backend_issue_15.py — stages + parallel machines + WebSocket + REST
import asyncio, json, random
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI()

# ===== Serve your HTML (issue_15/index.html) =====
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index15.html"

@app.get("/")
async def read_root():
    # Opens your index.html directly
    return FileResponse(INDEX_FILE)


# ========= DIGITAL TWIN MODEL (Stages with parallel machines) =========
SECONDS_PER_MIN = 60.0
TICK_SECONDS = 2.0

class Machine:
    def __init__(self, id: str, base_rate: float):
        self.id = id
        self.base_rate = base_rate               # items/min (ideal)
        self.throughput_factor = 1.0             # multiplier
        self.uptime = 1.0                        # 0..1
        self.queue = 0.0                         # waiting items at this machine
        self.status = "on"                       # on/off
        self.total_processed = 0.0
        self.last_change = datetime.utcnow().isoformat() + "Z"

    def capacity_this_tick(self, delta_seconds: float, staffing_mod: float) -> float:
        if self.status != "on":
            self.uptime = max(0.0, self.uptime - 0.001 * (delta_seconds / TICK_SECONDS))
            return 0.0
        # tiny random outages / recoveries
        if random.random() < 0.0005:
            self.uptime = max(0.5, self.uptime - random.uniform(0.05, 0.2))
        else:
            self.uptime = min(1.0, self.uptime + 0.0005 * (delta_seconds / TICK_SECONDS))
        ideal_per_tick = self.base_rate * (delta_seconds / SECONDS_PER_MIN)
        return max(0.0, ideal_per_tick * self.throughput_factor * staffing_mod * self.uptime)

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
        self.input_queue = 0.0  # backlog waiting to be routed into machines

    def to_dict(self):
        return {
            "id": self.id,
            "input_queue": round(self.input_queue, 2),
            "machines": [m.to_dict() for m in self.machines],
        }

# Initial line (each stage has one machine; you can add parallel machines later)
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
    "time": datetime.utcnow().isoformat() + "Z",
}

def compute_staffing_modifier(shifts: int) -> float:
    # More shifts => more staff with diminishing returns
    return 1.0 + 0.25 * (shifts - 1)

# ===== Core simulation =====
def route_to_shortest_queue(stage: Stage, amount: float):
    """Route items from stage.input_queue to parallel machines by shortest queue."""
    if amount <= 0 or not stage.machines:
        return
    chunk = max(0.1, amount / 10.0)  # small chunks → more stable
    remaining = amount
    while remaining > 1e-6:
        m = min(stage.machines, key=lambda x: x.queue)
        push = min(chunk, remaining)
        m.queue += push
        remaining -= push

def process_production_tick(delta_seconds: float):
    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])

    # ambient drift
    twin_state["ambient_temp"] = round(min(40, max(15, twin_state["ambient_temp"] + random.uniform(-0.05, 0.05))), 2)
    twin_state["ambient_humidity"] = round(min(80, max(20, twin_state["ambient_humidity"] + random.uniform(-0.1, 0.1))), 2)

    # arrivals into stage 0 depend on staffing and stage0 capacity
    if stages:
        s0 = stages[0]
        ideal = sum(m.base_rate for m in s0.machines) * (delta_seconds / SECONDS_PER_MIN)
        arrivals = max(0.0, random.gauss(ideal * staffing_mod, ideal * 0.2))
        s0.input_queue += arrivals

    # route stage inputs to parallel machines
    for st in stages:
        if st.input_queue > 0:
            amt = st.input_queue
            st.input_queue = 0.0
            route_to_shortest_queue(st, amt)

    # process each stage
    new_finished = 0.0
    for idx, st in enumerate(stages):
        processed_sum = 0.0
        for m in st.machines:
            cap = m.capacity_this_tick(delta_seconds, staffing_mod)
            take = min(m.queue, cap)
            m.queue -= take
            m.total_processed += take
            processed_sum += take
            m.throughput_factor = min(1.8, max(0.2, m.throughput_factor + random.uniform(-0.001, 0.001)))
        if idx < len(stages) - 1:
            stages[idx + 1].input_queue += processed_sum
        else:
            new_finished += processed_sum

    twin_state["total_output"] += int(new_finished)
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

# ===== WebSocket broadcasting =====
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept(); self.active.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active: self.active.remove(websocket)
    async def broadcast(self, message: Dict):
        data = json.dumps(message)
        living = []
        for ws in list(self.active):
            try:
                await ws.send_text(data); living.append(ws)
            except Exception:
                pass
        self.active = living

manager = ConnectionManager()

def current_metrics_payload():
    machines_flat = []
    for st in stages:
        for m in st.machines:
            md = m.to_dict(); md["stage_id"] = st.id
            machines_flat.append(md)
    bnecks = []
    for st in stages:
        if st.input_queue > 5: bnecks.append(f"Stage{st.id}:input")
        for m in st.machines:
            if m.queue > 5: bnecks.append(m.id)
    return {
        "time": twin_state["time"],
        "ambient_temp": twin_state["ambient_temp"],
        "ambient_humidity": twin_state["ambient_humidity"],
        "total_output": twin_state["total_output"],
        "stages": [s.to_dict() for s in stages],
        "machines": machines_flat,
        "bottlenecks": bnecks,
        "staffing_shifts": twin_state["staffing_shifts"],
    }

async def simulator_loop():
    while True:
        process_production_tick(TICK_SECONDS)
        await manager.broadcast({"type": "metrics", "payload": current_metrics_payload()})
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.get_event_loop().create_task(simulator_loop())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "metrics", "payload": current_metrics_payload()}))
        while True:
            obj = json.loads(await websocket.receive_text())
            action = obj.get("action")

            if action == "add_shift":
                twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)

            elif action == "remove_shift":
                twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)

            elif action == "toggle_machine":
                mid = obj.get("machine_id")
                for st in stages:
                    for m in st.machines:
                        if m.id == mid:
                            m.status = "off" if m.status == "on" else "on"
                            m.last_change = datetime.utcnow().isoformat() + "Z"

            elif action == "set_throughput":
                mid = obj.get("machine_id"); val = float(obj.get("value", 1.0))
                for st in stages:
                    for m in st.machines:
                        if m.id == mid:
                            m.throughput_factor = max(0.2, min(2.0, val))
                            m.last_change = datetime.utcnow().isoformat() + "Z"

            elif action == "add_parallel_machine":
                stage_index = int(obj.get("stage_index"))
                new_id = obj.get("machine_id", f"M_new_{stage_index}_{random.randint(100,999)}")
                base_rate = float(obj.get("base_rate", 50))
                st = next((s for s in stages if s.id == stage_index), None)
                if not st:
                    await websocket.send_text(json.dumps({"type":"error","payload":"stage not found"})); continue
                if any(m.id == new_id for m in st.machines):
                    await websocket.send_text(json.dumps({"type":"error","payload":"machine id exists"})); continue
                st.machines.append(Machine(new_id, base_rate))

            elif action == "delete_machine":
                mid = obj.get("machine_id")
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
                    await websocket.send_text(json.dumps({"type":"error","payload":"machine not found"})); continue

            elif action == "add_machine":  # adds a brand-new STAGE at the end
                new_id = obj.get("machine_id", f"M_new_{len(stages)+1}")
                base_rate = float(obj.get("base_rate", 50))
                stages.append(Stage(len(stages)+1, [Machine(new_id, base_rate)]))

            else:
                await websocket.send_text(json.dumps({"type":"error","payload":"unknown action"}))
                continue

            await manager.broadcast({"type": "metrics", "payload": current_metrics_payload()})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# ===== REST API (for acceptance criteria) =====
class NewMachineReq(BaseModel):
    machine_id: str
    base_rate: float = 50.0

@app.get("/api/stages")
async def api_get_stages():
    return {"stages": [s.to_dict() for s in stages]}

@app.get("/api/machines")
async def api_get_machines():
    machines_flat = []
    for st in stages:
        for m in st.machines:
            md = m.to_dict(); md["stage_id"] = st.id
            machines_flat.append(md)
    return {"machines": machines_flat}

@app.post("/api/stages/{stage_id}/machines")
async def api_add_parallel_machine(stage_id: int, req: NewMachineReq):
    st = next((s for s in stages if s.id == stage_id), None)
    if not st:
        return JSONResponse({"ok": False, "error": "stage not found"}, status_code=404)
    if any(m.id == req.machine_id for m in st.machines):
        return JSONResponse({"ok": False, "error": "machine id exists"}, status_code=409)
    st.machines.append(Machine(req.machine_id, req.base_rate))
    await manager.broadcast({"type": "metrics", "payload": current_metrics_payload()})
    return {"ok": True, "stage": st.to_dict()}

@app.delete("/api/machines/{machine_id}")
async def api_delete_machine(machine_id: str):
    for st in stages:
        for i, m in enumerate(list(st.machines)):
            if m.id == machine_id:
                st.input_queue += m.queue
                del st.machines[i]
                await manager.broadcast({"type": "metrics", "payload": current_metrics_payload()})
                return {"ok": True, "stage_id": st.id}
    return JSONResponse({"ok": False, "error": "machine not found"}, status_code=404)
