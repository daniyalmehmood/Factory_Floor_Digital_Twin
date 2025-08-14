# backend.py
import asyncio
import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import threading
import time
import os
import re
import secrets

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Header, Depends, status, Query
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt
from passlib.context import CryptContext

import paho.mqtt.client as mqtt  # for optional external ingestion

# -------------------
# CONFIG
# -------------------
DB_PATH = "twin_history.db"
API_KEY = os.environ.get("FACTORY_API_KEY", "secret123")
MQTT_ENABLED = False
MQTT_BROKER = os.environ.get("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "/factory/digitaltwin/sensors")
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0

# JWT Configuration
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

app = FastAPI()

# Allow local testing from file:// or localhost dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files - this is important for serving your index.html
app.mount("/static", StaticFiles(directory="static"), name="static")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# HTTP Bearer for token authentication
security = HTTPBearer(auto_error=False)

# -----------------------
# PYDANTIC MODELS
# -----------------------
class MachineIn(BaseModel):
    name: str
    base_rate: float = 50.0
    position: int = 1

class PresetIn(BaseModel):
    name: str
    description: str = ""

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "viewer"  # Default to viewer role

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user: dict

# -------------------
# AUTHENTICATION HELPERS
# -------------------
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def create_access_token(data: dict) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

def get_user_by_username(username: str) -> Optional[dict]:
    """Get user by username from database"""
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT id, username, password_hash, role, created_at, last_login FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        if row:
            return {
                "id": row[0],
                "username": row[1],
                "password_hash": row[2],
                "role": row[3],
                "created_at": row[4],
                "last_login": row[5]
            }
    return None

def get_user_by_id(user_id: int) -> Optional[dict]:
    """Get user by ID from database"""
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT id, username, password_hash, role, created_at, last_login FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            return {
                "id": row[0],
                "username": row[1],
                "password_hash": row[2],
                "role": row[3],
                "created_at": row[4],
                "last_login": row[5]
            }
    return None

def update_last_login(user_id: int):
    """Update user's last login timestamp"""
    with db_lock:
        cur = db_conn.cursor()
        now = datetime.utcnow().isoformat() + "Z"
        cur.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
        db_conn.commit()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Get current authenticated user"""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    token = credentials.credentials
    payload = verify_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user = get_user_by_id(int(user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return user

def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin role"""
    if current_user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

# -------------------
# DATABASE (SQLite)
# -------------------
def get_db_version(conn):
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
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns

def migrate_database(conn):
    current_version = get_db_version(conn)
    cursor = conn.cursor()
    
    if current_version < 1:
        print("Migrating database to version 1...")
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='presets'")
            if cursor.fetchone():
                if not check_column_exists(conn, 'presets', 'description'):
                    cursor.execute("ALTER TABLE presets ADD COLUMN description TEXT")
                    print("Added 'description' column to presets table")
                
                if not check_column_exists(conn, 'presets', 'created_at'):
                    cursor.execute("ALTER TABLE presets ADD COLUMN created_at TEXT")
                    print("Added 'created_at' column to presets table")
                    
                    default_date = datetime.utcnow().isoformat() + "Z"
                    cursor.execute("UPDATE presets SET created_at = ? WHERE created_at IS NULL", (default_date,))
                    print("Set default created_at for existing presets")
            
            conn.commit()
            set_db_version(conn, 1)
            print("Database migration to version 1 completed")
        except Exception as e:
            print(f"Migration to version 1 failed: {e}")
            conn.rollback()

    # Migration to version 2: Add users table
    if current_version < 2:
        print("Migrating database to version 2...")
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL,
                last_login TEXT
            );
            """)
            
            # Create default admin user if no users exist
            cursor.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
            
            if user_count == 0:
                admin_password_hash = get_password_hash("admin123")  # Default admin password
                created_at = datetime.utcnow().isoformat() + "Z"
                cursor.execute("""
                INSERT INTO users (username, password_hash, role, created_at) 
                VALUES (?, ?, ?, ?)
                """, ("admin", admin_password_hash, "admin", created_at))
                print("Created default admin user (username: admin, password: admin123)")
            
            conn.commit()
            set_db_version(conn, 2)
            print("Database migration to version 2 completed")
        except Exception as e:
            print(f"Migration to version 2 failed: {e}")
            conn.rollback()

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    """)
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        config TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    
    # Create users table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        created_at TEXT NOT NULL,
        last_login TEXT
    );
    """)
    
    conn.commit()
    migrate_database(conn)
    return conn

db_conn = init_db()
db_lock = threading.Lock()

def save_metrics_snapshot(snapshot: dict):
    try:
        with db_lock:
            cur = db_conn.cursor()
            cur.execute("INSERT INTO metrics (ts, payload) VALUES (?, ?)", (snapshot["time"], json.dumps(snapshot)))
            db_conn.commit()
    except Exception as e:
        print("DB save error:", e)

def query_history(minutes: int = 60):
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

# -----------------------
# DIGITAL TWIN MODEL
# -----------------------
class Machine:
    def __init__(self, id: str, name: str, base_rate: float, position: int):
        self.id = id
        self.name = name
        self.base_rate = base_rate
        self.throughput_factor = 1.0
        self.uptime = 1.0
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

    def from_dict(self, data: dict):
        self.name = data.get("name", self.name)
        self.base_rate = float(data.get("base_rate", self.base_rate))
        self.throughput_factor = float(data.get("throughput_factor", 1.0))
        self.uptime = float(data.get("uptime", 1.0))
        self.queue = float(data.get("queue", 0.0))
        self.position = int(data.get("position", self.position))
        self.status = data.get("status", "on")
        self.total_processed = float(data.get("total_processed", 0.0))
        self.last_change = data.get("last_change", datetime.utcnow().isoformat() + "Z")

# Initial default machines
DEFAULT_MACHINES = [
    {"id": "M1_cutter", "name": "Cutting Station", "base_rate": 60, "position": 1},
    {"id": "M2_press", "name": "Press Station", "base_rate": 50, "position": 2},
    {"id": "M3_paint", "name": "Paint Station", "base_rate": 40, "position": 3},
    {"id": "M4_inspect", "name": "Inspection Station", "base_rate": 70, "position": 4},
]

machines: Dict[str, Machine] = {}
machines_lock = asyncio.Lock()

def initialize_default_machines():
    for m in DEFAULT_MACHINES:
        machines[m["id"]] = Machine(m["id"], m["name"], m["base_rate"], m["position"])

initialize_default_machines()

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
    if not name or not name.strip():
        raise ValueError("Preset name cannot be empty")
    
    name = name.strip()
    if len(name) > 50:
        raise ValueError("Preset name cannot exceed 50 characters")
    
    if not re.match(r'^[a-zA-Z0-9\s\-_\.]+$', name):
        raise ValueError("Preset name can only contain letters, numbers, spaces, hyphens, underscores, and periods")
    
    return name

def reset_simulation():
    global twin_state
    machines.clear()
    initialize_default_machines()
    twin_state.clear()
    twin_state.update(INITIAL_TWIN_STATE.copy())
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

def normalize_machine_positions():
    """Ensure machine positions are sequential starting from 1"""
    sorted_machines = sorted(machines.values(), key=lambda x: x.position)
    for idx, machine in enumerate(sorted_machines, start=1):
        machine.position = idx

def compute_staffing_modifier(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)

def machines_snapshot():
    return [m.to_dict() for m in sorted(machines.values(), key=lambda x: x.position)]

def process_production_tick(delta_seconds: float):
    staffing_mod = compute_staffing_modifier(twin_state["staffing_shifts"])

    twin_state["ambient_temp"] += random.uniform(-0.05, 0.05)
    twin_state["ambient_humidity"] += random.uniform(-0.1, 0.1)
    twin_state["ambient_temp"] = round(max(15, min(40, twin_state["ambient_temp"])), 2)
    twin_state["ambient_humidity"] = round(max(20, min(80, twin_state["ambient_humidity"])), 2)

    machines_sorted = sorted(machines.values(), key=lambda m: m.position)
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
# WebSocket manager
# -------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[Dict[str, any]] = []

    async def connect(self, websocket: WebSocket, user: Optional[dict] = None):
        await websocket.accept()
        connection_info = {
            "websocket": websocket,
            "user": user,
            "connected_at": datetime.utcnow()
        }
        self.active.append(connection_info)

    def disconnect(self, websocket: WebSocket):
        self.active = [conn for conn in self.active if conn["websocket"] != websocket]

    async def broadcast(self, message: Dict):
        data = json.dumps(message) if isinstance(message, dict) else message
        living = []
        for conn_info in list(self.active):
            try:
                if isinstance(data, str):
                    await conn_info["websocket"].send_text(data)
                else:
                    await conn_info["websocket"].send_json(data)
                living.append(conn_info)
            except Exception:
                pass
        self.active = living

manager = ConnectionManager()

# -------------------
# AUTHENTICATION ENDPOINTS
# -------------------
@app.post("/api/register")
async def register(user_data: UserCreate):
    """Register a new user"""
    # Validate username
    if not user_data.username or len(user_data.username.strip()) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters long")
    
    if not re.match(r'^[a-zA-Z0-9_-]+$', user_data.username):
        raise HTTPException(status_code=400, detail="Username can only contain letters, numbers, hyphens, and underscores")
    
    # Validate password
    if len(user_data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")
    
    # Validate role
    if user_data.role not in ["admin", "viewer"]:
        raise HTTPException(status_code=400, detail="Role must be either 'admin' or 'viewer'")
    
    # Check if user already exists
    existing_user = get_user_by_username(user_data.username)
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    # Hash password and create user
    try:
        password_hash = get_password_hash(user_data.password)
        created_at = datetime.utcnow().isoformat() + "Z"
        
        with db_lock:
            cur = db_conn.cursor()
            cur.execute("""
            INSERT INTO users (username, password_hash, role, created_at) 
            VALUES (?, ?, ?, ?)
            """, (user_data.username, password_hash, user_data.role, created_at))
            db_conn.commit()
            user_id = cur.lastrowid
        
        return {"message": "User registered successfully", "user_id": user_id}
    except Exception as e:
        print(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Failed to register user")

@app.post("/api/token")
async def login(user_credentials: UserLogin):
    """Authenticate user and return JWT token"""
    user = get_user_by_username(user_credentials.username)
    
    if not user or not verify_password(user_credentials.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Update last login
    update_last_login(user["id"])
    
    # Create access token
    access_token = create_access_token(data={"sub": str(user["id"])})
    
    # Return token and user info
    user_info = {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"]
    }
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user_info
    }

@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current user information"""
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "role": current_user["role"],
        "created_at": current_user["created_at"],
        "last_login": current_user["last_login"]
    }

