"""Plotly figures for the dashboard panels.

Every figure is built in the light theme; ``DARK_MAP`` maps each light colour to its dark-theme step, and the page
swaps them in the browser when the viewer prefers a dark scheme. Panel titles live in the HTML cards, not here.

Usage:
    from pl_power_market import figures
    fig = figures.negative_hours(metrics.negative_hours_monthly(rce))
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from pl_power_market.metrics import TZ

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
# chart chrome: light value -> dark value
CHROME = {
    "surface": ("#fcfcfb", "#1a1a19"),
    "ink": ("#0b0b0b", "#f0efec"),
    "ink2": ("#52514e", "#c3c2b7"),
    "muted": ("#898781", "#898781"),
    "grid": ("#e1e0d9", "#2c2c2a"),
    "axis": ("#c3c2b7", "#383835"),
}
# one colour per entity across the whole page (validated categorical slots, fixed order)
SERIES = {
    "rce": ("#2a78d6", "#3987e5"),
    "sdac": ("#eb6834", "#d95926"),
    "wind": ("#1baf7a", "#199e70"),
    "pv": ("#eda100", "#c98500"),
    "imb": ("#e87ba4", "#d55181"),
    "discharge": ("#008300", "#008300"),
    "charge": ("#6250d6", "#9085e9"),
    "other": ("#a9a8a1", "#5f5e5a"),
}
DARK_MAP = {light: dark for light, dark in [*CHROME.values(), *SERIES.values()] if light != dark}
# translucent fills are written as "rgba(r,g,b,a)"; the page maps their "rgba(r,g,b," prefix the same way
DARK_MAP.update({f"rgba({int(l[1:3], 16)},{int(l[3:5], 16)},{int(l[5:7], 16)},":
                 f"rgba({int(d[1:3], 16)},{int(d[3:5], 16)},{int(d[5:7], 16)},"
                 for l, d in SERIES.values() if l != d})
# sequential blue ramp (steps 100..700); dark mode runs it the other way so low values recede into the surface
RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

C = {k: v[0] for k, v in {**CHROME, **SERIES}.items()}


def rgba(hex_color: str, alpha: float) -> str:
    return f"rgba({int(hex_color[1:3], 16)},{int(hex_color[3:5], 16)},{int(hex_color[5:7], 16)},{alpha})"
LABELS = {"rce": "RCE", "sdac": "SDAC", "imb": "Imbalance price CEN", "pv": "PV", "wind": "Wind",
          "other": "Thermal & other", "charge": "Charge", "discharge": "Discharge"}


def _axes(fig: go.Figure) -> go.Figure:
    fig.update_xaxes(showgrid=False, linecolor=C["axis"], tickfont=dict(color=C["muted"]), ticks="", zeroline=False)
    fig.update_yaxes(gridcolor=C["grid"], linecolor=C["axis"], tickfont=dict(color=C["muted"]), zeroline=True,
                     zerolinecolor=C["axis"], zerolinewidth=1, title_font=dict(color=C["ink2"], size=12))
    return fig


def base(fig: go.Figure, height: int = 380, hovermode: str = "x unified", legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, font=dict(family=FONT, size=12, color=C["ink2"]),
        paper_bgcolor=C["surface"], plot_bgcolor=C["surface"],
        margin=dict(l=60, r=16, t=36 if legend else 12, b=36),
        hovermode=hovermode, hoverlabel=dict(bgcolor=C["surface"], bordercolor=C["axis"], font=dict(color=C["ink"])),
        showlegend=legend,
        legend=dict(orientation="h", x=0, y=1.0, yanchor="bottom", font=dict(color=C["ink2"]), bgcolor="rgba(0,0,0,0)"),
        barcornerradius=4,
    )
    return _axes(fig)


def _local(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Naive local wall-clock time for plotting (Plotly ignores time zones)."""
    return idx.tz_convert(TZ).tz_localize(None)


