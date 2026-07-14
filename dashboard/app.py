"""Streamlit dashboard for cal-task-sync.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.read import get_completions, get_wake_ups, get_weights  # noqa: E402

st.set_page_config(page_title="cal-task-sync", layout="wide")
config = load_config()

st.title("Daily Tasks: Wake Time & Weight")
st.caption(f"Storage backend: {config.storage_backend}")


def _minutes_to_label(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _date_range(days: pd.Series, pad_days: int = 14) -> list[str] | None:
    """Widen the x-axis so a single point isn't zoomed into sub-seconds."""
    if days.empty:
        return None
    start = pd.Timestamp(days.min()).normalize() - timedelta(days=pad_days)
    end = pd.Timestamp(days.max()).normalize() + timedelta(days=pad_days)
    # Always show at least ~30 days of context.
    if (end - start).days < 30:
        mid = pd.Timestamp(days.min()).normalize()
        start = mid - timedelta(days=15)
        end = mid + timedelta(days=15)
    return [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")]


def _wake_fig(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        data=go.Scatter(
            x=df["day"],
            y=df["wake_minutes"],
            mode="lines+markers",
            marker={"size": 10},
            line={"width": 2},
            hovertemplate="%{x|%b %d, %Y}<br>%{customdata}<extra></extra>",
            customdata=df["wake_time"],
            name="Wake-up",
        )
    )
    # Tick every hour, but zoom y around the data.
    y_min = max(0, int(df["wake_minutes"].min()) - 60)
    y_max = min(24 * 60, int(df["wake_minutes"].max()) + 60)
    tick_vals = list(range((y_min // 60) * 60, y_max + 1, 60))
    fig.update_yaxes(
        tickmode="array",
        tickvals=tick_vals,
        ticktext=[_minutes_to_label(v) for v in tick_vals],
        range=[y_max, y_min],  # reversed: earlier at top
        title="Wake-up time",
    )
    x_range = _date_range(df["day"])
    fig.update_xaxes(title="Date", type="date", range=x_range, tickformat="%b %d\n%Y")
    fig.update_layout(margin={"t": 20, "b": 40}, height=420, showlegend=False)
    return fig


def _weight_fig(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        data=go.Scatter(
            x=df["day"],
            y=df["weight_lbs"],
            mode="lines+markers",
            marker={"size": 10},
            line={"width": 2},
            hovertemplate="%{x|%b %d, %Y}<br>%{y:.1f} lbs<extra></extra>",
            name="Weight",
        )
    )
    y_min = float(df["weight_lbs"].min()) - 5
    y_max = float(df["weight_lbs"].max()) + 5
    fig.update_yaxes(title="Weight (lbs)", range=[y_min, y_max])
    fig.update_xaxes(title="Date", type="date", range=_date_range(df["day"]), tickformat="%b %d\n%Y")
    fig.update_layout(margin={"t": 20, "b": 40}, height=420, showlegend=False)
    return fig


wake = get_wake_ups(config)
weight = get_weights(config)
completions = get_completions(config)

if not wake.empty:
    wake = wake.copy()
    wake["day"] = pd.to_datetime(wake["day"]).dt.normalize()
if not weight.empty:
    weight = weight.copy()
    weight["day"] = pd.to_datetime(weight["day"]).dt.normalize()

col1, col2 = st.columns(2)

with col1:
    st.subheader("Wake-up time over time")
    if wake.empty:
        st.info("No wake-up data yet. Run the sync first.")
    else:
        st.plotly_chart(_wake_fig(wake), use_container_width=True)
        st.metric("Latest wake-up", str(wake.iloc[-1]["wake_time"])[:5])
        st.caption(f"{len(wake)} logged day(s). Google Tasks keeps little history of recurring dailies — new syncs will add points over time.")

with col2:
    st.subheader("Weight over time")
    if weight.empty:
        st.info("No weight data yet. Run the sync first.")
    else:
        st.plotly_chart(_weight_fig(weight), use_container_width=True)
        latest = weight.iloc[-1]
        delta = None
        if pd.notna(latest.get("start_weight_lbs")):
            delta = round(float(latest["weight_lbs"]) - float(latest["start_weight_lbs"]), 1)
        st.metric(
            "Current weight",
            f"{latest['weight_lbs']} lbs",
            delta=f"{delta} lbs vs start" if delta is not None else None,
        )
        st.caption(f"{len(weight)} logged day(s), including any seeded start weight.")

st.divider()
st.subheader("Task completion overview")
if completions.empty:
    st.info("No task data yet. Run the sync first.")
else:
    # A task can arrive from both the Tasks and Calendar sources; collapse to one
    # row per (day, title), keeping the completed state if any source says done.
    deduped = completions.dropna(subset=["day"]).copy()
    deduped["is_completed"] = deduped["is_completed"].astype(bool)
    deduped["day_only"] = deduped["day"].dt.date
    deduped = (
        deduped.sort_values("is_completed")
        .drop_duplicates(subset=["day_only", "title"], keep="last")
    )
    done = deduped[deduped["is_completed"]]
    if done.empty:
        st.info("No completed tasks recorded yet.")
    else:
        by_day = done.groupby("day_only").size().reset_index(name="completed")
        by_day.columns = ["day", "completed"]
        fig = px.bar(by_day, x="day", y="completed")
        fig.update_layout(title="Completed tasks per day", height=360)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(
            deduped.sort_values(["day_only", "title"], ascending=[False, True]),
            use_container_width=True,
        )
