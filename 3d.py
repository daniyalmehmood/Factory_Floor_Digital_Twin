# backend.py
import asyncio
import json
import math
import random
import uuid
from datetime import datetime
from typing import Dict, List, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI()

# Allow local testing from file:// or localhost dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files and templates (optional)
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    templates = Jinja2Templates(directory="static")
except Exception:
    # Handle case where static directory doesn't exist
    pass

# -----------------------
# PYDANTIC MODELS
# -----------------------
class MachineIn(BaseModel):
    name: str
    base_rate: float = 50.0  # items per minute
    position: int = 1

# -----------------------
# DIGITAL TWIN MODEL
# -----------------------
class Machine:
    def __init__(self, id: str, name: str, base_rate: float, position: int):
        self.id = id
        self.name = name
        self.base_rate = base_rate  # items per minute
        self.throughput_factor = 1.0
        self.uptime = 1.0  # fraction
        self.queue = 0.0
        self.position = position
        self.status = "on"
        self.total_processed = 0.0
        self.last_change = datetime.utcnow().isoformat() + "Z"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "base_rate": self.base_rate,
            "throughput_factor": round(self.throughput_factor, 3),
            "uptime": round(self.uptime, 3),
            "queue": round(self.queue, 2),
            "position": self.position,
            "status": self.status,
            "total_processed": int(self.total_processed),
            "last_change": self.last_change
        }

# In-memory store for machines
machines: Dict[str, Machine] = {}
machines_lock = asyncio.Lock()

# Initialize with default machines
def initialize_default_machines():
    default_machines = [
        ("M1_cutter", "Cutting Station", 60, 1),
        ("M2_press", "Press Station", 50, 2),
        ("M3_paint", "Paint Station", 40, 3),
        ("M4_inspect", "Inspection Station", 70, 4),
    ]
    for mid, name, rate, pos in default_machines:
        machines[mid] = Machine(mid, name, rate, pos)

initialize_default_machines()

# global twin state
twin_state = {
    "staffing_shifts": 1,   # number of active shifts (affects throughput)
    "ambient_temp": 25.0,
    "ambient_humidity": 45.0,
    "last_output_total": 0,
    "total_output": 0,      # finished products
    "time": datetime.utcnow().isoformat() + "Z",
}

# -----------------------
# HELPER FUNCTIONS
# -----------------------
TICK_SECONDS = 2.0  # update interval in seconds
SECONDS_PER_MIN = 60.0

def compute_staffing_modifier(shifts: int) -> float:
    # More shifts -> more staff -> higher throughput, with diminishing returns.
    return 1.0 + 0.25 * (shifts - 1)

def machines_snapshot():
    """Get snapshot of all machines as list of dicts"""
    return [m.to_dict() for m in sorted(machines.values(), key=lambda x: x.position)]

def process_production_tick(delta_seconds: float):
    """Simulate one tick across the pipeline."""
    global twin_state, machines

    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])

    # Ambient drift
    twin_state["ambient_temp"] += random.uniform(-0.05, 0.05)
    twin_state["ambient_humidity"] += random.uniform(-0.1, 0.1)
    twin_state["ambient_temp"] = round(max(15, min(40, twin_state["ambient_temp"])), 2)
    twin_state["ambient_humidity"] = round(max(20, min(80, twin_state["ambient_humidity"])), 2)

    # For each machine, compute how many items it processed this tick from its queue or upstream
    # Order machines by position for pipeline
    machines_sorted = sorted(machines.values(), key=lambda m: m.position)
    new_output = 0.0

    for i, m in enumerate(machines_sorted):
        if m.status != "on":
            # If machine is off, nothing processes
            m.uptime = max(0.0, m.uptime - 0.001 * (delta_seconds / TICK_SECONDS))
            continue

        # simulate random small failures impacting uptime
        if random.random() < 0.0005:  # very small chance of transient drop
            m.uptime = max(0.5, m.uptime - random.uniform(0.05, 0.2))
        else:
            # recover slowly to 1.0
            m.uptime = min(1.0, m.uptime + 0.0005 * (delta_seconds / TICK_SECONDS))

        # available processing capacity this tick (items)
        # base_rate (items per minute) -> per tick: base_rate * (delta_seconds/60)
        ideal_per_tick = m.base_rate * (delta_seconds / SECONDS_PER_MIN)
        effective_rate = ideal_per_tick * m.throughput_factor * staffing_mod * m.uptime

        # Machine pulls from its own queue first (backlog)
        # If first machine, it generates new raw items (arrivals)
        if i == 0:
            # generate new raw items based on upstream feed (random arrivals influenced by staffing)
            arrivals = max(0.0, random.gauss(ideal_per_tick * staffing_mod, ideal_per_tick * 0.2))
            m.queue += arrivals

        # processable items = min(queue, capacity)
        processed = min(m.queue, max(0.0, effective_rate))
        # reduce queue
        m.queue -= processed
        m.total_processed += processed

        # push processed to next machine's queue, or to output if last
        if i < len(machines_sorted) - 1:
            machines_sorted[i + 1].queue += processed
        else:
            new_output += processed

        # small random fluctuation to throughput_factor to simulate wear or maintenance
        m.throughput_factor += random.uniform(-0.001, 0.001)
        m.throughput_factor = max(0.2, min(1.8, m.throughput_factor))

    twin_state["total_output"] += int(new_output)
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

