"""Daily price-arbitrage potential of a 1 MW / 2 MWh battery.

The dispatch itself comes from the companion project bess-arbitrage-pl (one MILP per delivery day, perfect
foresight of day-ahead prices, HiGHS via SciPy). This module applies it to the dashboard's price history and
caches daily results keyed by a hash of each day's prices, so a daily build only solves new or revised days.

Usage:
    from pl_power_market.arbitrage import BATTERY, arbitrage_daily, day_schedule
    daily = arbitrage_daily(rce, cache=Path("data/bess_daily.parquet"))
    schedule = day_schedule(rce, date(2026, 10, 22))
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

import pandas as pd
from bess_arbitrage import BatteryParams, optimize_day
from bess_arbitrage.backtest import run_backtest

from pl_power_market.metrics import MIN_INTERVALS

# 87 % round-trip efficiency, usable SoC 5-95 %, max 1.5 full cycles per day, 30 PLN per MWh discharged
BATTERY = BatteryParams(power_mw=1.0, energy_mwh=2.0)
COLUMNS = ["revenue_pln", "gross_revenue_pln", "discharged_mwh", "cycles", "avg_buy_price", "avg_sell_price", "key"]
log = logging.getLogger(__name__)


def _day_key(p: pd.Series, params: BatteryParams) -> str:
    return hashlib.sha1(p.to_numpy(dtype="float64").tobytes() + repr(params).encode()).hexdigest()[:16]


def arbitrage_daily(prices: pd.DataFrame, params: BatteryParams = BATTERY, cache: Path | None = None) -> pd.DataFrame:
    """Optimal daily revenue (PLN, net of degradation cost) for every complete day of ``prices``."""
    complete = {day: grp["price"] for day, grp in prices.groupby("business_date")
                if len(grp) >= MIN_INTERVALS and not grp["price"].isna().any()}
    keys = {day: _day_key(p, params) for day, p in complete.items()}
    cached = pd.read_parquet(cache) if cache is not None and cache.exists() else pd.DataFrame(columns=COLUMNS)
    # fixed resolution: Parquet round trips and pandas 2 vs 3 would otherwise give s / ms / ns indexes
    cached.index = pd.DatetimeIndex(pd.to_datetime(cached.index), name="date").as_unit("ns")
    hit = [day for day in complete if pd.Timestamp(day) in cached.index and cached.at[pd.Timestamp(day), "key"] == keys[day]]
    todo = sorted(set(complete) - set(hit))
    frames = [cached.loc[[pd.Timestamp(d) for d in hit]]] if hit else []
    if todo:
        new = run_backtest(prices[prices["business_date"].isin(todo)][["price", "business_date"]], params)
        new.index = pd.DatetimeIndex(pd.to_datetime(new.index), name="date").as_unit("ns")
        new["key"] = [keys[d.date()] for d in new.index]
        frames.append(new[COLUMNS])
        log.info("battery dispatch: %d days solved, %d from cache", len(todo), len(hit))
    out = pd.concat(frames).sort_index() if frames else pd.DataFrame(columns=COLUMNS)
    if cache is not None and todo:
        cache.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(cache)
    return out


def day_schedule(prices: pd.DataFrame, day: date, params: BatteryParams = BATTERY) -> pd.DataFrame | None:
    """Charge / discharge schedule for one complete day (None if the day is incomplete)."""
    p = prices.loc[prices["business_date"] == day, "price"]
    if len(p) < MIN_INTERVALS or p.isna().any():
        return None
    return optimize_day(p, params).schedule
