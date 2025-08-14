import os, asyncio, json, random
from collections import deque, defaultdict
from typing import List, Dict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

BASE_DIR   = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"  # Use local static directory
INDEX_HTML = STATIC_DIR / "index16.html"

app = FastAPI(title="Factory Digital Twin · Issue 16")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# Remove or comment out the by_haya mount if present
# app.mount("/by_haya", StaticFiles(directory=str(BASE_DIR)), name="by_haya")

@app.get("/")
def root_page():
    return FileResponse(str(INDEX_HTML))

# =======================
# Simulation settings
# =======================
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0
HISTORY_MAXLEN = int((6*60*60) / max(TICK_SECONDS,1.0))

def now_utc(): return datetime.now(timezone.utc)
def iso_z(dt: datetime) -> str: return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

class Machine:
    def __init__(self, id, base_rate, position):
        self.id = id
        self.base_rate = base_rate
        self.position = position
        self.throughput_factor = 1.0
        self.uptime = 1.0
        self.queue = 0.0
        self.status = "on"
        self.total_processed = 0.0
    def to_dict(self):
        return {
            "id": self.id,
            "base_rate": self.base_rate,
            "throughput_factor": round(self.throughput_factor,3),
            "uptime": round(self.uptime,3),
            "queue": round(self.queue,2),
            "position": self.position,
            "status": self.status,
            "total_processed": int(self.total_processed)
        }

machines: List[Machine] = [
    Machine("M1_cutter", 60, 1),
    Machine("M2_press", 50, 2),
    Machine("M3_paint", 40, 3),
    Machine("M4_inspect", 70, 4),
]

history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))

twin_state = {
    "staffing_shifts": 1,
    "ambient_temp": 25.0,
    "ambient_humidity": 45.0,
    "total_output": 0,
    "time": iso_z(now_utc())
}

def compute_staffing_modifier(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)

def process_production_tick(delta_seconds: float):
    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])
    twin_state["ambient_temp"] += random.uniform(-0.05,0.05)
    twin_state["ambient_humidity"] += random.uniform(-0.1,0.1)
    twin_state["ambient_temp"] = round(max(15,min(40,twin_state["ambient_temp"])),2)
    twin_state["ambient_humidity"] = round(max(20,min(80,twin_state["ambient_humidity"])),2)

    machines_sorted = sorted(machines, key=lambda m: m.position)
    new_output = 0.0
    tick_ts = now_utc()

    for i, m in enumerate(machines_sorted):
        if m.status != "on":
            m.uptime = max(0.0, m.uptime - 0.001*(delta_seconds/TICK_SECONDS))
            history[m.id].append((tick_ts,float(m.queue),0.0))
            continue

        if random.random() < 0.0005:
            m.uptime = max(0.5, m.uptime - random.uniform(0.05,0.2))
        else:
            m.uptime = min(1.0, m.uptime + 0.0005*(delta_seconds/TICK_SECONDS))

        ideal_per_tick = m.base_rate*(delta_seconds/SECONDS_PER_MIN)
        effective_rate = ideal_per_tick*m.throughput_factor*staffing_mod*m.uptime

        if i==0:
            arrivals = max(0.0, random.gauss(ideal_per_tick*staffing_mod, ideal_per_tick*0.2))
            m.queue += arrivals

        processed = min(m.queue, max(0.0,effective_rate))
        m.queue -= processed
        m.total_processed += processed

        if i < len(machines_sorted)-1:
            machines_sorted[i+1].queue += processed
        else:
            new_output += processed

        m.throughput_factor += random.uniform(-0.001,0.001)
        m.throughput_factor = max(0.2,min(1.8,m.throughput_factor))

        history[m.id].append((tick_ts,float(m.queue),float(processed)))

    twin_state["total_output"] += int(new_output)
    twin_state["time"] = iso_z(now_utc())

# =======================
# WebSocket broadcast
# =======================
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
    async def connect(self, ws: WebSocket):
        await ws.accept(); self.active.append(ws)
    def disconnect(self, ws: WebSocket):
        if ws in self.active: self.active.remove(ws)
    async def broadcast(self, msg: Dict):
        data = json.dumps(msg); alive=[]
        for ws in list(self.active):
            try: await ws.send_text(data); alive.append(ws)
            except: pass
        self.active = alive

manager = ConnectionManager()

async def simulator_loop():
    while True:
        process_production_tick(TICK_SECONDS)
        snapshot = [m.to_dict() for m in sorted(machines, key=lambda x: x.position)]
        bottlenecks = [m["id"] for m in snapshot if m["queue"]>5]
        metrics = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": snapshot,
            "bottlenecks": bottlenecks,
            "staffing_shifts": twin_state["staffing_shifts"]
        }
        await manager.broadcast({"type":"metrics","payload":metrics})
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(simulator_loop())

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # send initial
        await ws.send_text(json.dumps({"type":"metrics","payload":{
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines":[m.to_dict() for m in sorted(machines,key=lambda x:x.position)],
            "bottlenecks":[],
            "staffing_shifts":twin_state["staffing_shifts"]
        }}))
        while True:
            obj = json.loads(await ws.receive_text())
            action = obj.get("action")
            if action=="add_shift": twin_state["staffing_shifts"]=min(3,twin_state["staffing_shifts"]+1)
            elif action=="remove_shift": twin_state["staffing_shifts"]=max(1,twin_state["staffing_shifts"]-1)
            elif action=="toggle_machine":
                mid=obj.get("machine_id")
                for m in machines:
                    if m.id==mid:
                        m.status="off" if m.status=="on" else "on"; break
            elif action=="move_equipment":
                mid=obj.get("machine_id"); pos=int(obj.get("new_position",1))
                for m in machines:
                    if m.id==mid: m.position=pos; break
                for idx,m in enumerate(sorted(machines,key=lambda x:x.position),start=1): m.position=idx
            elif action=="set_throughput":
                mid=obj.get("machine_id"); val=float(obj.get("value",1.0))
                for m in machines:
                    if m.id==mid: m.throughput_factor=max(0.2,min(2.0,val)); break
            elif action=="add_machine":
                mid=obj.get("machine_id",f"M_new_{len(machines)+1}")
                pos=int(obj.get("position",len(machines)+1))
                rate=float(obj.get("base_rate",50))
                machines.append(Machine(mid,rate,pos))
                for idx,m in enumerate(sorted(machines,key=lambda x:x.position),start=1): m.position=idx
                _=history[mid]
            # else ignore unknown
            snapshot = [m.to_dict() for m in sorted(machines,key=lambda x:x.position)]
            bottlenecks = [m["id"] for m in snapshot if m["queue"]>5]
            await manager.broadcast({"type":"metrics","payload":{
                "time": twin_state["time"],
                "ambient_temp": twin_state["ambient_temp"],
                "ambient_humidity": twin_state["ambient_humidity"],
                "total_output": twin_state["total_output"],
                "machines": snapshot,
                "bottlenecks": bottlenecks,
                "staffing_shifts": twin_state["staffing_shifts"]
            }})
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
