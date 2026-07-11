"""Streamlit dashboard for cal-task-sync.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.read import get_completions, get_wake_ups, get_weights  # noqa: E402

st.set_page_config(page_title="cal-task-sync", layout="wide")
config = load_config()

st.title("Daily Tasks: Wake Time & Weight")
st.caption(f"Storage backend: {config.storage_backend}")


def _minutes_to_label(minutes: float) -> str:
    minutes = int(minutes)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


wake = get_wake_ups(config)
weight = get_weights(config)
completions = get_completions(config)

col1, col2 = st.columns(2)

with col1:
    st.subheader("Wake-up time over time")
    if wake.empty:
        st.info("No wake-up data yet. Run the sync first.")
    else:
        fig = px.line(wake, x="day", y="wake_minutes", markers=True)
        tick_vals = list(range(0, 24 * 60 + 1, 60))
        fig.update_yaxes(
            tickmode="array",
            tickvals=tick_vals,
            ticktext=[_minutes_to_label(v) for v in tick_vals],
            title="Wake-up time",
            autorange="reversed",  # earlier (smaller) at top reads like "better"
        )
        fig.update_xaxes(title="Date")
        st.plotly_chart(fig, use_container_width=True)
        st.metric("Latest wake-up", wake.iloc[-1]["wake_time"])

with col2:
    st.subheader("Weight over time")
    if weight.empty:
        st.info("No weight data yet. Run the sync first.")
    else:
        fig = px.line(weight, x="day", y="weight_lbs", markers=True)
        fig.update_yaxes(title="Weight (lbs)")
        fig.update_xaxes(title="Date")
        st.plotly_chart(fig, use_container_width=True)
        latest = weight.iloc[-1]
        delta = None
        if pd.notna(latest.get("start_weight_lbs")):
            delta = round(float(latest["weight_lbs"]) - float(latest["start_weight_lbs"]), 1)
        st.metric("Current weight", f"{latest['weight_lbs']} lbs", delta=f"{delta} lbs vs start" if delta is not None else None)

st.divider()
st.subheader("Task completion overview")
if completions.empty:
    st.info("No task data yet. Run the sync first.")
else:
    done = completions[completions["is_completed"].astype(bool)].copy()
    if done.empty:
        st.info("No completed tasks recorded yet.")
    else:
        by_day = done.dropna(subset=["day"]).groupby(done["day"].dt.date).size().reset_index(name="completed")
        by_day.columns = ["day", "completed"]
        fig = px.bar(by_day, x="day", y="completed")
        fig.update_layout(title="Completed tasks per day")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(completions.sort_values("updated_at", ascending=False), use_container_width=True)
