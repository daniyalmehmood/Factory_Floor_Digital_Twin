# pdm_single_file_app_pro_sim.py
import os
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta, date
import requests

st.set_page_config(page_title="AI Predictive Maintenance - Pro + Simulator", layout="wide")

# ---- Optional minimal theming / branding ----
st.markdown("""
    <style>
    .stApp { font-family: 'Segoe UI', system-ui; }
    </style>
""", unsafe_allow_html=True)

# ---- Cost assumptions (you can edit) ----
DOWNTIME_COST_PER_HOUR = {
    "M1 Cutting": 120.0,
    "M2 Press":   180.0,
    "M3 Paint":   150.0,
    "M4 Inspect": 100.0,
}
EXPECTED_HOURS_IF_FAIL = 4  # estimated downtime hours per failure

# ---- Backend integration settings ----
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").strip()
# If a token is provided, we'll use it; otherwise we will auto-login
BACKEND_JWT_TOKEN = os.getenv("BACKEND_JWT_TOKEN", "").strip()
BACKEND_USERNAME = os.getenv("BACKEND_USERNAME", "admin").strip()
BACKEND_PASSWORD = os.getenv("BACKEND_PASSWORD", "admin123").strip()

def _backend_url(path: str) -> str:
    return f"{BACKEND_BASE_URL.rstrip('/')}{path}"

def _login_for_token() -> str | None:
    """
    Attempt to log in to the FastAPI backend using BACKEND_USERNAME/PASSWORD
    and return an access token. Returns None on failure.
    """
    if not BACKEND_BASE_URL:
        return None
    try:
        resp = requests.post(
            _backend_url("/api/token"),
            json={"username": BACKEND_USERNAME, "password": BACKEND_PASSWORD},
            timeout=6,
        )
        if resp.ok:
            data = resp.json()
            return data.get("access_token")
    except Exception:
        pass
    return None

def get_backend_token() -> str | None:
    """
    Resolve a token to call protected backend endpoints:
    - Use BACKEND_JWT_TOKEN env if present
    - Otherwise cache one in session_state by auto-logging in
    """
    if BACKEND_JWT_TOKEN:
        return BACKEND_JWT_TOKEN

    token = st.session_state.get("backend_token")
    if token:
        return token

    token = _login_for_token()
    if token:
        st.session_state["backend_token"] = token
        return token
    return None

def backend_get_json(path: str, retry_once: bool = True):
    """
    GET helper that attaches Authorization header automatically.
    If a 401 is returned, it will re-login once and retry.
    Returns (json_or_none, status_code).
    """
    if not BACKEND_BASE_URL:
        return None, None

    token = get_backend_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(_backend_url(path), headers=headers, timeout=6)
        if resp.status_code == 401 and retry_once:
            # Token might be expired or invalid, re-login and retry once
            st.session_state.pop("backend_token", None)
            new_token = _login_for_token()
            if new_token:
                st.session_state["backend_token"] = new_token
                headers["Authorization"] = f"Bearer {new_token}"
                resp = requests.get(_backend_url(path), headers=headers, timeout=6)
        if resp.ok:
            return resp.json(), resp.status_code
        return None, resp.status_code
    except Exception:
        return None, None

def try_load_backend_machines(base_url: str | None, token: str | None = None):
    """
    Optionally pull live machine list from FastAPI backend (/api/latest).
    This keeps the Streamlit UI unchanged; if the call fails, we fall back
    to locally generated dummy machines.
    """
    if not base_url:
        return None

    data, status = backend_get_json("/api/latest")
    if not data or "machines" not in data:
        return None

    machines = data.get("machines") or []
    if not machines:
        return None

    # FIX: stable sort by position; if missing, fall back to original order
    sorted_machines = [
        m for _, m in sorted(
            enumerate(machines),
            key=lambda pair: (
                pair[1].get("position") is None,
                pair[1].get("position", pair[0])
            )
        )
    ]

    # Map backend machines into generator-friendly structure (cap at 4 to fit existing layout)
    mapped = []
    for i, m in enumerate(sorted_machines[:4]):
        base_rate = float(m.get("base_rate", 50.0))
        # derive stable pseudo sensor baselines from base_rate/id hash
        br_mod = (base_rate % 25.0)
        temp_base = 65.0 + br_mod            # ~65-90
        vib_base = 0.30 + (br_mod % 10) / 100.0  # ~0.30-0.40
        press_base = 4.8 + (br_mod % 7) / 10.0   # ~4.8-5.5
        mapped.append({
            "id": m.get("id", f"M{i+1}"),
            "name": m.get("name", f"Machine {i+1}"),
            "temp_base": float(temp_base),
            "vib_base": float(vib_base),
            "press_base": float(press_base),
            "last_maint": datetime.utcnow() - timedelta(days=int(np.random.randint(6, 12)))
        })
    return mapped