# -----------------------
# REST API ENDPOINTS (now with authentication)
# -----------------------
@app.get('/api/machines')
async def list_machines(current_user: dict = Depends(get_current_user)):
    return machines_snapshot()

@app.post('/api/machines', status_code=201)
async def create_machine(payload: MachineIn, current_user: dict = Depends(require_admin)):
    async with machines_lock:
        mid = str(uuid.uuid4())
        m = Machine(mid, payload.name, payload.base_rate, payload.position)
        machines[mid] = m
    
    normalize_machine_positions()
    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot()})
    return m.to_dict()

@app.put('/api/machines/{machine_id}')
async def update_machine(machine_id: str, payload: MachineIn, current_user: dict = Depends(require_admin)):
    async with machines_lock:
        if machine_id not in machines:
            raise HTTPException(status_code=404, detail='Machine not found')
        
        m = machines[machine_id]
        m.name = payload.name
        m.base_rate = payload.base_rate
        m.position = payload.position
        m.last_change = datetime.utcnow().isoformat() + "Z"
    
    normalize_machine_positions()
    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot()})
    return m.to_dict()

@app.delete('/api/machines/{machine_id}')
async def delete_machine(machine_id: str, current_user: dict = Depends(require_admin)):
    async with machines_lock:
        if machine_id not in machines:
            raise HTTPException(status_code=404, detail='Machine not found')

        m = machines.pop(machine_id)
        m.queue = 0.0

    normalize_machine_positions()
    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot()})
    return {'status': 'deleted', 'id': machine_id}

