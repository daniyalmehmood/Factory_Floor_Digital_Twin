# backend.py
import asyncio
import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import threading
import os
import re
import secrets

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt
from passlib.context import CryptContext

# -------------------
# CONFIG
# -------------------
DB_PATH = "twin_history.db"
TICK_SECONDS = 2.0
SECONDS_PER_MIN = 60.0

# JWT Configuration
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Routing strategies
ALLOWED_ROUTING = {"shortest_queue", "round_robin", "random"}

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
    # When provided, interpreted as the stage index to add/move this machine to
    position: Optional[int] = None

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

class StageRoutingIn(BaseModel):
    routing_strategy: str

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
# DIGITAL TWIN MODEL (Staged with parallel machines)
# -----------------------
class Machine:
    def __init__(self, id: str, name: str, base_rate: float, stage_id: int):
        self.id = id
        self.name = name
        self.base_rate = base_rate
        self.throughput_factor = 1.0
        self.uptime = 1.0
        self.queue = 0.0
        self.stage_id = stage_id
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
        ideal_per_tick = self.base_rate * (delta_seconds / SECONDS_PER_MIN)
        return max(0.0, ideal_per_tick * self.throughput_factor * staffing_mod * self.uptime)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "base_rate": self.base_rate,
            "throughput_factor": round(self.throughput_factor, 3),
            "uptime": round(self.uptime, 3),
            "queue": round(self.queue, 2),
            "stage_id": self.stage_id,
            "position": self.stage_id,  # backward compatibility
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
        self.stage_id = int(data.get("stage_id", data.get("position", self.stage_id)))
        self.status = data.get("status", "on")
        self.total_processed = float(data.get("total_processed", 0.0))
        self.last_change = data.get("last_change", datetime.now(timezone.utc).isoformat())

class Stage:
    def __init__(self, id: int, machines: Optional[List[Machine]] = None, routing_strategy: str = "shortest_queue"):
        self.id = id
        self.machines: List[Machine] = machines or []
        self.input_queue = 0.0
        self.routing_strategy = routing_strategy if routing_strategy in ALLOWED_ROUTING else "shortest_queue"
        self._rr_index = 0  # round-robin pointer

    def to_dict(self):
        return {
            "id": self.id,
            "input_queue": round(self.input_queue, 2),
            "routing_strategy": self.routing_strategy,
            "machines": [m.to_dict() for m in self.machines],
        }

# Initial default stages and machines
def build_default_stages() -> List[Stage]:
    return [
        Stage(1, [Machine("M1_cutter", "Cutting Station", 60, 1)], "shortest_queue"),
        Stage(2, [Machine("M2_press", "Press Station", 50, 2)], "shortest_queue"),
        Stage(3, [Machine("M3_paint", "Paint Station", 40, 3)], "shortest_queue"),
        Stage(4, [Machine("M4_inspect", "Inspection Station", 70, 4)], "shortest_queue"),
    ]

stages: List[Stage] = build_default_stages()

# Keep an index for quick lookup (id -> Machine), refreshed from stages
machines: Dict[str, Machine] = {}
stages_lock = asyncio.Lock()

def refresh_machines_index():
    machines.clear()
    for st in stages:
        for m in st.machines:
            machines[m.id] = m

refresh_machines_index()

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
# Helper functions (staged flow)
# -------------------
def reset_simulation():
    global twin_state, stages
    stages = build_default_stages()
    refresh_machines_index()
    twin_state.clear()
    twin_state.update(INITIAL_TWIN_STATE.copy())
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

def staffing_modifier(shifts: int) -> float:
    return 1.0 + 0.25 * (shifts - 1)

def _route_chunks(stage: Stage, amount: float, pick_machine_func):
    if amount <= 0 or not stage.machines:
        return
    # Break amount into chunks to smooth distribution
    chunk = max(0.1, amount / 10.0)
    remaining = amount
    while remaining > 1e-6:
        m = pick_machine_func()
        push = min(chunk, remaining)
        m.queue += push
        remaining -= push