# -----------------------------
# 1) Generate data (optionally seeded from backend machine list)
# -----------------------------
@st.cache_data
def make_data(seed: int = 42, backend_base_url: str | None = None, backend_token_marker: str = "auto"):
    """
    backend_token_marker exists only to let Streamlit cache invalidate when the token source flips
    (e.g., env token provided vs auto-login). We don't store the sensitive token itself here.
    """
    np.random.seed(seed)

    end_date = datetime(2025, 8, 14)
    start_date = end_date - timedelta(days=29)  # 30 days
    dates = pd.date_range(start_date, end_date, freq="D")

    machines_from_backend = try_load_backend_machines(backend_base_url)

    if machines_from_backend:
        machines = machines_from_backend
        # generic failure dates per machine (1 event randomly placed in window)
        failure_dates = {}
        for m in machines:
            # Choose one failure event between start_date+10 and end_date-2
            fail_day = start_date + timedelta(days=int(np.random.randint(10, 26)))
            failure_dates[m["name"]] = [fail_day]
    else:
        machines = [
            {"id": "M1", "name": "M1 Cutting", "temp_base": 70, "vib_base": 0.35, "press_base": 5.1, "last_maint": datetime(2025, 7, 18)},
            {"id": "M2", "name": "M2 Press",   "temp_base": 82, "vib_base": 0.55, "press_base": 5.3, "last_maint": datetime(2025, 7, 16)},
            {"id": "M3", "name": "M3 Paint",   "temp_base": 66, "vib_base": 0.30, "press_base": 4.9, "last_maint": datetime(2025, 7, 22)},
            {"id": "M4", "name": "M4 Inspect", "temp_base": 78, "vib_base": 0.42, "press_base": 5.0, "last_maint": datetime(2025, 7, 20)},
        ]
        failure_dates = {
            "M1 Cutting": [datetime(2025, 7, 30)],
            "M2 Press":   [datetime(2025, 8, 10)],
            "M3 Paint":   [datetime(2025, 8, 12)],
            "M4 Inspect": [datetime(2025, 8, 5)],
        }

    rows = []
    for m in machines:
        last_maint_date = m["last_maint"]
        fut = failure_dates.get(m["name"], [])
        future_failures = sorted([d for d in fut if d >= start_date])
        next_failure = future_failures[0] if future_failures else None

        for d in dates:
            days_since_maint = (d - last_maint_date).days
            runtime_hours = np.clip(np.random.normal(16, 3), 8, 24)

            temp = m["temp_base"] + np.random.normal(0, 2)
            vib  = m["vib_base"] + np.random.normal(0, 0.06)
            press = m["press_base"] + np.random.normal(0, 0.08)

            if next_failure is not None:
                days_to_fail = (next_failure - d).days
                if 0 <= days_to_fail <= 7:
                    temp += (7 - days_to_fail) * 3.0
                    vib  += (7 - days_to_fail) * 0.08

            temp = float(np.clip(temp, 50, 115))
            vib  = float(np.clip(vib, 0.15, 1.5))
            press = float(np.clip(press, 4.5, 6.2))

            actual_failure = int(d in failure_dates.get(m["name"], []))

            if actual_failure:
                maint_performed = 1
            else:
                maint_performed = 1 if (np.random.rand() < 0.03 and days_since_maint > 7) else 0

            # Risk score (rule-based)
            temp_score  = np.clip((temp - 60) / 40, 0, 1)    # 60..100C
            vib_score   = np.clip((vib - 0.2) / 0.9, 0, 1)   # 0.2..1.1
            maint_score = np.clip(days_since_maint / 30, 0, 1)

            risk = 45*temp_score + 35*vib_score + 20*maint_score

            # occasional spike bonus
            if np.random.rand() < 0.08:
                risk += 8
                temp += 2.5

            risk = float(np.clip(risk, 0, 100))

            failure_expected = int((risk >= 70) or (temp > 93 and vib > 0.9) or (days_since_maint >= 27))

            rows.append({
                "date": d.date().isoformat(),
                "machine_id": m["id"],
                "machine_name": m["name"],
                "runtime_hours": round(runtime_hours, 2),
                "temperature_c": round(temp, 2),
                "vibration_g": round(vib, 3),
                "pressure_bar": round(press, 2),
                "days_since_maintenance": days_since_maint,
                "maintenance_performed": maint_performed,
                "risk_score": round(risk, 1),
                "failure_expected": failure_expected,
                "actual_failure": actual_failure
            })

            if actual_failure or maint_performed:
                last_maint_date = d + timedelta(days=1)

    df = pd.DataFrame(rows)

    # Predict failure within next 7 days (look-ahead window)
    df["predict_fail_within_7d"] = 0
    for mname in df["machine_name"].unique():
        sub = df[df["machine_name"] == mname].copy()
        idxs = sub.index
        for i in range(len(sub)):
            window = sub.iloc[i:i+7]
            df.loc[idxs[i], "predict_fail_within_7d"] = int((window["failure_expected"] == 1).any())
    return df