def _step(name: str, x, y, color: str, width: float = 2):
    """15-minute price as a step line (value holds over the interval that starts at x)."""
    return go.Scatter(x=x, y=y, name=name, mode="lines", line=dict(color=color, width=width, shape="hv"),
                      hovertemplate=f"%{{y:,.0f}} PLN/MWh<extra>{name}</extra>")


def today_tomorrow(prices: dict[str, pd.DataFrame], mix: pd.DataFrame, fcst: pd.DataFrame, today: date,
                   now: pd.Timestamp) -> go.Figure:
    """Prices for today and tomorrow (top) with actual and forecast PV / wind output (bottom)."""
    start = pd.Timestamp(today, tz=TZ)
    end = pd.Timestamp(today + timedelta(days=2), tz=TZ)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.6, 0.4])
    for key in ("rce", "sdac"):
        df = prices[key]
        df = df[(df.index >= start) & (df.index < end)]
        fig.add_trace(_step(LABELS[key], _local(df.index), df["price"], C[key]), row=1, col=1)
    actual = mix[(mix.index >= start) & (mix.index < end)]
    fc = fcst[(fcst.index >= start) & (fcst.index < end)]
    for key in ("pv", "wind"):
        fig.add_trace(go.Scatter(x=_local(actual.index), y=actual[key] / 1000, name=f"{LABELS[key]} actual",
                                 mode="lines", line=dict(color=C[key], width=2), legendgroup=key,
                                 hovertemplate=f"%{{y:.1f}} GW<extra>{LABELS[key]} actual</extra>"), row=2, col=1)
        fig.add_trace(go.Scatter(x=_local(fc.index), y=fc[key] / 1000, name=f"{LABELS[key]} forecast",
                                 mode="lines", line=dict(color=C[key], width=1.5, dash="dot"), legendgroup=key,
                                 hovertemplate=f"%{{y:.1f}} GW<extra>{LABELS[key]} forecast</extra>"), row=2, col=1)
    base(fig, height=470)
    midnight = (start + pd.Timedelta(days=1)).tz_localize(None)
    for x in (midnight,):
        fig.add_vline(x=x, line=dict(color=C["axis"], width=1))
    now_local = now.tz_convert(TZ).tz_localize(None)
    if start.tz_localize(None) <= now_local < end.tz_localize(None):
        fig.add_vline(x=now_local, line=dict(color=C["ink2"], width=1, dash="dot"))
        # label between the two rows, so it never collides with the day labels above the chart
        fig.add_annotation(x=now_local, xref="x2", y=1, yref="y2 domain", text="now", showarrow=False,
                           yanchor="bottom", font=dict(color=C["ink2"], size=11))
    for day, x0 in ((today, start), (today + timedelta(days=1), start + pd.Timedelta(days=1))):
        fig.add_annotation(x=(x0 + pd.Timedelta(minutes=20)).tz_localize(None), xanchor="left", y=1, yref="paper",
                           yanchor="bottom", text=f"<b>{'Today' if day == today else 'Tomorrow'}</b> · {day:%a %d %b}",
                           showarrow=False, font=dict(color=C["ink"], size=12))
    fig.update_layout(margin=dict(t=64), legend=dict(y=1.08))
    fig.update_yaxes(title_text="PLN/MWh", row=1, col=1)
    fig.update_yaxes(title_text="GW", row=2, col=1, rangemode="tozero")
    fig.update_xaxes(range=[start.tz_localize(None), end.tz_localize(None)], tickformat="%H:%M\n%a %d %b")
    return fig