def route_to_stage(stage: Stage, amount: float):
    if not stage.machines or amount <= 0:
        return

    strategy = stage.routing_strategy
    if strategy == "shortest_queue":
        def pick():
            return min(stage.machines, key=lambda x: x.queue)
        _route_chunks(stage, amount, pick)
    elif strategy == "round_robin":
        n = len(stage.machines)
        if n == 0:
            return
        def pick():
            m = stage.machines[stage._rr_index % n]
            stage._rr_index = (stage._rr_index + 1) % n
            return m
        _route_chunks(stage, amount, pick)
    elif strategy == "random":
        def pick():
            return random.choice(stage.machines)
        _route_chunks(stage, amount, pick)
    else:
        # Fallback
        def pick():
            return min(stage.machines, key=lambda x: x.queue)
        _route_chunks(stage, amount, pick)

def process_production_tick(delta_seconds: float):
    smod = staffing_modifier(twin_state["staffing_shifts"])

    # Environment
    twin_state["ambient_temp"] = round(
        min(40, max(15, twin_state["ambient_temp"] + random.uniform(-0.05, 0.05))), 2
    )
    twin_state["ambient_humidity"] = round(
        min(80, max(20, twin_state["ambient_humidity"] + random.uniform(-0.1, 0.1))), 2
    )

    # Material arrival to stage 1 proportional to total base_rate of parallel machines
    if stages:
        s0 = stages[0]
        ideal = sum(m.base_rate for m in s0.machines) * (delta_seconds / SECONDS_PER_MIN)
        arrivals = max(0.0, random.gauss(ideal * smod, ideal * 0.2))
        s0.input_queue += arrivals

    # Distribute input queues based on stage routing strategy
    finished_passthrough = 0.0
    for idx, st in enumerate(stages):
        if st.input_queue > 0:
            amt = st.input_queue
            st.input_queue = 0.0
            if st.machines:
                route_to_stage(st, amt)
            else:
                if idx < len(stages) - 1:
                    stages[idx + 1].input_queue += amt
                else:
                    finished_passthrough += amt

    # Process all stages
    finished_processed = 0.0
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
            finished_processed += processed_sum

    twin_state["total_output"] += int(finished_processed + finished_passthrough)
    twin_state["time"] = datetime.utcnow().isoformat() + "Z"

def stages_snapshot():
    return [s.to_dict() for s in stages]

def machines_snapshot():
    # Flatten with stage_id included
    flat = []
    for s in stages:
        for m in s.machines:
            flat.append(m.to_dict())
    return flat

def compute_bottlenecks():
    bottlenecks = []
    for st in stages:
        if st.input_queue > 5:
            bottlenecks.append(f"Stage{st.id}:input")
        for m in st.machines:
            if m.queue > max(2.0, 0.5 * (m.base_rate/SECONDS_PER_MIN) * TICK_SECONDS):
                bottlenecks.append(m.id)
    return bottlenecks

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
# REST API ENDPOINTS (auth)
# -----------------------
@app.get('/api/machines')
async def list_machines(current_user: dict = Depends(get_current_user)):
    return machines_snapshot()

@app.get('/api/stages')
async def list_stages(current_user: dict = Depends(get_current_user)):
    return {"stages": stages_snapshot()}

@app.put('/api/stages/{stage_id}/routing')
async def set_stage_routing(stage_id: int, payload: StageRoutingIn, current_user: dict = Depends(require_admin)):
    strategy = payload.routing_strategy
    if strategy not in ALLOWED_ROUTING:
        raise HTTPException(status_code=400, detail=f"Invalid routing strategy. Allowed: {sorted(list(ALLOWED_ROUTING))}")
    async with stages_lock:
        st = next((s for s in stages if s.id == stage_id), None)
        if not st:
            raise HTTPException(status_code=404, detail='Stage not found')
        st.routing_strategy = strategy
    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
    return {"ok": True, "stage_id": stage_id, "routing_strategy": strategy}

@app.post('/api/machines', status_code=201)
async def create_machine(payload: MachineIn, current_user: dict = Depends(require_admin)):
    """
    If payload.position (stage index) is provided, add a parallel machine to that stage.
    Otherwise, create a new stage with this machine at the next available stage id.
    """
    mid = str(uuid.uuid4())
    name = payload.name
    base_rate = payload.base_rate

    async with stages_lock:
        if payload.position:
            # Add to existing stage as parallel machine
            stage = next((s for s in stages if s.id == int(payload.position)), None)
            if not stage:
                raise HTTPException(status_code=404, detail='Stage not found')
            if any(m.id == mid for m in stage.machines):
                raise HTTPException(status_code=400, detail='Duplicate machine ID')
            m = Machine(mid, name, base_rate, stage.id)
            stage.machines.append(m)
        else:
            # New stage with this machine
            new_stage_id = (stages[-1].id + 1) if stages else 1
            m = Machine(mid, name, base_rate, new_stage_id)
            stages.append(Stage(new_stage_id, [m]))

        refresh_machines_index()

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
    return m.to_dict()