# -----------------------
# PRESET ENDPOINTS (now with authentication)
# -----------------------
@app.get('/api/presets')
async def list_presets(current_user: dict = Depends(get_current_user)):
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT id, name, description, created_at FROM presets ORDER BY created_at DESC")
        rows = cur.fetchall()
    
    presets = []
    for row in rows:
        presets.append({
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "created_at": row[3]
        })
    return presets

@app.post('/api/presets', status_code=201)
async def create_preset(payload: PresetIn, current_user: dict = Depends(require_admin)):
    try:
        name = validate_preset_name(payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    config = {
        "twin_state": twin_state,
        "machines": machines_snapshot()
    }
    
    try:
        with db_lock:
            cur = db_conn.cursor()
            cur.execute(
                "INSERT INTO presets (name, description, config, created_at) VALUES (?, ?, ?, ?)",
                (name, payload.description, json.dumps(config), datetime.utcnow().isoformat() + "Z")
            )
            db_conn.commit()
            preset_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Preset name already exists")
    
    return {
        "id": preset_id,
        "name": name,
        "description": payload.description,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

@app.post('/api/presets/{preset_id}/load')
async def load_preset(preset_id: int, current_user: dict = Depends(require_admin)):
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT name, config FROM presets WHERE id = ?", (preset_id,))
        row = cur.fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    
    try:
        config = json.loads(row[1])
        
        # Load machines
        if "machines" in config:
            machines.clear()
            for m_data in config["machines"]:
                machine = Machine(
                    m_data["id"],
                    m_data.get("name", m_data["id"]),
                    float(m_data.get("base_rate", 50)),
                    int(m_data.get("position", 1))
                )
                machine.from_dict(m_data)
                machines[machine.id] = machine
            
            normalize_machine_positions()
        
        # Load twin state
        if "twin_state" in config:
            ts = config["twin_state"]
            twin_state["staffing_shifts"] = int(ts.get("staffing_shifts", 1))
            twin_state["ambient_temp"] = float(ts.get("ambient_temp", 25.0))
            twin_state["ambient_humidity"] = float(ts.get("ambient_humidity", 45.0))
        
        # Broadcast update
        current_payload = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "machines": machines_snapshot(),
            "bottlenecks": [],
            "staffing_shifts": twin_state["staffing_shifts"],
        }
        await manager.broadcast({"type": "metrics", "payload": current_payload})
        
        return {"message": f"Loaded preset: {row[0]}"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading preset: {str(e)}")

@app.delete('/api/presets/{preset_id}')
async def delete_preset(preset_id: int, current_user: dict = Depends(require_admin)):
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Preset not found")
        db_conn.commit()
    
    return {"message": "Preset deleted"}

# -----------------------
# BACKGROUND TASK
# -----------------------
async def simulator_loop():
    while True:
        try:
            if machines:
                process_production_tick(TICK_SECONDS)
                
                machines_snapshot_data = machines_snapshot()
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
                
                await manager.broadcast({"type": "metrics", "payload": metrics})
                save_metrics_snapshot(metrics)
        except Exception as e:
            print(f"Simulator loop error: {e}")
        
        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(simulator_loop())

# -----------------------
# WEBSOCKET ENDPOINT - Enhanced with authentication
# -----------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(None)):
    # Try to authenticate user if token is provided
    user = None
    if token:
        payload = verify_token(token)
        if payload:
            user_id = payload.get("sub")
            if user_id:
                user = get_user_by_id(int(user_id))
    
    await manager.connect(websocket, user)
    try:
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
        
        # Send authentication status
        if user:
            await websocket.send_text(json.dumps({
                "type": "auth_status", 
                "payload": {
                    "authenticated": True,
                    "user": {
                        "id": user["id"],
                        "username": user["username"],
                        "role": user["role"]
                    }
                }
            }))
        else:
            await websocket.send_text(json.dumps({
                "type": "auth_status", 
                "payload": {"authenticated": False}
            }))
        
        while True:
            msg = await websocket.receive_text()
            try:
                obj = json.loads(msg)
            except:
                await websocket.send_text(json.dumps({"type":"error","payload":"invalid json"}))
                continue

            action = obj.get("action")
            
            # Unprotected actions
            if action == "get_snapshot":
                snapshot = {
                    "time": twin_state["time"],
                    "ambient_temp": twin_state["ambient_temp"],
                    "ambient_humidity": twin_state["ambient_humidity"],
                    "total_output": twin_state["total_output"],
                    "machines": machines_snapshot(),
                    "bottlenecks": [m_id for m_id, m in machines.items() if m.queue > 5],
                    "staffing_shifts": twin_state["staffing_shifts"],
                }
                await websocket.send_text(json.dumps({"type":"metrics","payload": snapshot}))
                continue
            elif action == "ping":
                await websocket.send_json({'type': 'pong'})
                continue

            # All other commands require authentication and admin role
            if not user:
                await websocket.send_text(json.dumps({"type":"error","payload":"authentication required"}))
                continue
            
            if user["role"] != "admin":
                await websocket.send_text(json.dumps({"type":"error","payload":"admin access required"}))
                continue

            # Handle admin commands
            if action == "add_shift":
                twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)
            elif action == "remove_shift":
                twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)
            elif action == "move_equipment":
                mid = obj.get("machine_id")
                new_pos = obj.get("new_position")
                if mid in machines:
                    machines[mid].position = int(new_pos)
                    machines[mid].last_change = datetime.utcnow().isoformat() + "Z"
                    normalize_machine_positions()
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
                    normalize_machine_positions()
            elif action == "reset_simulation":
                reset_simulation()
            else:
                await websocket.send_text(json.dumps({"type":"error","payload":"unknown action"}))
                continue

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