def risk_level(r):
    if r >= 80: return "Critical"
    if r >= 60: return "High"
    if r >= 40: return "Medium"
    return "Low"

def status_badge(r):
    if r >= 80: return ("Critical", "🔴")
    if r >= 60: return ("Warning",  "🟠")
    return ("Good", "🟢")

def next_maintenance_suggestion(risk, today: date):
    if risk >= 80: return today + timedelta(days=1)
    if risk >= 60: return today + timedelta(days=3)
    return None

# ---- Load base data ----
# Use the token source as a cache marker: 'env' vs 'auto' vs 'none'
if BACKEND_JWT_TOKEN:
    token_marker = "env"
elif BACKEND_BASE_URL:
    token_marker = "auto"
else:
    token_marker = "none"

df_base = make_data(42, BACKEND_BASE_URL or None, token_marker)
df_base["date"] = pd.to_datetime(df_base["date"])
df_base["risk_level"] = df_base["risk_score"].apply(risk_level)

# ---- Simulator state ----
if "sim_events" not in st.session_state:
    st.session_state["sim_events"] = []  # list of {type, machine, date}

def apply_simulations(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for ev in st.session_state["sim_events"]:
        m = ev["machine"]
        d = pd.to_datetime(ev["date"]).normalize()
        if ev["type"] == "actual_failure":
            mask = (df["machine_name"] == m) & (df["date"] == d)
            df.loc[mask, ["actual_failure", "failure_expected"]] = 1
            df.loc[mask, "risk_score"] = 95.0
        elif ev["type"] == "spike_3d":
            # three days leading up to the date (exclusive)
            for delta in [3,2,1]:
                day = d - timedelta(days=delta)
                mask = (df["machine_name"] == m) & (df["date"] == day)
                # boost readings and risk
                df.loc[mask, "temperature_c"] = (df.loc[mask, "temperature_c"] + 15).clip(upper=115)
                df.loc[mask, "vibration_g"] = (df.loc[mask, "vibration_g"] + 0.5).clip(upper=1.5)
                df.loc[mask, "risk_score"] = (df.loc[mask, "risk_score"] + 30).clip(upper=100)
                df.loc[mask, "failure_expected"] = 1

    # Recompute predict_fail_within_7d after edits
    df = df.sort_values(["machine_name","date"]).reset_index(drop=True)
    df["predict_fail_within_7d"] = 0
    for mname in df["machine_name"].unique():
        sub = df[df["machine_name"] == mname]
        idxs = sub.index.to_list()
        for i, idx in enumerate(idxs):
            window = df.loc[idxs[i]:idxs[min(i+6, len(idxs)-1)], "failure_expected"]
            df.loc[idx, "predict_fail_within_7d"] = int((window == 1).any())
    return df

st.title("AI Predictive Maintenance - Pro + Simulator")
st.caption("Try injecting failures or sensor spikes to see how the system responds.")

# --- Sidebar filters ---
machines = ["All"] + sorted(df_base["machine_name"].unique().tolist())
machine_sel = st.sidebar.selectbox("Machine", machines, index=0)

# Date slider using Python datetime objects
min_date = pd.to_datetime(df_base["date"]).min().to_pydatetime()
max_date = pd.to_datetime(df_base["date"]).max().to_pydatetime()
start_date, end_date = st.sidebar.slider(
    "Date range",
    min_value=min_date,
    max_value=max_date,
    value=(min_date, max_date),
    step=timedelta(days=1),
    format="YYYY-MM-DD",
)

show_predictions = st.sidebar.checkbox("Show only predicted failures (within 7 days)", value=False)

# --- Simulation Lab ---
with st.sidebar.expander("🧪 Simulation Lab", expanded=True):
    sim_m = st.selectbox("Machine to simulate", sorted(df_base["machine_name"].unique().tolist()), key="sim_m")
    sim_date = st.date_input("Simulation date", value=max_date.date(), min_value=min_date.date(), max_value=max_date.date(), key="sim_date")
    cols = st.columns(2)
    if cols[0].button("Inject ACTUAL FAILURE on date"):
        st.session_state["sim_events"].append({"type":"actual_failure", "machine": sim_m, "date": sim_date})
        st.toast(f"Injected actual failure for {sim_m} on {sim_date}")
    if cols[1].button("Inject 3-day SPIKE before date"):
        st.session_state["sim_events"].append({"type":"spike_3d", "machine": sim_m, "date": sim_date})
        st.toast(f"Injected 3-day spike for {sim_m} before {sim_date}")
    if st.button("Clear simulations"):
        st.session_state["sim_events"] = []
        st.toast("Cleared simulations")

df = apply_simulations(df_base)

# Filtering
filtered = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
if machine_sel != "All":
    filtered = filtered[filtered["machine_name"] == machine_sel]

if show_predictions:
    filtered = filtered[filtered["predict_fail_within_7d"] == 1]

# --- KPIs and status use the slider's END DATE (focus day) ---
focus_date = pd.to_datetime(end_date).normalize()
focus_df = df[df["date"] == focus_date]

hi_risk_machines = focus_df[focus_df["risk_score"] >= 70]["machine_name"].nunique()
predicted_within_7d = focus_df[focus_df["predict_fail_within_7d"] == 1]["machine_name"].nunique()
actual_failures_today = focus_df[focus_df["actual_failure"] == 1]["machine_name"].nunique()

# Estimated 7-day downtime cost (USD) for machines flagged on focus day
est_cost = 0.0
for _, row in focus_df[focus_df["predict_fail_within_7d"] == 1].iterrows():
    est_cost += DOWNTIME_COST_PER_HOUR.get(row["machine_name"], 120.0) * EXPECTED_HOURS_IF_FAIL

c1, c2, c3, c4 = st.columns(4)
c1.metric("High-Risk Machines (selected day)", hi_risk_machines)
c2.metric("Predicted Fail (≤ 7 days)", predicted_within_7d)
c3.metric("Actual Failures (selected day)", actual_failures_today)
c4.metric("Est. 7-day Downtime Cost (USD)", f"{est_cost:,.0f}")

with st.expander("ℹ️ How the simulator works"):
    st.write("""
    • Inject ACTUAL FAILURE: marks the chosen machine as failed on the chosen date (risk→95, actual_failure=1).
    • Inject 3-day SPIKE: raises temperature & vibration for the three days before the chosen date and sets failure_expected=1 → predictions appear.
    • Use the slider's end date to inspect any day; KPIs/Gauges reflect that day.
    • Click Clear simulations to reset to original data.
    """)

st.divider()

# --- Machine Status (selected day) ---
def badge_tuple(r):
    if r >= 80: return ("Critical", "🔴")
    if r >= 60: return ("High",  "🟠")
    if r >= 40: return ("Medium", "🟡")
    return ("Good", "🟢")

st.subheader(f"Machine Status ({focus_date.date()})")
cols = st.columns(4)
for i, mname in enumerate(sorted(df["machine_name"].unique().tolist())[:4]):
    sub = focus_df[focus_df["machine_name"] == mname]
    val = float(sub["risk_score"].iloc[0]) if not sub.empty else 0.0
    label, dot = badge_tuple(val)
    with cols[i]:
        st.markdown(f"**{mname}**")
        st.markdown(f"{dot} {label}  ·  Risk: **{val:.1f}**")

st.divider()

# --- Gauges per machine (selected day) ---
st.subheader("Risk Gauges (selected day)")
gcols = st.columns(4)
for idx, mname in enumerate(sorted(df["machine_name"].unique().tolist())[:4]):
    sub = focus_df[focus_df["machine_name"] == mname]
    val = float(sub["risk_score"].iloc[0]) if not sub.empty else 0.0
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=val,
        title={"text": mname},
        gauge={"axis": {"range": [0, 100]}}
    ))
    gcols[idx].plotly_chart(fig, use_container_width=True)