def bess_day(schedule: pd.DataFrame) -> go.Figure:
    """Optimal dispatch of the 1 MW / 2 MWh battery for one day: price (top) and charge/discharge power (bottom)."""
    x = _local(schedule.index)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.55, 0.45])
    fig.add_trace(_step(LABELS["rce"], x, schedule["price"], C["rce"]), row=1, col=1)
    fig.add_trace(go.Bar(x=x, y=-schedule["charge_mw"], name=LABELS["charge"], marker_color=C["charge"],
                         hovertemplate="%{y:.2f} MW<extra>Charge</extra>"), row=2, col=1)
    fig.add_trace(go.Bar(x=x, y=schedule["discharge_mw"], name=LABELS["discharge"], marker_color=C["discharge"],
                         hovertemplate="%{y:.2f} MW<extra>Discharge</extra>"), row=2, col=1)
    base(fig, height=400)
    fig.update_layout(barmode="relative", bargap=0.15, barcornerradius=2)
    fig.update_yaxes(title_text="PLN/MWh", row=1, col=1)
    fig.update_yaxes(title_text="MW", row=2, col=1, range=[-1.1, 1.1], tickvals=[-1, 0, 1])
    fig.update_xaxes(tickformat="%H:%M")
    return fig


def _rolling_pair(series: pd.Series, name: str, color: str, unit: str, window: int = 30) -> list[go.Scatter]:
    roll = series.rolling(window, min_periods=window // 2).mean()
    return [
        go.Scatter(x=series.index, y=series, name="Daily", mode="lines", line=dict(color=rgba(color, 0.4), width=1),
                   hovertemplate=f"%{{y:,.0f}} {unit}<extra>Daily</extra>"),
        go.Scatter(x=roll.index, y=roll, name=f"{window}-day mean", mode="lines", line=dict(color=color, width=2),
                   hovertemplate=f"%{{y:,.0f}} {unit}<extra>{window}-day mean</extra>"),
    ]


def bess_history(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure(_rolling_pair(daily["revenue_pln"], "BESS", C["discharge"], "PLN"))
    base(fig, height=330)
    fig.update_yaxes(title_text="PLN per day")
    return fig


def spreads(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure(_rolling_pair(daily["spread"], "spread", C["rce"], "PLN/MWh"))
    base(fig, height=330)
    fig.update_yaxes(title_text="PLN/MWh (max − min)")
    return fig


def negative_hours(monthly: pd.DataFrame, current: pd.Period | None = None) -> go.Figure:
    x = monthly.index.to_timestamp()
    y = monthly["negative_h"]
    partial = monthly.index == current
    days = np.where(partial, monthly["days"].astype(str) + " days so far", monthly["days"].astype(str) + " days")
    fig = go.Figure(go.Bar(x=x, y=y, marker_color=C["rce"], marker_opacity=np.where(partial, 0.45, 1.0).tolist(),
                           name="Negative-price hours", customdata=days, width=24 * 86400000 * 0.8,
                           hovertemplate="%{x|%b %Y}: %{y:.0f} h<extra>%{customdata}</extra>"))
    base(fig, height=330, hovermode="closest", legend=False)
    if len(y) and y.max() > 0:   # label only the record month
        fig.add_annotation(x=y.idxmax().to_timestamp(), y=y.max(), text=f"{y.max():.0f} h", showarrow=False,
                           yanchor="bottom", yshift=2, font=dict(color=C["ink2"], size=11))
    fig.update_yaxes(title_text="hours with price < 0")
    fig.update_xaxes(tickformat="%b\n%Y", dtick="M3")
    return fig


def heatmap(profile: pd.DataFrame) -> go.Figure:
    x = [p.to_timestamp() for p in profile.columns]
    fig = go.Figure(go.Heatmap(
        z=profile.to_numpy(), x=x, y=profile.index, coloraxis="coloraxis", xgap=1, ygap=1,
        hovertemplate="%{x|%b %Y}, %{y}:00-%{y}:59<br>%{z:,.0f} PLN/MWh<extra></extra>"))
    base(fig, height=430, hovermode="closest", legend=False)
    lo, hi = np.nanpercentile(profile.to_numpy(), [2, 98]) if profile.size else (0, 1)
    scale = [[i / (len(RAMP) - 1), c] for i, c in enumerate(RAMP)]
    fig.update_layout(
        coloraxis=dict(colorscale=scale, cmin=float(lo), cmax=float(hi),
                       colorbar=dict(title=dict(text="PLN/MWh", font=dict(color=C["ink2"])), thickness=12,
                                     tickfont=dict(color=C["muted"]), outlinewidth=0)),
        meta={"dark": {"coloraxis.colorscale": [[i / (len(RAMP) - 1), c] for i, c in enumerate(RAMP[::-1])]}},
    )
    fig.update_yaxes(title_text="hour of day (local)", autorange="reversed", dtick=3, showgrid=False, zeroline=False)
    fig.update_xaxes(tickformat="%b\n%Y", dtick="M3")
    return fig


def mix_week(mixp: pd.DataFrame) -> go.Figure:
    """Stacked generation (thermal & other, wind, PV) with demand (top) and price (bottom)."""
    x = _local(mixp.index)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.62, 0.38])
    for key in ("other", "wind", "pv"):
        fig.add_trace(go.Scatter(x=x, y=mixp[key], name=LABELS[key], mode="lines", stackgroup="gen",
                                 line=dict(color=C[key], width=1), fillcolor=rgba(C[key], 0.75),
                                 hovertemplate=f"%{{y:.1f}} GW<extra>{LABELS[key]}</extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=mixp["demand"], name="Demand", mode="lines", line=dict(color=C["ink"], width=1.5),
                             hovertemplate="%{y:.1f} GW<extra>Demand</extra>"), row=1, col=1)
    fig.add_trace(_step(LABELS["rce"], x, mixp["price"], C["rce"]), row=2, col=1)
    base(fig, height=500)
    fig.update_yaxes(title_text="GW", row=1, col=1, rangemode="tozero")
    fig.update_yaxes(title_text="PLN/MWh", row=2, col=1)
    fig.update_xaxes(tickformat="%a %d %b")
    return fig


def pv_price(bins: pd.DataFrame, hours: tuple[int, int], series: str = "rce") -> go.Figure:
    """Midday price distribution by PV output: median with interquartile band (top), share of negative prices (bottom)."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.62, 0.38])
    x = bins["pv_gw"]
    fig.add_trace(go.Scatter(x=x, y=bins["p75"], mode="lines", line=dict(width=0), showlegend=False,
                             hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=bins["p25"], mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=rgba(C["rce"], 0.15), name="Interquartile range", hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=bins["median"], mode="lines+markers", name=f"Median {LABELS[series]}",
                             line=dict(color=C["rce"], width=2), marker=dict(size=8, line=dict(color=C["surface"], width=2)),
                             customdata=np.c_[bins["p25"], bins["p75"], bins["n"]],
                             hovertemplate="median %{y:,.0f} PLN/MWh<br>IQR %{customdata[0]:,.0f} to %{customdata[1]:,.0f}"
                                           "<br>%{customdata[2]:,} quarter-hours<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=x, y=bins["neg_share"] * 100, name="Share of negative prices", marker_color=C["rce"],
                         width=0.6, showlegend=False, hovertemplate="%{y:.0f} % negative<extra></extra>"), row=2, col=1)
    base(fig, height=460, hovermode="x unified")
    fig.update_yaxes(title_text="PLN/MWh", row=1, col=1)
    fig.update_yaxes(title_text="% negative", row=2, col=1, rangemode="tozero")
    fig.update_xaxes(title_text=f"PV output, GW (quarter-hours {hours[0]}:00-{hours[1]}:00 local)", row=2, col=1)
    return fig


def imbalance(imb: pd.DataFrame, ref: pd.DataFrame, series: str = "rce") -> go.Figure:
    fig = go.Figure()
    fig.add_trace(_step(LABELS[series], _local(ref.index), ref["price"], C[series], width=1.5))
    fig.add_trace(_step(LABELS["imb"], _local(imb.index), imb["price"], C["imb"], width=1.5))
    base(fig, height=360)
    fig.update_yaxes(title_text="PLN/MWh")
    fig.update_xaxes(tickformat="%a %d %b")
    return fig