# -------------------
# HTTP ENDPOINTS (now with authentication where needed)
# -------------------
@app.get("/")
async def index():
    """Serve the main dashboard"""
    return FileResponse("index.html")

@app.get("/api/history")
async def api_history(minutes: int = 60, current_user: dict = Depends(get_current_user)):
    rows = query_history(minutes)
    return {"minutes": minutes, "count": len(rows), "snapshots": rows}

@app.get("/api/latest")
async def api_latest(current_user: dict = Depends(get_current_user)):
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT payload FROM metrics ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
    if not r:
        return {}
    return json.loads(r[0])

@app.post("/api/export")
async def api_export(current_user: dict = Depends(require_admin)):
    data = {
        "twin_state": twin_state,
        "machines": machines_snapshot(),
    }
    return JSONResponse(content=data)

@app.post("/api/import")
async def api_import(payload: dict, current_user: dict = Depends(require_admin)):
    ms = payload.get("machines")
    ts = payload.get("twin_state")
    if not isinstance(ms, list):
        raise HTTPException(status_code=400, detail="Invalid machines list")
    
    machines.clear()
    for m_data in ms:
        machine = Machine(
            m_data["id"], 
            m_data.get("name", m_data["id"]), 
            float(m_data.get("base_rate", 50)), 
            int(m_data.get("position", 1))
        )
        machine.from_dict(m_data)
        machines[machine.id] = machine
    
    normalize_machine_positions()
    
    if isinstance(ts, dict):
        twin_state["staffing_shifts"] = int(ts.get("staffing_shifts", twin_state["staffing_shifts"]))
        twin_state["ambient_temp"] = float(ts.get("ambient_temp", twin_state["ambient_temp"]))
        twin_state["ambient_humidity"] = float(ts.get("ambient_humidity", twin_state["ambient_humidity"]))
    return {"ok": True}

@app.post("/api/reset")
async def reset_simulation_endpoint(current_user: dict = Depends(require_admin)):
    try:
        reset_simulation()
        return {"ok": True, "message": "Simulation reset to initial state"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset simulation: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)