st.divider()

# --- Time series for selected machine ---
st.subheader("Time Series (Temperature & Vibration)")
m_for_chart = "M2 Press" if machine_sel == "All" else machine_sel
ts = df[df["machine_name"] == m_for_chart].sort_values("date")

fig_ts = make_subplots(specs=[[{"secondary_y": True}]])
fig_ts.add_trace(go.Scatter(x=ts["date"], y=ts["temperature_c"], name="Temperature (°C)"), secondary_y=False)
fig_ts.add_trace(go.Scatter(x=ts["date"], y=ts["vibration_g"], name="Vibration (g)"), secondary_y=True)
fig_ts.update_yaxes(title_text="Temperature (°C)", secondary_y=False)
fig_ts.update_yaxes(title_text="Vibration (g)", secondary_y=True)
fig_ts.update_layout(height=400, margin=dict(l=10, r=10, t=30, b=10))
st.plotly_chart(fig_ts, use_container_width=True)

# --- Failure timeline (actual vs. predicted) ---
st.subheader("Failure Timeline")
tl = filtered.copy().sort_values(["date", "machine_name"])

af = tl[tl["actual_failure"] == 1]
pf = tl[tl["predict_fail_within_7d"] == 1]

fig_tl = go.Figure()
fig_tl.add_trace(go.Scatter(x=af["date"], y=af["machine_name"], mode="markers", name="Actual Failure", marker=dict(size=12, symbol="x")))
fig_tl.add_trace(go.Scatter(x=pf["date"], y=pf["machine_name"], mode="markers", name="Predicted (≤7d)", marker=dict(size=10)))
fig_tl.update_layout(xaxis_title="Date", yaxis_title="Machine", height=380, margin=dict(l=10, r=10, t=25, b=10))
st.plotly_chart(fig_tl, use_container_width=True)