@app.post('/api/stages/{stage_index}/machines', status_code=201)
async def add_parallel_machine(stage_index: int, payload: MachineIn, current_user: dict = Depends(require_admin)):
    """
    Add a new machine in parallel to the specified stage.
    """
    mid = str(uuid.uuid4())
    name = payload.name
    base_rate = payload.base_rate

    async with stages_lock:
        stage = next((s for s in stages if s.id == int(stage_index)), None)
        if not stage:
            raise HTTPException(status_code=404, detail='Stage not found')
        if any(m.id == mid for m in stage.machines):
            raise HTTPException(status_code=400, detail='Duplicate machine ID')
        m = Machine(mid, name, base_rate, stage.id)
        stage.machines.append(m)
        refresh_machines_index()

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
    return m.to_dict()

@app.put('/api/machines/{machine_id}')
async def update_machine(machine_id: str, payload: MachineIn, current_user: dict = Depends(require_admin)):
    async with stages_lock:
        # Find machine and stage
        src_stage = next((s for s in stages if any(m.id == machine_id for m in s.machines)), None)
        if not src_stage:
            raise HTTPException(status_code=404, detail='Machine not found')
        m = next(m for m in src_stage.machines if m.id == machine_id)

        # Update basics
        m.name = payload.name
        m.base_rate = payload.base_rate
        m.last_change = datetime.utcnow().isoformat() + "Z"

        # Move to a different stage if position provided
        if payload.position and int(payload.position) != src_stage.id:
            # remove from old stage
            src_stage.machines = [mm for mm in src_stage.machines if mm.id != machine_id]
            # add to target stage (create stage if missing)
            tgt_stage = next((s for s in stages if s.id == int(payload.position)), None)
            if not tgt_stage:
                tgt_stage = Stage(int(payload.position), [])
                stages.append(tgt_stage)
                stages.sort(key=lambda s: s.id)
            m.stage_id = tgt_stage.id
            tgt_stage.machines.append(m)

        refresh_machines_index()

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
    return m.to_dict()

@app.delete('/api/machines/{machine_id}')
async def delete_machine(machine_id: str, current_user: dict = Depends(require_admin)):
    async with stages_lock:
        found = False
        for st in stages:
            for i, m in enumerate(list(st.machines)):
                if m.id == machine_id:
                    st.input_queue += m.queue  # return WIP to stage input buffer
                    del st.machines[i]
                    found = True
                    break
            if found:
                break
        if not found:
            raise HTTPException(status_code=404, detail='Machine not found')

        refresh_machines_index()

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
    return {'status': 'deleted', 'id': machine_id}

@app.delete('/api/machines/by-name/{machine_name}')
async def delete_machine_by_name(machine_name: str, stage_id: Optional[int] = None, delete_all: bool = False, current_user: dict = Depends(require_admin)):
    """
    Delete machine(s) by name. If stage_id is provided, restrict search to that stage.
    By default deletes the first match; if delete_all=True, deletes all matches.
    """
    async with stages_lock:
        matches = []
        for st in stages:
            if stage_id is not None and st.id != stage_id:
                continue
            for i, m in enumerate(st.machines):
                if m.name == machine_name:
                    matches.append((st, i, m))
        if not matches:
            raise HTTPException(status_code=404, detail='Machine with given name not found')

        deleted_ids = []
        if delete_all:
            # delete from last index to first to keep indices valid
            for st, i, m in sorted(matches, key=lambda x: x[1], reverse=True):
                st.input_queue += m.queue
                del st.machines[i]
                deleted_ids.append(m.id)
        else:
            st, i, m = matches[0]
            st.input_queue += m.queue
            del st.machines[i]
            deleted_ids.append(m.id)

        refresh_machines_index()

    await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
    return {'status': 'deleted', 'deleted_ids': deleted_ids, 'name': machine_name, 'count': len(deleted_ids)}

