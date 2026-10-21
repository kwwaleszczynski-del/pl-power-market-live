"""Synthetic 15-minute data in the shape returned by ``pse_api.load_series`` (no network)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from pl_power_market.metrics import TZ
from pl_power_market.pse_api import SeriesStatus


def quarter_hours(day: date) -> pd.DatetimeIndex:
    """Interval starts (UTC) of one local delivery day: 92, 96 or 100 quarter-hours."""
    start, end = pd.Timestamp(day, tz=TZ), pd.Timestamp(day + timedelta(days=1), tz=TZ)
    return pd.date_range(start, end, freq="15min", inclusive="left").tz_convert("UTC")


def make_frame(days: list[date], **columns) -> pd.DataFrame:
    """Columns are callables of the local hour (float array) and the day, or constants."""
    frames = []
    for day in days:
        idx = quarter_hours(day)
        local = idx.tz_convert(TZ)
        hour = np.asarray(local.hour + local.minute / 60, dtype=float)
        data = {name: (f(hour, day) if callable(f) else np.full(len(idx), f, dtype=float)) for name, f in columns.items()}
        df = pd.DataFrame(data, index=pd.DatetimeIndex(idx, name="ts_utc"))
        df["business_date"] = day
        frames.append(df)
    return pd.concat(frames)


def pv_shape(hour):
    return np.clip(np.sin((hour - 6) / 13 * np.pi), 0, None)


def duck_price(hour, day):
    return 450 + 350 * np.exp(-((hour - 19) ** 2) / 4) + 150 * np.exp(-((hour - 8) ** 2) / 3) - 520 * pv_shape(hour)


@pytest.fixture
def synthetic_loaded():
    """Everything the dashboard needs: 60 days of history, today and tomorrow."""
    today = date(2026, 6, 10)
    hist = [today - timedelta(days=i) for i in range(60, -2, -1)]   # .. today, tomorrow
    rce = make_frame(hist, price=duck_price)
    sdac = make_frame(hist, price=lambda h, d: duck_price(h, d) + 10)
    past = [d for d in hist if d <= today]
    mix = make_frame(past, demand=lambda h, d: 18000 + 3000 * np.sin(h / 24 * np.pi),
                     pv=lambda h, d: 11000 * pv_shape(h), wind=3000.0, jg=lambda h, d: 12000 + 2000 * pv_shape(h),
                     jnwrb=lambda h, d: 8000 + 9000 * pv_shape(h), exch_par=-500.0, exch_nonpar=100.0)
    fcst = make_frame([today, today + timedelta(days=1)], demand=19000.0, pv=lambda h, d: 10500 * pv_shape(h), wind=3500.0)
    imb = make_frame([d for d in hist if d < today], price=lambda h, d: duck_price(h, d) * 1.2 - 50)
    data = {"rce": rce, "sdac": sdac, "mix": mix, "fcst": fcst, "imb": imb}
    return today, {k: (v, SeriesStatus(k, fetched=["2026-06"])) for k, v in data.items()}