st.divider()

# --- Maintenance Recommendations (based on selected day) ---
st.subheader("Maintenance Recommendations")
recs = []
for mname in sorted(df["machine_name"].unique().tolist()):
    sub = focus_df[focus_df["machine_name"] == mname]
    if sub.empty:
        continue
    r = float(sub["risk_score"].iloc[0])
    when = next_maintenance_suggestion(r, focus_date.date())
    recs.append({
        "Machine": mname,
        "Risk": round(r,1),
        "Suggested Maintenance Date": when.isoformat() if when else "-"
    })
st.dataframe(pd.DataFrame(recs), use_container_width=True, hide_index=True)

# --- Action: Mock CMMS Ticket ---
st.subheader("Action")
mnames = sorted(df["machine_name"].unique().tolist())
sel_m = st.selectbox("Select machine to schedule maintenance", mnames, key="cmms_select")
if st.button("Create Maintenance Ticket", key="cmms_button"):
    st.success(f"Ticket created for {sel_m} (mock). Your CMMS integration can receive this event via API.")

# --- Tabular view ---
st.subheader("Operational Table")
st.dataframe(
    filtered[
        ["date","machine_id","machine_name","runtime_hours","temperature_c","vibration_g","pressure_bar",
         "days_since_maintenance","risk_score","risk_level","failure_expected","actual_failure","predict_fail_within_7d"]
    ].sort_values(["date","machine_name"], ascending=[False, True]),
    use_container_width=True,
    hide_index=True
)

# --- Download ---
st.download_button(
    label="Download CSV",
    data=df.to_csv(index=False).encode("utf-8"),
    file_name="factory_dummy_data_simulated.csv",
    mime="text/csv"
)

st.caption("Pro + Simulator: Inject failures/spikes from the sidebar to see KPIs, gauges, and timelines react immediately.")

# To run this app, use the Docker container as described in the project README.