# -----------------------
# PRESET ENDPOINTS (auth)
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
    def validate_preset_name(name: str) -> str:
        if not name or not name.strip():
            raise ValueError("Preset name cannot be empty")
        name = name.strip()
        if len(name) > 50:
            raise ValueError("Preset name cannot exceed 50 characters")
        if not re.match(r'^[a-zA-Z0-9\s\-_\.]+$', name):
            raise ValueError("Preset name can only contain letters, numbers, spaces, hyphens, underscores, and periods")
        return name

    try:
        name = validate_preset_name(payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    config = {
        "twin_state": twin_state,
        "stages": stages_snapshot(),      # persist stages with strategies
        "machines": machines_snapshot()   # keep for compatibility
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

        # Load stages if present, else reconstruct from machines list by position
        loaded_stages = []
        if "stages" in config and isinstance(config["stages"], list) and config["stages"]:
            for s in config["stages"]:
                sid = int(s.get("id"))
                mlist = []
                for m_data in s.get("machines", []):
                    machine = Machine(
                        m_data.get("id"),
                        m_data.get("name", m_data.get("id")),
                        float(m_data.get("base_rate", 50)),
                        int(m_data.get("stage_id", m_data.get("position", sid)))
                    )
                    machine.from_dict(m_data)
                    mlist.append(machine)
                routing_strategy = s.get("routing_strategy", "shortest_queue")
                st_obj = Stage(sid, mlist, routing_strategy=routing_strategy)
                st_obj.input_queue = float(s.get("input_queue", 0.0))
                loaded_stages.append(st_obj)
        elif "machines" in config and isinstance(config["machines"], list):
            # Fallback: group by position as stage_id
            by_stage: Dict[int, List[Machine]] = {}
            for m_data in config["machines"]:
                sid = int(m_data.get("stage_id", m_data.get("position", 1)))
                mach = Machine(
                    m_data.get("id"),
                    m_data.get("name", m_data.get("id")),
                    float(m_data.get("base_rate", 50)),
                    sid
                )
                mach.from_dict(m_data)
                by_stage.setdefault(sid, []).append(mach)
            for sid in sorted(by_stage.keys()):
                loaded_stages.append(Stage(sid, by_stage[sid]))

        if loaded_stages:
            # replace stages
            global stages
            stages = sorted(loaded_stages, key=lambda s: s.id)
            refresh_machines_index()

        # Load twin state
        if "twin_state" in config:
            ts = config["twin_state"]
            twin_state["staffing_shifts"] = int(ts.get("staffing_shifts", 1))
            twin_state["ambient_temp"] = float(ts.get("ambient_temp", 25.0))
            twin_state["ambient_humidity"] = float(ts.get("ambient_humidity", 45.0))

        current_payload = {
            "time": twin_state["time"],
            "ambient_temp": twin_state["ambient_temp"],
            "ambient_humidity": twin_state["ambient_humidity"],
            "total_output": twin_state["total_output"],
            "stages": stages_snapshot(),
            "machines": machines_snapshot(),
            "bottlenecks": compute_bottlenecks(),
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
            process_production_tick(TICK_SECONDS)
            metrics = {
                "time": twin_state["time"],
                "ambient_temp": twin_state["ambient_temp"],
                "ambient_humidity": twin_state["ambient_humidity"],
                "total_output": twin_state["total_output"],
                "stages": stages_snapshot(),
                "machines": machines_snapshot(),
                "bottlenecks": compute_bottlenecks(),
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
            "stages": stages_snapshot(),
            "machines": machines_snapshot(),
            "bottlenecks": compute_bottlenecks(),
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
                    "stages": stages_snapshot(),
                    "machines": machines_snapshot(),
                    "bottlenecks": compute_bottlenecks(),
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
            updated = False
            if action == "add_shift":
                twin_state["staffing_shifts"] = min(3, twin_state["staffing_shifts"] + 1)
                updated = True
            elif action == "remove_shift":
                twin_state["staffing_shifts"] = max(1, twin_state["staffing_shifts"] - 1)
                updated = True
            elif action == "move_equipment":
                mid = obj.get("machine_id")
                new_stage = obj.get("new_position")
                if mid in machines and new_stage:
                    new_stage = int(new_stage)
                    # Find current stage
                    src_stage = next((s for s in stages if any(m.id == mid for m in s.machines)), None)
                    if src_stage:
                        m = next(m for m in src_stage.machines if m.id == mid)
                        # remove from old
                        src_stage.machines = [mm for mm in src_stage.machines if mm.id != mid]
                        # add to target stage (create if missing)
                        tgt_stage = next((s for s in stages if s.id == new_stage), None)
                        if not tgt_stage:
                            tgt_stage = Stage(new_stage, [])
                            stages.append(tgt_stage)
                            stages.sort(key=lambda s: s.id)
                        m.stage_id = tgt_stage.id
                        m.last_change = datetime.utcnow().isoformat() + "Z"
                        tgt_stage.machines.append(m)
                        refresh_machines_index()
                        updated = True
            elif action == "toggle_machine":
                mid = obj.get("machine_id")
                if mid in machines:
                    m = machines[mid]
                    m.status = "off" if m.status == "on" else "on"
                    m.last_change = datetime.utcnow().isoformat() + "Z"
                    updated = True
            elif action == "set_throughput":
                mid = obj.get("machine_id")
                val = float(obj.get("value", 1.0))
                if mid in machines:
                    machines[mid].throughput_factor = max(0.2, min(2.0, val))
                    machines[mid].last_change = datetime.utcnow().isoformat() + "Z"
                    updated = True
            elif action == "add_machine":
                # Create a new stage with a single machine (or, if stage_index exists and as_parallel==True, add parallel)
                new_id = obj.get("machine_id", f"M_new_{len(machines)+1}")
                name = obj.get("name", new_id)
                base_rate = float(obj.get("base_rate", 50))
                desired_stage = obj.get("stage_index")  # optional
                as_parallel = bool(obj.get("as_parallel", False))
                async with stages_lock:
                    if desired_stage:
                        desired_stage = int(desired_stage)
                        tgt_stage = next((s for s in stages if s.id == desired_stage), None)
                        if tgt_stage:
                            if as_parallel:
                                tgt_stage.machines.append(Machine(new_id, name, base_rate, desired_stage))
                            else:
                                # position already taken -> notify client
                                await websocket.send_text(json.dumps({"type":"error","payload":"position_taken: Stage already exists. Use as_parallel to add parallel machine."}))
                                continue
                        else:
                            m = Machine(new_id, name, base_rate, desired_stage)
                            stages.append(Stage(desired_stage, [m]))
                            stages.sort(key=lambda s: s.id)
                    else:
                        # Append at next stage id
                        new_stage_id = (stages[-1].id + 1) if stages else 1
                        m = Machine(new_id, name, base_rate, new_stage_id)
                        stages.append(Stage(new_stage_id, [m]))
                        stages.sort(key=lambda s: s.id)
                    refresh_machines_index()
                    updated = True
            elif action == "add_parallel_machine":
                stage_index = int(obj.get("stage_index", 0))
                if stage_index <= 0:
                    await websocket.send_text(json.dumps({"type":"error","payload":"invalid stage_index"}))
                else:
                    new_id = (obj.get("machine_id") or f"M_new_{stage_index}_{random.randint(100,999)}").strip()
                    base_rate = float(obj.get("base_rate", 50))
                    name = (obj.get("name") or new_id).strip()
                    async with stages_lock:
                        stg = next((s for s in stages if s.id == stage_index), None)
                        if not stg:
                            await websocket.send_text(json.dumps({"type":"error","payload":"stage not found"}))
                        elif any(m.id == new_id for m in stg.machines):
                            await websocket.send_text(json.dumps({"type":"error","payload":"machine id exists"}))
                        else:
                            stg.machines.append(Machine(new_id, name, base_rate, stage_index))
                            refresh_machines_index()
                            updated = True
            elif action == "set_stage_strategy":
                stage_id = int(obj.get("stage_id"))
                strategy = obj.get("routing_strategy")
                if strategy not in ALLOWED_ROUTING:
                    await websocket.send_text(json.dumps({"type":"error","payload":"invalid routing strategy"}))
                else:
                    async with stages_lock:
                        st = next((s for s in stages if s.id == stage_id), None)
                        if not st:
                            await websocket.send_text(json.dumps({"type":"error","payload":"stage not found"}))
                        else:
                            st.routing_strategy = strategy
                            updated = True
            elif action == "delete_machine":
                mid = obj.get("machine_id")
                if not mid:
                    await websocket.send_text(json.dumps({"type":"error","payload":"machine_id required"}))
                else:
                    async with stages_lock:
                        found = False
                        for st in stages:
                            for i, m in enumerate(list(st.machines)):
                                if m.id == mid:
                                    st.input_queue += m.queue
                                    del st.machines[i]
                                    found = True
                                    break
                            if found:
                                break
                        if not found:
                            await websocket.send_text(json.dumps({"type":"error","payload":"machine not found"}))
                        else:
                            refresh_machines_index()
                            updated = True
            elif action == "delete_machine_by_name":
                mname = obj.get("name")
                stage_id = obj.get("stage_id")
                if not mname:
                    await websocket.send_text(json.dumps({"type":"error","payload":"name required"}))
                else:
                    async with stages_lock:
                        matches = []
                        for st in stages:
                            if stage_id is not None and st.id != int(stage_id):
                                continue
                            for i, m in enumerate(st.machines):
                                if m.name == mname:
                                    matches.append((st, i, m))
                        if not matches:
                            await websocket.send_text(json.dumps({"type":"error","payload":"machine name not found"}))
                        else:
                            st, i, m = matches[0]
                            st.input_queue += m.queue
                            del st.machines[i]
                            refresh_machines_index()
                            updated = True
            elif action == "reset_simulation":
                reset_simulation()
                updated = True
            else:
                await websocket.send_text(json.dumps({"type":"error","payload":"unknown action"}))
                continue

            if updated:
                current_payload = {
                    "time": twin_state["time"],
                    "ambient_temp": twin_state["ambient_temp"],
                    "ambient_humidity": twin_state["ambient_humidity"],
                    "total_output": twin_state["total_output"],
                    "stages": stages_snapshot(),
                    "machines": machines_snapshot(),
                    "bottlenecks": compute_bottlenecks(),
                    "staffing_shifts": twin_state["staffing_shifts"],
                }
                await manager.broadcast({"type": "metrics", "payload": current_payload})
                # also broadcast light announcement for selector refresh
                await manager.broadcast({'type': 'machines_updated', 'machines': machines_snapshot(), 'stages': stages_snapshot()})
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)

# -------------------
# HTTP ENDPOINTS
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
        "stages": stages_snapshot(),
        "machines": machines_snapshot(),
    }
    return JSONResponse(content=data)

@app.post("/api/import")
async def api_import(payload: dict, current_user: dict = Depends(require_admin)):
    try:
        loaded_stages = []
        if isinstance(payload.get("stages"), list) and payload["stages"]:
            for s in payload["stages"]:
                sid = int(s.get("id"))
                mlist = []
                for m_data in s.get("machines", []):
                    machine = Machine(
                        m_data.get("id"),
                        m_data.get("name", m_data.get("id")),
                        float(m_data.get("base_rate", 50)),
                        int(m_data.get("stage_id", m_data.get("position", sid)))
                    )
                    machine.from_dict(m_data)
                    mlist.append(machine)
                routing_strategy = s.get("routing_strategy", "shortest_queue")
                st_obj = Stage(sid, mlist, routing_strategy=routing_strategy)
                st_obj.input_queue = float(s.get("input_queue", 0.0))
                loaded_stages.append(st_obj)
        elif isinstance(payload.get("machines"), list):
            # fallback: group by position
            by_stage: Dict[int, List[Machine]] = {}
            for m_data in payload["machines"]:
                sid = int(m_data.get("stage_id", m_data.get("position", 1)))
                mach = Machine(
                    m_data.get("id"),
                    m_data.get("name", m_data.get("id")),
                    float(m_data.get("base_rate", 50)),
                    sid
                )
                mach.from_dict(m_data)
                by_stage.setdefault(sid, []).append(mach)
            for sid in sorted(by_stage.keys()):
                loaded_stages.append(Stage(sid, by_stage[sid]))

        if loaded_stages:
            global stages
            stages = sorted(loaded_stages, key=lambda s: s.id)
            refresh_machines_index()

        ts = payload.get("twin_state") or {}
        if isinstance(ts, dict):
            twin_state["staffing_shifts"] = int(ts.get("staffing_shifts", twin_state["staffing_shifts"]))
            twin_state["ambient_temp"] = float(ts.get("ambient_temp", twin_state["ambient_temp"]))
            twin_state["ambient_humidity"] = float(ts.get("ambient_humidity", twin_state["ambient_humidity"]))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {e}")

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