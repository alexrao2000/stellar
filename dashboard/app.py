"""Streamlit dashboard for cal-task-sync.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import available_timezones

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

CHART_TYPES = ["Scatter + best fit", "Line"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import detect_local_timezone, load_config  # noqa: E402
from src.read import get_completions, get_wake_ups, get_weights  # noqa: E402
from src.timezones import resolve_timezone  # noqa: E402

st.set_page_config(page_title="cal-task-sync", layout="wide")

# Resolve timezone: auto-detect, else let the user pick. The choice is applied to
# the config used for this session (and any manual sync triggered below).
_detected_tz = detect_local_timezone()
if not _detected_tz:
    _common = [
        "America/Los_Angeles", "America/Denver", "America/Chicago",
        "America/New_York", "UTC", "Europe/London", "Europe/Berlin",
        "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney",
    ]
    _all = sorted(available_timezones())
    with st.sidebar:
        st.warning("Could not auto-detect your timezone. Please pick one.")
        _choice = st.selectbox(
            "Timezone", options=_common + ["--- all ---"] + _all, index=0, key="tz_choice"
        )
    os.environ["LOCAL_TIMEZONE"] = _choice if _choice != "--- all ---" else "UTC"

config = load_config()

st.title("Daily Tasks: Wake Time & Weight")

_active_tz = resolve_timezone(config, allow_google=False)
_top_left, _top_right = st.columns([3, 1])
with _top_left:
    st.caption(
        f"Storage backend: {config.storage_backend}  |  Timezone: {_active_tz} "
        "(sync uses your Google Calendar timezone)"
    )
with _top_right:
    if st.button("Sync now", type="primary", use_container_width=True):
        with st.spinner("Running sync from Google Tasks..."):
            try:
                from src.sync import run as run_sync

                run_sync()
                st.cache_data.clear()
                st.success("Sync complete.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Sync failed: {exc}")


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


def _best_fit(days: pd.Series, y: pd.Series):
    """Return (x_endpoints, y_endpoints, slope_per_day) for an OLS best-fit line."""
    x_ord = days.map(pd.Timestamp.toordinal).to_numpy(dtype=float)
    y_arr = y.to_numpy(dtype=float)
    mask = ~np.isnan(x_ord) & ~np.isnan(y_arr)
    if mask.sum() < 2:
        return None
    slope, intercept = np.polyfit(x_ord[mask], y_arr[mask], 1)
    x_ends = np.array([x_ord[mask].min(), x_ord[mask].max()])
    y_ends = slope * x_ends + intercept
    x_dates = [pd.Timestamp.fromordinal(int(round(v))) for v in x_ends]
    return x_dates, y_ends, slope


def _series_trace(df: pd.DataFrame, y_col: str, chart_type: str, name: str, hovertemplate, customdata=None):
    """Scatter markers (default) or connected line for the primary data series."""
    mode = "markers" if chart_type == "Scatter + best fit" else "lines+markers"
    return go.Scatter(
        x=df["day"],
        y=df[y_col],
        mode=mode,
        marker={"size": 8, "opacity": 0.75},
        line={"width": 2},
        hovertemplate=hovertemplate,
        customdata=customdata,
        name=name,
    )


def _wake_fig(df: pd.DataFrame, chart_type: str) -> go.Figure:
    fig = go.Figure(
        data=_series_trace(
            df,
            "wake_minutes",
            chart_type,
            "Wake-up",
            "%{x|%b %d, %Y}<br>%{customdata}<extra></extra>",
            customdata=df["wake_time"],
        )
    )
    if chart_type == "Scatter + best fit":
        fit = _best_fit(df["day"], df["wake_minutes"])
        if fit:
            x_dates, y_ends, slope = fit
            fig.add_trace(
                go.Scatter(
                    x=x_dates,
                    y=y_ends,
                    mode="lines",
                    line={"width": 3, "dash": "solid", "color": "#e45756"},
                    name=f"Trend ({slope * 30:+.1f} min/mo)",
                    hoverinfo="skip",
                )
            )
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
    fig.update_layout(margin={"t": 20, "b": 40}, height=420, showlegend=True,
                      legend={"orientation": "h", "y": 1.08})
    return fig


def _weight_fig(df: pd.DataFrame, chart_type: str) -> go.Figure:
    fig = go.Figure(
        data=_series_trace(
            df,
            "weight_lbs",
            chart_type,
            "Weight",
            "%{x|%b %d, %Y}<br>%{y:.1f} lbs<extra></extra>",
        )
    )
    if chart_type == "Scatter + best fit":
        fit = _best_fit(df["day"], df["weight_lbs"])
        if fit:
            x_dates, y_ends, slope = fit
            fig.add_trace(
                go.Scatter(
                    x=x_dates,
                    y=y_ends,
                    mode="lines",
                    line={"width": 3, "color": "#e45756"},
                    name=f"Trend ({slope * 30:+.1f} lbs/mo)",
                    hoverinfo="skip",
                )
            )
    y_min = float(df["weight_lbs"].min()) - 5
    y_max = float(df["weight_lbs"].max()) + 5
    fig.update_yaxes(title="Weight (lbs)", range=[y_min, y_max])
    fig.update_xaxes(title="Date", type="date", range=_date_range(df["day"]), tickformat="%b %d\n%Y")
    fig.update_layout(margin={"t": 20, "b": 40}, height=420, showlegend=True,
                      legend={"orientation": "h", "y": 1.08})
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
        wake_type = st.radio(
            "Chart type", CHART_TYPES, horizontal=True, key="wake_chart_type"
        )
        st.plotly_chart(_wake_fig(wake, wake_type), use_container_width=True)
        st.metric("Latest wake-up", str(wake.iloc[-1]["wake_time"])[:5])
        st.caption(f"{len(wake)} logged day(s).")

with col2:
    st.subheader("Weight over time")
    if weight.empty:
        st.info("No weight data yet. Run the sync first.")
    else:
        weight_type = st.radio(
            "Chart type", CHART_TYPES, horizontal=True, key="weight_chart_type"
        )
        st.plotly_chart(_weight_fig(weight, weight_type), use_container_width=True)
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
