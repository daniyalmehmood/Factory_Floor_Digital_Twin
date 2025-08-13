# backend.py
import asyncio
import json
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List
import threading
import time
import os
import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import paho.mqtt.client as mqtt  # for optional external ingestion

# -------------------
# CONFIG
# -------------------
DB_PATH = "twin_history.db"
API_KEY = os.environ.get("FACTORY_API_KEY", "secret123")  # simple API key for protected actions
MQTT_ENABLED = False  # set True to enable external MQTT ingest
MQTT_BROKER = os.environ.get("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "/factory/digitaltwin/sensors")
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# -------------------
# DATABASE (SQLite) - with migration support
# -------------------
def get_db_version(conn):
    """Get current database schema version"""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        if cursor.fetchone():
            cursor.execute("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
            result = cursor.fetchone()
            return result[0] if result else 0
        return 0
    except:
        return 0

def set_db_version(conn, version):
    """Set database schema version"""
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    cursor.execute("INSERT INTO schema_version (version, updated_at) VALUES (?, ?)", 
                  (version, datetime.utcnow().isoformat() + "Z"))
    conn.commit()

def check_column_exists(conn, table_name, column_name):
    """Check if a column exists in a table"""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns

def migrate_database(conn):
    """Handle database migrations"""
    current_version = get_db_version(conn)
    cursor = conn.cursor()
    
    # Migration to version 1: Add description and created_at columns to presets
    if current_version < 1:
        print("Migrating database to version 1...")
        try:
            # Check if presets table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='presets'")
            if cursor.fetchone():
                # Add missing columns if they don't exist
                if not check_column_exists(conn, 'presets', 'description'):
                    cursor.execute("ALTER TABLE presets ADD COLUMN description TEXT")
                    print("Added 'description' column to presets table")
                
                if not check_column_exists(conn, 'presets', 'created_at'):
                    cursor.execute("ALTER TABLE presets ADD COLUMN created_at TEXT")
                    print("Added 'created_at' column to presets table")
                    
                    # Set default created_at for existing records
                    default_date = datetime.utcnow().isoformat() + "Z"
                    cursor.execute("UPDATE presets SET created_at = ? WHERE created_at IS NULL", (default_date,))
                    print("Set default created_at for existing presets")
            
            conn.commit()
            set_db_version(conn, 1)
            print("Database migration to version 1 completed")
        except Exception as e:
            print(f"Migration to version 1 failed: {e}")
            conn.rollback()
    
    # Future migrations can be added here
    # if current_version < 2:
    #     # Migration logic for version 2

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    
    # Create metrics table
    c.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    """)
    
    # Create presets table with all columns
    c.execute("""
    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        config TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    
    conn.commit()
    
    # Run migrations
    migrate_database(conn)
    
    return conn

db_conn = init_db()
db_lock = threading.Lock()

def save_metrics_snapshot(snapshot: dict):
    """Save snapshot (dict) into SQLite DB as JSON."""
    try:
        with db_lock:
            cur = db_conn.cursor()
            cur.execute("INSERT INTO metrics (ts, payload) VALUES (?, ?)", (snapshot["time"], json.dumps(snapshot)))
            db_conn.commit()
    except Exception as e:
        print("DB save error:", e)

def query_history(minutes: int = 60):
    """Return raw metric snapshots for last N minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    cutoff_iso = cutoff.isoformat() + "Z"
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT ts, payload FROM metrics WHERE ts >= ? ORDER BY ts ASC", (cutoff_iso,))
        rows = cur.fetchall()
    results = []
    for ts, payload in rows:
        try:
            results.append(json.loads(payload))
        except:
            pass
    return results

# -------------------
# Digital Twin model (same core idea as earlier)
# -------------------
class Machine:
    def __init__(self, id, base_rate, position):
        self.id = id
        self.base_rate = base_rate
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

    def from_dict(self, data: dict):
        """Restore machine state from saved data"""
        self.base_rate = float(data.get("base_rate", self.base_rate))
        self.throughput_factor = float(data.get("throughput_factor", 1.0))
        self.uptime = float(data.get("uptime", 1.0))
        self.queue = float(data.get("queue", 0.0))
        self.position = int(data.get("position", self.position))
        self.status = data.get("status", "on")
        self.total_processed = float(data.get("total_processed", 0.0))

# Initial default machines
DEFAULT_MACHINES = [
    {"id": "M1_cutter", "base_rate": 60, "position": 1},
    {"id": "M2_press", "base_rate": 50, "position": 2},
    {"id": "M3_paint", "base_rate": 40, "position": 3},
    {"id": "M4_inspect", "base_rate": 70, "position": 4},
]

machines: List[Machine] = [
    Machine(m["id"], m["base_rate"], m["position"]) for m in DEFAULT_MACHINES
]

# Initial twin state
INITIAL_TWIN_STATE = {
    "staffing_shifts": 1,
    "ambient_temp": 25.0,
    "ambient_humidity": 45.0,
    "total_output": 0,
}

twin_state = INITIAL_TWIN_STATE.copy()
twin_state["time"] = datetime.utcnow().isoformat() + "Z"

# -------------------
# Helper functions
# -------------------
def validate_preset_name(name: str) -> str:
    """Validate and sanitize preset name"""
    if not name or not name.strip():
        raise ValueError("Preset name cannot be empty")
    
    name = name.strip()
    if len(name) > 50:
        raise ValueError("Preset name cannot exceed 50 characters")
    
    if not re.match(r'^[a-zA-Z0-9\s\-_\.]+$', name):
        raise ValueError("Preset name can only contain letters, numbers, spaces, hyphens, underscores, and periods")
    
    return name

def reset_simulation():
    """Reset simulation to initial state"""
    global machines, twin_state
    
    # Reset machines to defaults
    machines.clear()
    machines.extend([Machine(m["id"], m["base_rate"], m["position"]) for m in DEFAULT_MACHINES])
    
    # Reset twin state
    twin_state.clear()
    twin_state.update(INITIAL_TWIN_STATE.copy())
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

def normalize_machine_positions():
    """Ensure machine positions are sequential starting from 1"""
    sorted_machines = sorted(machines, key=lambda x: x.position)
    for idx, machine in enumerate(sorted_machines, start=1):
        machine.position = idx

# -------------------
# Helper simulation logic (unchanged)
# -------------------
def compute_staffing_modifier(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)

def process_production_tick(delta_seconds: float):
    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])

    # ambient drift
    twin_state["ambient_temp"] += random.uniform(-0.05, 0.05)
    twin_state["ambient_humidity"] += random.uniform(-0.1, 0.1)
    twin_state["ambient_temp"] = round(max(15, min(40, twin_state["ambient_temp"])), 2)
    twin_state["ambient_humidity"] = round(max(20, min(80, twin_state["ambient_humidity"])), 2)

    machines_sorted = sorted(machines, key=lambda m: m.position)
    new_output = 0.0

    for i, m in enumerate(machines_sorted):
        if m.status != "on":
            m.uptime = max(0.0, m.uptime - 0.001 * (delta_seconds / TICK_SECONDS))
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

    twin_state["total_output"] += int(new_output)
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

# -------------------
# WebSocket manager for dashboards (unchanged)
# -------------------
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
        data = json.dumps(message)
        living = []
        for ws in list(self.active):
            try:
                await ws.send_text(data)
                living.append(ws)
            except Exception:
                pass
        self.active = living

manager = ConnectionManager()

# -------------------
# Simulator loop (unchanged)
# -------------------
async def simulator_loop():
    while True:
        process_production_tick(TICK_SECONDS)
        machines_snapshot = [m.to_dict() for m in sorted(machines, key=lambda x: x.position)]
        bottlenecks = [m["id"] for m in machines_snapshot if m["queue"] > max(2.0, 0.5 * (m["base_rate"]/SECONDS_PER_MIN) * TICK_SECONDS)]
        metrics = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": machines_snapshot,
            "bottlenecks": bottlenecks,
            "staffing_shifts": twin_state["staffing_shifts"],
        }
        # broadcast
        await manager.broadcast({"type": "metrics", "payload": metrics})
        # persist
        save_metrics_snapshot(metrics)
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    # start simulation background task
    loop = asyncio.get_event_loop()
    loop.create_task(simulator_loop())

    # start optional MQTT ingest in a separate thread so it doesn't block uvicorn
    if MQTT_ENABLED:
        t = threading.Thread(target=start_mqtt_client, daemon=True)
        t.start()

# -------------------
# MQTT ingestion (unchanged)
# -------------------
def on_mqtt_connect(client, userdata, flags, rc):
    print("MQTT connected:", rc)
    client.subscribe(MQTT_TOPIC)

def on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except:
        return
    # Simple mapping: if payload has sensor_id, value, type -> apply
    sid = payload.get("sensor_id")
    mtype = payload.get("type")
    val = payload.get("value")
    if sid and mtype:
        # find machine
        for m in machines:
            if m.id == sid:
                if mtype == "queue_increase":
                    m.queue += float(val or 0)
                elif mtype == "throughput_factor":
                    m.throughput_factor = float(val or m.throughput_factor)
                elif mtype == "toggle":
                    m.status = "off" if m.status == "on" else "on"
                # broadcast an immediate update
                snapshot = {
                    "time": datetime.utcnow().isoformat() + "Z",
                    "ambient_temp": twin_state["ambient_temp"],
                    "ambient_humidity": twin_state["ambient_humidity"],
                    "total_output": twin_state["total_output"],
                    "machines": [x.to_dict() for x in sorted(machines, key=lambda z: z.position)],
                    "bottlenecks": [x.id for x in machines if x.queue > 5],
                    "staffing_shifts": twin_state["staffing_shifts"],
                }
                # schedule broadcast on event loop
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon_threadsafe(asyncio.create_task, manager.broadcast({"type": "metrics", "payload": snapshot}))
                except Exception:
                    pass
                break

def start_mqtt_client():
    client = mqtt.Client()
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_forever()
    except Exception as e:
        print("MQTT error:", e)

# -------------------
# WebSocket endpoint (unchanged from your fixed version)
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
            msg = await websocket.receive_text()
            try:
                obj = json.loads(msg)
            except:
                await websocket.send_text(json.dumps({"type":"error","payload":"invalid json"}))
                continue

            # Expect commands to include optional "api_key" for protected actions
            action = obj.get("action")
            key = obj.get("api_key") or ""
            # Unprotected actions: request snapshot
            if action == "get_snapshot":
                snapshot = {
                    "time": twin_state["time"],
                    "ambient_temp": twin_state["ambient_temp"],
                    "ambient_humidity": twin_state["ambient_humidity"],
                    "total_output": twin_state["total_output"],
                    "machines": [m.to_dict() for m in sorted(machines, key=lambda x: x.position)],
                    "bottlenecks": [m.id for m in machines if m.queue > 5],
                    "staffing_shifts": twin_state["staffing_shifts"],
                }
                await websocket.send_text(json.dumps({"type":"metrics","payload": snapshot}))
                continue

            # Protected commands require API_KEY
            if key != API_KEY:
                await websocket.send_text(json.dumps({"type":"error","payload":"missing or invalid api_key"}))
                continue

            # handle protected action commands
            if action == "add_shift":
                twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)
            elif action == "remove_shift":
                twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)
            elif action == "move_equipment":
                mid = obj.get("machine_id"); new_pos = int(obj.get("new_position", 1))
                for m in machines:
                    if m.id == mid:
                        m.position = new_pos
                        break
                normalize_machine_positions()
            elif action == "toggle_machine":
                mid = obj.get("machine_id")
                for m in machines:
                    if m.id == mid:
                        m.status = "off" if m.status == "on" else "on"
                        break
            elif action == "set_throughput":
                mid = obj.get("machine_id"); val = float(obj.get("value", 1.0))
                for m in machines:
                    if m.id == mid:
                        m.throughput_factor = max(0.2, min(2.0, val))
                        break
            elif action == "add_machine":
                new_id = obj.get("machine_id", f"M_new_{len(machines)+1}")
                pos = int(obj.get("position", len(machines)+1))
                base_rate = float(obj.get("base_rate", 50))
                machines.append(Machine(new_id, base_rate, pos))
                normalize_machine_positions()
            elif action == "import_config":
                # replace machines/state with supplied config
                cfg = obj.get("config")
                if isinstance(cfg, dict):
                    # simple mapping, expect keys 'machines' list and 'twin_state'
                    new_machines = []
                    for m in cfg.get("machines", []):
                        new_machines.append(Machine(m["id"], float(m.get("base_rate", 50)), int(m.get("position", 1))))
                    if new_machines:
                        machines.clear()
                        machines.extend(new_machines)
                    # load twin-level items
                    ts = cfg.get("twin_state", {})
                    twin_state["staffing_shifts"] = int(ts.get("staffing_shifts", twin_state["staffing_shifts"]))
                    twin_state["ambient_temp"] = float(ts.get("ambient_temp", twin_state["ambient_temp"]))
                    twin_state["ambient_humidity"] = float(ts.get("ambient_humidity", twin_state["ambient_humidity"]))
                else:
                    await websocket.send_text(json.dumps({"type":"error","payload":"invalid config payload"}))
                    continue
            else:
                await websocket.send_text(json.dumps({"type":"error","payload":"unknown action"}))
                continue

            # After action, broadcast updated snapshot
            snapshot = {
                "time": twin_state["time"],
                "ambient_temp": twin_state["ambient_temp"],
                "ambient_humidity": twin_state["ambient_humidity"],
                "total_output": twin_state["total_output"],
                "machines": [m.to_dict() for m in sorted(machines, key=lambda x: x.position)],
                "bottlenecks": [m.id for m in machines if m.queue > 5],
                "staffing_shifts": twin_state["staffing_shifts"],
            }
            await manager.broadcast({"type": "metrics", "payload": snapshot})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

# -------------------
# HTTP endpoints: enhanced with better error handling
# -------------------
def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

@app.get("/")
async def index():
    return FileResponse("index.html")

@app.get("/api/history")
async def api_history(minutes: int = 60):
    # returns list of snapshots for last N minutes
    rows = query_history(minutes)
    return {"minutes": minutes, "count": len(rows), "snapshots": rows}

@app.get("/api/latest")
async def api_latest():
    # return latest saved snapshot (last row)
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT payload FROM metrics ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
    if not r:
        return {}
    return json.loads(r[0])

@app.post("/api/export")
async def api_export(x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    data = {
        "twin_state": twin_state,
        "machines": [m.to_dict() for m in machines],
    }
    return JSONResponse(content=data)

@app.post("/api/import")
async def api_import(payload: dict, x_api_key: str = Header(None)):
    global machines
    require_api_key(x_api_key)
    # Expect payload with twin_state and machines
    ms = payload.get("machines")
    ts = payload.get("twin_state")
    if not isinstance(ms, list):
        raise HTTPException(status_code=400, detail="Invalid machines list")
    new_machines = []
    for m in ms:
        new_machines.append(Machine(m["id"], float(m.get("base_rate", 50)), int(m.get("position", 1))))
    machines.clear()
    machines.extend(new_machines)
    # load twin state fields if present
    if isinstance(ts, dict):
        twin_state["staffing_shifts"] = int(ts.get("staffing_shifts", twin_state["staffing_shifts"]))
        twin_state["ambient_temp"] = float(ts.get("ambient_temp", twin_state["ambient_temp"]))
        twin_state["ambient_humidity"] = float(ts.get("ambient_humidity", twin_state["ambient_humidity"]))
    return {"ok": True}

# -------------------
# Enhanced Presets endpoints with backwards compatibility
# -------------------
@app.post("/api/presets")
async def save_preset(payload: dict, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    
    try:
        name = validate_preset_name(payload.get("name", ""))
        description = payload.get("description", "").strip()[:200]  # Limit description length
        
        config = {
            "twin_state": twin_state.copy(),
            "machines": [m.to_dict() for m in machines],
        }
        
        created_at = datetime.utcnow().isoformat() + "Z"
        
        with db_lock:
            cur = db_conn.cursor()
            try:
                # Check if the new columns exist before using them
                if check_column_exists(db_conn, 'presets', 'description') and check_column_exists(db_conn, 'presets', 'created_at'):
                    cur.execute(
                        "INSERT INTO presets (name, description, config, created_at) VALUES (?, ?, ?, ?)", 
                        (name, description, json.dumps(config), created_at)
                    )
                else:
                    # Fallback for old schema
                    cur.execute(
                        "INSERT INTO presets (name, config) VALUES (?, ?)", 
                        (name, json.dumps(config))
                    )
                db_conn.commit()
                preset_id = cur.lastrowid
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=400, detail="A preset with this name already exists")
        
        return {"ok": True, "id": preset_id, "message": f"Preset '{name}' saved successfully"}
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to save preset")

@app.get("/api/presets")
async def list_presets():
    with db_lock:
        cur = db_conn.cursor()
        
        # Check if new columns exist and query accordingly
        if check_column_exists(db_conn, 'presets', 'description') and check_column_exists(db_conn, 'presets', 'created_at'):
            cur.execute("SELECT id, name, description, created_at FROM presets ORDER BY created_at DESC")
            rows = cur.fetchall()
            return [
                {
                    "id": r[0], 
                    "name": r[1], 
                    "description": r[2] or "", 
                    "created_at": r[3] or datetime.utcnow().isoformat() + "Z"
                } 
                for r in rows
            ]
        else:
            # Fallback for old schema
            cur.execute("SELECT id, name FROM presets ORDER BY id DESC")
            rows = cur.fetchall()
            return [
                {
                    "id": r[0], 
                    "name": r[1], 
                    "description": "", 
                    "created_at": datetime.utcnow().isoformat() + "Z"
                } 
                for r in rows
            ]

@app.post("/api/presets/{preset_id}/load")
async def load_preset(preset_id: int, x_api_key: str = Header(None)):
    global machines
    require_api_key(x_api_key)
    
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT name, config FROM presets WHERE id = ?", (preset_id,))
        row = cur.fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    
    preset_name, config_json = row
    
    try:
        cfg = json.loads(config_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupted preset data")
    
    # Validate configuration structure
    if not isinstance(cfg, dict) or "machines" not in cfg or "twin_state" not in cfg:
        raise HTTPException(status_code=500, detail="Invalid preset configuration")
    
    try:
        # Load machines with complete state restoration
        new_machines = []
        for machine_data in cfg.get("machines", []):
            machine = Machine(
                machine_data["id"], 
                float(machine_data.get("base_rate", 50)), 
                int(machine_data.get("position", 1))
            )
            # Restore complete machine state
            machine.from_dict(machine_data)
            new_machines.append(machine)
        
        # Replace machines
        machines.clear()
        machines.extend(new_machines)
        normalize_machine_positions()
        
        # Load complete twin state
        saved_twin_state = cfg.get("twin_state", {})
        twin_state.clear()
        twin_state.update(saved_twin_state)
        twin_state["time"] = datetime.utcnow().isoformat() + "Z"  # Update timestamp
        
        return {"ok": True, "message": f"Preset '{preset_name}' loaded successfully"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load preset: {str(e)}")

@app.post("/api/presets/{preset_id}/duplicate")
async def duplicate_preset(preset_id: int, payload: dict, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    
    # Get original preset
    with db_lock:
        cur = db_conn.cursor()
        if check_column_exists(db_conn, 'presets', 'description'):
            cur.execute("SELECT name, description, config FROM presets WHERE id = ?", (preset_id,))
            row = cur.fetchone()
            if row:
                original_name, original_description, config_json = row
            else:
                raise HTTPException(status_code=404, detail="Preset not found")
        else:
            cur.execute("SELECT name, config FROM presets WHERE id = ?", (preset_id,))
            row = cur.fetchone()
            if row:
                original_name, config_json = row
                original_description = ""
            else:
                raise HTTPException(status_code=404, detail="Preset not found")
    
    try:
        new_name = validate_preset_name(payload.get("name", f"{original_name} (Copy)"))
        new_description = payload.get("description", original_description or "").strip()[:200]
        
        created_at = datetime.utcnow().isoformat() + "Z"
        
        with db_lock:
            cur = db_conn.cursor()
            try:
                if check_column_exists(db_conn, 'presets', 'description') and check_column_exists(db_conn, 'presets', 'created_at'):
                    cur.execute(
                        "INSERT INTO presets (name, description, config, created_at) VALUES (?, ?, ?, ?)", 
                        (new_name, new_description, config_json, created_at)
                    )
                else:
                    cur.execute(
                        "INSERT INTO presets (name, config) VALUES (?, ?)", 
                        (new_name, config_json)
                    )
                db_conn.commit()
                new_preset_id = cur.lastrowid
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=400, detail="A preset with this name already exists")
        
        return {"ok": True, "id": new_preset_id, "message": f"Preset duplicated as '{new_name}'"}
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to duplicate preset")

@app.delete("/api/presets/{preset_id}")
async def delete_preset(preset_id: int, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT name FROM presets WHERE id = ?", (preset_id,))
        row = cur.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Preset not found")
        
        preset_name = row[0]
        cur.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        db_conn.commit()
    
    return {"ok": True, "message": f"Preset '{preset_name}' deleted successfully"}

@app.post("/api/reset")
async def reset_simulation_endpoint(x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    
    try:
        reset_simulation()
        return {"ok": True, "message": "Simulation reset to initial state"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset simulation: {str(e)}")

# -------------------
# Run uvicorn to start
# -------------------
# Start with: uvicorn backend:app --reload --host 0.0.0.0 --port 8000