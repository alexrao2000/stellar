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
import plotly.graph_objects as go
import streamlit as st

CHART_TYPES = ["Scatter + best fit", "Line"]

# Palette
ACCENT = "#4C6FFF"     # primary data
TREND = "#F4661B"      # trend / best-fit lines
GRID = "rgba(0,0,0,0.06)"
ROLLING_WINDOW = 14    # days, for the completion trend line

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import detect_local_timezone, load_config  # noqa: E402
from src.read import get_completions, get_wake_ups, get_weights  # noqa: E402
from src.timezones import resolve_timezone  # noqa: E402

st.set_page_config(page_title="cal-task-sync", page_icon="✅", layout="wide")

# --- Timezone resolution (auto-detect, else prompt) ---
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
_active_tz = resolve_timezone(config, allow_google=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _minutes_to_label(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _style(fig: go.Figure, height: int = 380) -> go.Figure:
    """Apply a clean, consistent look to every chart."""
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin={"t": 30, "b": 40, "l": 10, "r": 10},
        font={"family": "Inter, -apple-system, Segoe UI, sans-serif", "size": 13},
        legend={"orientation": "h", "y": 1.12, "x": 0, "bgcolor": "rgba(0,0,0,0)"},
        hoverlabel={"font_size": 12},
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def _date_range(days: pd.Series, pad_days: int = 14) -> list[str] | None:
    if days.empty:
        return None
    start = pd.Timestamp(days.min()).normalize() - timedelta(days=pad_days)
    end = pd.Timestamp(days.max()).normalize() + timedelta(days=pad_days)
    if (end - start).days < 30:
        mid = pd.Timestamp(days.min()).normalize()
        start = mid - timedelta(days=15)
        end = mid + timedelta(days=15)
    return [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")]


def _best_fit(days: pd.Series, y: pd.Series):
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


def _series_trace(df, y_col, chart_type, name, hovertemplate, customdata=None):
    mode = "markers" if chart_type == "Scatter + best fit" else "lines+markers"
    return go.Scatter(
        x=df["day"], y=df[y_col], mode=mode,
        marker={"size": 7, "opacity": 0.7, "color": ACCENT},
        line={"width": 2, "color": ACCENT},
        hovertemplate=hovertemplate, customdata=customdata, name=name,
    )


def _trend_trace(days, y, label_fmt):
    fit = _best_fit(days, y)
    if not fit:
        return None
    x_dates, y_ends, slope = fit
    return go.Scatter(
        x=x_dates, y=y_ends, mode="lines",
        line={"width": 3, "color": TREND},
        name=label_fmt(slope), hoverinfo="skip",
    )


def _wake_fig(df: pd.DataFrame, chart_type: str) -> go.Figure:
    fig = go.Figure(_series_trace(
        df, "wake_minutes", chart_type, "Wake-up",
        "%{x|%b %d, %Y}<br>%{customdata}<extra></extra>", customdata=df["wake_time"],
    ))
    if chart_type == "Scatter + best fit":
        t = _trend_trace(df["day"], df["wake_minutes"], lambda s: f"Trend ({s * 30:+.1f} min/mo)")
        if t:
            fig.add_trace(t)
    y_min = max(0, int(df["wake_minutes"].min()) - 60)
    y_max = min(24 * 60, int(df["wake_minutes"].max()) + 60)
    tick_vals = list(range((y_min // 60) * 60, y_max + 1, 60))
    fig.update_yaxes(
        tickmode="array", tickvals=tick_vals,
        ticktext=[_minutes_to_label(v) for v in tick_vals],
        range=[y_max, y_min], title=None,
    )
    fig.update_xaxes(type="date", range=_date_range(df["day"]), tickformat="%b %d\n%Y")
    return _style(fig)


def _weight_fig(df: pd.DataFrame, chart_type: str) -> go.Figure:
    fig = go.Figure(_series_trace(
        df, "weight_lbs", chart_type, "Weight",
        "%{x|%b %d, %Y}<br>%{y:.1f} lbs<extra></extra>",
    ))
    if chart_type == "Scatter + best fit":
        t = _trend_trace(df["day"], df["weight_lbs"], lambda s: f"Trend ({s * 30:+.1f} lbs/mo)")
        if t:
            fig.add_trace(t)
    y_min = float(df["weight_lbs"].min()) - 5
    y_max = float(df["weight_lbs"].max()) + 5
    fig.update_yaxes(range=[y_min, y_max], title="lbs")
    fig.update_xaxes(type="date", range=_date_range(df["day"]), tickformat="%b %d\n%Y")
    return _style(fig)


def _completion_fig(by_day: pd.DataFrame) -> go.Figure:
    """Bar chart of completed tasks/day plus a rolling-average trend line."""
    # Fill missing days with 0 so the rolling average is honest.
    full = pd.date_range(by_day["day"].min(), by_day["day"].max(), freq="D")
    series = by_day.set_index("day")["completed"].reindex(full, fill_value=0)
    rolling = series.rolling(ROLLING_WINDOW, min_periods=1).mean()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=series.index, y=series.values, name="Completed",
        marker={"color": ACCENT, "opacity": 0.35},
        hovertemplate="%{x|%b %d, %Y}<br>%{y} tasks<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=rolling.index, y=rolling.values, mode="lines",
        line={"width": 3, "color": TREND},
        name=f"{ROLLING_WINDOW}-day average",
        hovertemplate="%{x|%b %d, %Y}<br>%{y:.1f} avg<extra></extra>",
    ))
    fig.update_yaxes(title="tasks / day")
    fig.update_xaxes(type="date", tickformat="%b %Y")
    return _style(fig, height=340)


def _stat_row(items: list[tuple[str, str, str | None]]) -> None:
    """Render a row of metrics: list of (label, value, delta)."""
    cols = st.columns(len(items))
    for col, (label, value, delta) in zip(cols, items):
        col.metric(label, value, delta=delta)


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("Daily Tracker")
head_l, head_r = st.columns([4, 1])
with head_l:
    st.caption(
        f"Backend: {config.storage_backend} · Timezone: {_active_tz} "
        "(sync uses your Google Calendar timezone)"
    )
with head_r:
    if st.button("Sync now", type="primary", use_container_width=True):
        with st.spinner("Syncing from Google Tasks..."):
            try:
                from src.sync import run as run_sync

                run_sync()
                st.cache_data.clear()
                st.success("Sync complete.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Sync failed: {exc}")

wake = get_wake_ups(config)
weight = get_weights(config)
completions = get_completions(config)
if not wake.empty:
    wake = wake.copy()
    wake["day"] = pd.to_datetime(wake["day"]).dt.normalize()
if not weight.empty:
    weight = weight.copy()
    weight["day"] = pd.to_datetime(weight["day"]).dt.normalize()

# --------------------------------------------------------------------------- #
# Weight
# --------------------------------------------------------------------------- #
st.subheader("Weight")
if weight.empty:
    st.info("No weight data yet. Run the sync first.")
else:
    latest = weight.iloc[-1]
    peak = weight.loc[weight["weight_lbs"].idxmax()]
    floor = weight.loc[weight["weight_lbs"].idxmin()]
    avg = weight["weight_lbs"].mean()
    start_delta = None
    if pd.notna(latest.get("start_weight_lbs")):
        start_delta = f"{float(latest['weight_lbs']) - float(latest['start_weight_lbs']):+.1f} vs start"
    recent = weight[weight["day"] >= latest["day"] - pd.Timedelta(days=30)]
    mo_delta = None
    if len(recent) >= 2:
        mo_delta = f"{float(recent.iloc[-1]['weight_lbs']) - float(recent.iloc[0]['weight_lbs']):+.1f} in 30d"

    _stat_row([
        ("Current", f"{latest['weight_lbs']:.1f} lbs", start_delta),
        ("Peak", f"{peak['weight_lbs']:.1f} lbs", peak["day"].strftime("%b %d, %Y")),
        ("Floor", f"{floor['weight_lbs']:.1f} lbs", floor["day"].strftime("%b %d, %Y")),
        ("Average", f"{avg:.1f} lbs", None),
        ("Last 30 days", f"{recent.iloc[-1]['weight_lbs']:.1f} lbs" if len(recent) else "—", mo_delta),
    ])
    weight_type = st.radio("Chart type", CHART_TYPES, horizontal=True,
                           key="weight_chart_type", label_visibility="collapsed")
    st.plotly_chart(_weight_fig(weight, weight_type), use_container_width=True)
    st.caption(f"{len(weight)} logged day(s).")

st.divider()

# --------------------------------------------------------------------------- #
# Wake-up time
# --------------------------------------------------------------------------- #
st.subheader("Wake-up time")
if wake.empty:
    st.info("No wake-up data yet. Run the sync first.")
else:
    avg_min = wake["wake_minutes"].mean()
    earliest = wake.loc[wake["wake_minutes"].idxmin()]
    latest_rise = wake.loc[wake["wake_minutes"].idxmax()]
    _stat_row([
        ("Latest logged", str(wake.iloc[-1]["wake_time"])[:5], wake.iloc[-1]["day"].strftime("%b %d, %Y")),
        ("Average", _minutes_to_label(avg_min), None),
        ("Earliest", str(earliest["wake_time"])[:5], earliest["day"].strftime("%b %d, %Y")),
        ("Latest riser", str(latest_rise["wake_time"])[:5], latest_rise["day"].strftime("%b %d, %Y")),
    ])
    wake_type = st.radio("Chart type", CHART_TYPES, horizontal=True,
                         key="wake_chart_type", label_visibility="collapsed")
    st.plotly_chart(_wake_fig(wake, wake_type), use_container_width=True)
    st.caption(f"{len(wake)} logged day(s). Lower on the chart = earlier.")

st.divider()

# --------------------------------------------------------------------------- #
# Task completion
# --------------------------------------------------------------------------- #
st.subheader("Task completion")
if completions.empty:
    st.info("No task data yet. Run the sync first.")
else:
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
        by_day["day"] = pd.to_datetime(by_day["day"])

        best = by_day.loc[by_day["completed"].idxmax()]
        _stat_row([
            ("Total completed", f"{int(done.shape[0]):,}", None),
            ("Active days", f"{by_day.shape[0]:,}", None),
            ("Avg / day", f"{by_day['completed'].mean():.1f}", None),
            ("Best day", f"{int(best['completed'])} tasks", best["day"].strftime("%b %d, %Y")),
        ])
        st.plotly_chart(_completion_fig(by_day), use_container_width=True)
        with st.expander("Browse all task records"):
            st.dataframe(
                deduped.sort_values(["day_only", "title"], ascending=[False, True]),
                use_container_width=True,
            )
