
# pdm_single_file_app_pro.py
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta, date

st.set_page_config(page_title="AI Predictive Maintenance - Single File Demo (Pro)", layout="wide")

# ---- Optional minimal theming / branding ----
st.markdown("""
    <style>
    .stApp { font-family: 'Segoe UI', system-ui; }
    </style>
""", unsafe_allow_html=True)
# Optional logo: place a local file named 'logo.png' in the same folder.
# st.image("logo.png", width=140)

# ---- Cost assumptions (you can edit) ----
DOWNTIME_COST_PER_HOUR = {
    "M1 Cutting": 120.0,
    "M2 Press":   180.0,
    "M3 Paint":   150.0,
    "M4 Inspect": 100.0,
}
EXPECTED_HOURS_IF_FAIL = 4  # estimated downtime hours per failure

# -----------------------------
# 1) Generate dummy data in-code
# -----------------------------
@st.cache_data
def make_data(seed: int = 42):
    np.random.seed(seed)

    end_date = datetime(2025, 8, 14)
    start_date = end_date - timedelta(days=29)  # 30 days
    dates = pd.date_range(start_date, end_date, freq="D")

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
        future_failures = sorted([d for d in failure_dates[m["name"]] if d >= start_date])
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

            actual_failure = int(d in failure_dates[m["name"]])

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

# ---- Load / make data ----
df = make_data()
df["date"] = pd.to_datetime(df["date"])
df["risk_level"] = df["risk_score"].apply(risk_level)

st.title("AI Predictive Maintenance - Single File Demo (Pro)")
st.caption("M1 Cutting • M2 Press • M3 Paint • M4 Inspect")

# --- Sidebar filters ---
machines = ["All"] + sorted(df["machine_name"].unique().tolist())
machine_sel = st.sidebar.selectbox("Machine", machines, index=0)

# Date slider using Python datetime objects
min_date = pd.to_datetime(df["date"]).min().to_pydatetime()
max_date = pd.to_datetime(df["date"]).max().to_pydatetime()
start_date, end_date = st.sidebar.slider(
    "Date range",
    min_value=min_date,
    max_value=max_date,
    value=(min_date, max_date),
    step=timedelta(days=1),
    format="YYYY-MM-DD",
)

show_predictions = st.sidebar.checkbox("Show only predicted failures (within 7 days)", value=False)

# Filtering
filtered = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
if machine_sel != "All":
    filtered = filtered[filtered["machine_name"] == machine_sel]

if show_predictions:
    filtered = filtered[filtered["predict_fail_within_7d"] == 1]

# --- KPIs (based on the latest date available) ---
latest_date = df["date"].max()
latest = df[df["date"] == latest_date]
hi_risk_machines = latest[latest["risk_score"] >= 70]["machine_name"].nunique()
predicted_within_7d = latest[latest["predict_fail_within_7d"] == 1]["machine_name"].nunique()
actual_failures_today = latest[latest["actual_failure"] == 1]["machine_name"].nunique()

# Estimated 7-day downtime cost (USD)
latest_pred = df[(df["date"] == latest_date) & (df["predict_fail_within_7d"] == 1)]
est_cost = 0.0
for _, row in latest_pred.iterrows():
    est_cost += DOWNTIME_COST_PER_HOUR[row["machine_name"]] * EXPECTED_HOURS_IF_FAIL

c1, c2, c3, c4 = st.columns(4)
c1.metric("High-Risk Machines (today)", hi_risk_machines)
c2.metric("Predicted Fail (≤ 7 days)", predicted_within_7d)
c3.metric("Actual Failures (today)", actual_failures_today)
c4.metric("Est. 7-day Downtime Cost (USD)", f"{est_cost:,.0f}")

st.divider()

# --- Alerts Panel ---
critical = latest[latest["risk_score"] >= 80][["machine_name","risk_score"]]
high     = latest[(latest["risk_score"] >= 60) & (latest["risk_score"] < 80)][["machine_name","risk_score"]]
if not critical.empty or not high.empty:
    with st.expander("🚨 Alerts (today)", expanded=True):
        if not critical.empty:
            st.error("Critical:", icon="🚨")
            st.table(critical.rename(columns={"machine_name":"Machine","risk_score":"Risk"}))
        if not high.empty:
            st.warning("High:", icon="⚠️")
            st.table(high.rename(columns={"machine_name":"Machine","risk_score":"Risk"}))

# --- Machine Status (today) ---
st.subheader("Machine Status (today)")
cols = st.columns(4)
for i, mname in enumerate(sorted(df["machine_name"].unique().tolist())):
    sub = latest[latest["machine_name"] == mname]
    val = float(sub["risk_score"].iloc[0]) if not sub.empty else 0.0
    label, dot = status_badge(val)
    with cols[i]:
        st.markdown(f"**{mname}**")
        st.markdown(f"{dot} {label}  ·  Risk: **{val:.1f}**")

st.divider()

# --- Gauges per machine (today) ---
st.subheader("Current Risk Gauges")
gcols = st.columns(4)
for idx, mname in enumerate(sorted(df["machine_name"].unique().tolist())):
    sub = latest[latest["machine_name"] == mname]
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

# --- Maintenance Recommendations ---
st.subheader("Maintenance Recommendations")
recs = []
for mname in sorted(df["machine_name"].unique().tolist()):
    sub = latest[latest["machine_name"] == mname]
    if sub.empty:
        continue
    r = float(sub["risk_score"].iloc[0])
    when = next_maintenance_suggestion(r, latest_date.date())
    recs.append({
        "Machine": mname,
        "Risk": round(r,1),
        "Suggested Maintenance Date": when.isoformat() if when else "-"
    })
st.dataframe(pd.DataFrame(recs), use_container_width=True, hide_index=True)

# --- Action: Mock CMMS Ticket ---
st.subheader("Action")
mnames = sorted(df["machine_name"].unique().tolist())
sel_m = st.selectbox("Select machine to schedule maintenance", mnames)
if st.button("Create Maintenance Ticket"):
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
    file_name="factory_dummy_data.csv",
    mime="text/csv"
)

st.caption("Single-file demo: data is generated in-code. Risk is derived from temperature, vibration, and days since last maintenance.")
