"""Small 16:10 preview of the dashboard (thumb.webp), redrawn on every build.

The portfolio page and the profile README link to it, so their card always shows the latest prices instead of a
screenshot from the day the project was published. Inputs are the files the build already writes:
data/latest.json (KPIs) and data/today_tomorrow.csv (15-minute prices).
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from PIL import Image  # noqa: E402

log = logging.getLogger(__name__)

WIDTH, HEIGHT, DPI = 960, 600, 100
INK, MUTED, LINE, TOMORROW, TODAY = "#151a22", "#5a6474", "#e2e6ec", "#c2410c", "#0e7490"


def write_thumbnail(out_dir: Path, name: str = "thumb.webp") -> Path:
    """Draw the preview from the built site's data files and save it next to index.html."""
    data = out_dir / "data"
    latest = json.loads((data / "latest.json").read_text(encoding="utf-8"))
    col = f"{latest.get('price_series', 'rce')}_pln_mwh"
    frame = pd.read_csv(data / "today_tomorrow.csv", parse_dates=["ts_local"])
    prices = frame.dropna(subset=[col])
    focus = pd.Timestamp(latest["focus_day"])
    updated = pd.Timestamp(latest["generated_utc"]).tz_convert("Europe/Warsaw")

    fig = plt.figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor="white")
    fig.text(0.04, 0.93, "POLISH POWER MARKET · LIVE", fontsize=11, color=MUTED, weight="bold")
    fig.text(0.04, 0.85, "Prices today and tomorrow", fontsize=22, color=INK, weight="bold")
    fig.text(0.96, 0.93, f"Updated {updated:%d %b %Y, %H:%M} (Warsaw)", fontsize=11, color=MUTED, ha="right")

    for i, kpi in enumerate(latest["kpis"][:3]):        # tomorrow's average price, daily spread, battery potential
        x = 0.04 + i * 0.315
        fig.patches.append(plt.Rectangle((x, 0.62), 0.29, 0.18, transform=fig.transFigure, facecolor="#f6f7f9",
                                         edgecolor=LINE, linewidth=1))
        fig.text(x + 0.015, 0.73, kpi["value"], fontsize=20, color=INK, weight="bold")
        fig.text(x + 0.015, 0.655, kpi["label"], fontsize=8.5, color=MUTED)

    ax = fig.add_axes((0.08, 0.1, 0.88, 0.45))
    today_part = prices[prices["ts_local"] < focus]
    tomorrow_part = prices[prices["ts_local"] >= focus]
    ax.step(today_part["ts_local"], today_part[col], where="post", color=TODAY, linewidth=2, label="today")
    ax.step(tomorrow_part["ts_local"], tomorrow_part[col], where="post", color=TOMORROW, linewidth=2,
            label="tomorrow")
    ax.axvline(focus, color=MUTED, linewidth=1, linestyle=":")
    ax.axhline(0, color=LINE, linewidth=1)
    ax.set_ylabel("PLN/MWh", color=MUTED, fontsize=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M"))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18]))
    ax.tick_params(colors=MUTED, labelsize=9)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(LINE)
    ax.grid(axis="y", color=LINE, linewidth=0.8)
    ax.legend(loc="upper left", frameon=False, fontsize=10, ncol=2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor="white")
    plt.close(fig)
    path = out_dir / name
    Image.open(buf).convert("RGB").save(path, "WEBP", quality=85, method=6)
    log.info("thumbnail written to %s", path)
    return path