# -----------------------
# WEBSOCKET MANAGER
# -----------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: Dict):
        data = json.dumps(message) if isinstance(message, dict) else message
        living = []
        for ws in list(self.active):
            try:
                if isinstance(data, str):
                    await ws.send_text(data)
                else:
                    await ws.send_json(data)
                living.append(ws)
            except Exception:
                # drop broken websockets
                pass
        self.active = living

manager = ConnectionManager()

# -----------------------
# API ENDPOINTS
# -----------------------
@app.get('/api/machines')
async def list_machines():
    """Get list of all machines"""
    return machines_snapshot()

@app.post('/api/machines', status_code=201)
async def create_machine(payload: MachineIn):
    """Create a new machine"""
    async with machines_lock:
        mid = str(uuid.uuid4())
        m = Machine(mid, payload.name, payload.base_rate, payload.position)
        machines[mid] = m
    
    # Normalize positions to avoid conflicts
    machines_sorted = sorted(machines.values(), key=lambda x: x.position)
    for idx, machine in enumerate(machines_sorted, start=1):
        machine.position = idx
    
    # Broadcast update
    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot()})
    return m.to_dict()

@app.put('/api/machines/{machine_id}')
async def update_machine(machine_id: str, payload: MachineIn):
    """Update an existing machine"""
    async with machines_lock:
        if machine_id not in machines:
            raise HTTPException(status_code=404, detail='Machine not found')
        
        m = machines[machine_id]
        m.name = payload.name
        m.base_rate = payload.base_rate
        m.position = payload.position
        m.last_change = datetime.utcnow().isoformat() + "Z"
    
    # Normalize positions
    machines_sorted = sorted(machines.values(), key=lambda x: x.position)
    for idx, machine in enumerate(machines_sorted, start=1):
        machine.position = idx

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot()})
    return m.to_dict()

@app.delete('/api/machines/{machine_id}')
async def delete_machine(machine_id: str):
    """Delete a machine"""
    async with machines_lock:
        if machine_id not in machines:
            raise HTTPException(status_code=404, detail='Machine not found')

        m = machines.pop(machine_id)
        m.queue = 0.0  # Clear its queue

    # Normalize remaining machine positions
    machines_sorted = sorted(machines.values(), key=lambda x: x.position)
    for idx, machine in enumerate(machines_sorted, start=1):
        machine.position = idx

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot()})
    return {'status': 'deleted', 'id': machine_id}

# -----------------------
# BACKGROUND TASK: simulator broadcaster
# -----------------------
async def simulator_loop():
    """Run the production simulation and broadcast the twin state every tick."""
    while True:
        if machines:  # Only process if we have machines
            process_production_tick(TICK_SECONDS)
            
            # compute simple metrics for dashboard
            machines_snapshot_data = machines_snapshot()
            # find bottlenecks (high queue)
            bottlenecks = [m["id"] for m in machines_snapshot_data if m["queue"] > max(2.0, 0.5 * (m["base_rate"]/SECONDS_PER_MIN) * TICK_SECONDS)]
            
            metrics = {
                "time": twin_state["time"],
                "ambient_temp": twin_state["ambient_temp"],
                "ambient_humidity": twin_state["ambient_humidity"],
                "total_output": twin_state["total_output"],
                "machines": machines_snapshot_data,
                "bottlenecks": bottlenecks,
                "staffing_shifts": twin_state["staffing_shifts"],
            }
            
            # broadcast to all dashboards
            await manager.broadcast({"type": "metrics", "payload": metrics})
        
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    # start background simulator
    loop = asyncio.get_event_loop()
    loop.create_task(simulator_loop())

# -----------------------
# WEBSOCKET ENDPOINTS
# -----------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # on connect, send immediate snapshot
        initial_payload = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": machines_snapshot(),
            "bottlenecks": [],
            "staffing_shifts": twin_state["staffing_shifts"],
        }
        await websocket.send_text(json.dumps({"type": "metrics", "payload": initial_payload}))
        
        while True:
            msg = await websocket.receive_text()
            try:
                obj = json.loads(msg)
            except Exception:
                await websocket.send_text(json.dumps({"type":"error","payload":"invalid json"}))
                continue

            # Handle commands: add_shift, remove_shift, move_equipment, toggle_machine, set_throughput, etc.
            action = obj.get("action")
            
            if action == "ping":
                await websocket.send_json({'type': 'pong'})
                continue
                
            elif action == "add_shift":
                twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)
                
            elif action == "remove_shift":
                twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)
                
            elif action == "move_equipment":
                mid = obj.get("machine_id")
                new_pos = obj.get("new_position")
                if mid in machines:
                    machines[mid].position = int(new_pos)
                    machines[mid].last_change = datetime.utcnow().isoformat() + "Z"
                    # Normalize positions
                    machines_sorted = sorted(machines.values(), key=lambda x: x.position)
                    for idx, m in enumerate(machines_sorted, start=1):
                        m.position = idx
                        
            elif action == "toggle_machine":
                mid = obj.get("machine_id")
                if mid in machines:
                    m = machines[mid]
                    m.status = "off" if m.status == "on" else "on"
                    m.last_change = datetime.utcnow().isoformat() + "Z"
                    
            elif action == "set_throughput":
                mid = obj.get("machine_id")
                val = float(obj.get("value", 1.0))
                if mid in machines:
                    machines[mid].throughput_factor = max(0.2, min(2.0, val))
                    machines[mid].last_change = datetime.utcnow().isoformat() + "Z"
                    
            elif action == "add_machine":
                new_id = obj.get("machine_id", f"M_new_{len(machines)+1}")
                name = obj.get("name", f"Machine {len(machines)+1}")
                pos = int(obj.get("position", len(machines)+1))
                base_rate = float(obj.get("base_rate", 50))
                
                async with machines_lock:
                    machines[new_id] = Machine(new_id, name, base_rate, pos)
                    # normalize positions
                    machines_sorted = sorted(machines.values(), key=lambda x: x.position)
                    for idx, m in enumerate(machines_sorted, start=1):
                        m.position = idx
                        
            else:
                await websocket.send_text(json.dumps({"type":"error","payload":"unknown action"}))
                continue

            # after handling command, send a state update
            current_payload = {
                "time": twin_state["time"],
                "ambient_temp": twin_state["ambient_temp"],
                "ambient_humidity": twin_state["ambient_humidity"],
                "total_output": twin_state["total_output"],
                "machines": machines_snapshot(),
                "bottlenecks": [m_id for m_id, m in machines.items() if m.queue > 5],
                "staffing_shifts": twin_state["staffing_shifts"],
            }
            await manager.broadcast({"type": "metrics", "payload": current_payload})
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)

# Optional: serve a simple HTML page if templates are available
@app.get("/")
async def get_dashboard():
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Factory Digital Twin</title>
    </head>
    <body>
        <h1>Factory Digital Twin Dashboard</h1>
        <p>WebSocket endpoint: /ws</p>
        <p>API endpoints:</p>
        <ul>
            <li>GET /api/machines - List all machines</li>
            <li>POST /api/machines - Create new machine</li>
            <li>PUT /api/machines/{id} - Update machine</li>
            <li>DELETE /api/machines/{id} - Delete machine</li>
        </ul>
    </body>
    </html>
    """)

# Run with: uvicorn backend:app --reload --port 8000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)