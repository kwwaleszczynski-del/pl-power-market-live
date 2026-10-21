"""Market statistics behind the dashboard panels.

All functions take 15-minute frames indexed by interval start (UTC) with a ``business_date`` column, as returned
by ``pse_api.load_series``. Hours and months are local (Europe/Warsaw), so DST days have 92 or 100 intervals.

Usage:
    from pl_power_market import metrics
    daily = metrics.daily_prices(rce)
    neg = metrics.negative_hours_monthly(rce)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TZ = "Europe/Warsaw"
DT_H = 0.25           # interval length in hours
MIN_INTERVALS = 92    # shortest complete day (spring DST change)


def local_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index.tz_convert(TZ)


def daily_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Per delivery day: mean, min, max, max-min spread, 2 h spread and negative-price hours (complete days only)."""
    def two_hour_spread(p: pd.Series) -> float:
        # mean of the 8 most expensive minus the 8 cheapest quarter-hours: what a 2 h battery roughly captures
        s = np.sort(p.to_numpy())
        return float(s[-8:].mean() - s[:8].mean())

    g = prices.groupby("business_date")["price"]
    daily = pd.DataFrame({
        "mean": g.mean(), "min": g.min(), "max": g.max(),
        "n": g.count(),
        "neg_hours": g.apply(lambda p: float((p < 0).sum() * DT_H)),
        "spread_2h": g.apply(two_hour_spread),
    })
    daily["spread"] = daily["max"] - daily["min"]
    daily = daily[daily["n"] >= MIN_INTERVALS]
    daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index), name="date")
    return daily


def negative_hours_monthly(prices: pd.DataFrame) -> pd.DataFrame:
    """Hours with a negative (and zero-or-negative) price per calendar month."""
    df = prices.assign(month=pd.to_datetime(prices["business_date"]).dt.to_period("M").to_numpy())
    g = df.groupby("month")
    return pd.DataFrame({
        "negative_h": g["price"].apply(lambda p: float((p < 0).sum() * DT_H)),
        "nonpositive_h": g["price"].apply(lambda p: float((p <= 0).sum() * DT_H)),
        "days": g["business_date"].nunique(),
    })


def hour_month_profile(prices: pd.DataFrame) -> pd.DataFrame:
    """Mean price by local hour of day (rows 0-23) and calendar month (columns)."""
    local = local_index(prices)
    df = pd.DataFrame({"price": prices["price"].to_numpy(), "hour": local.hour,
                       "month": pd.to_datetime(prices["business_date"]).dt.to_period("M").to_numpy()})
    return df.pivot_table(index="hour", columns="month", values="price", aggfunc="mean")


def mix_with_price(mix: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Generation mix (GW) joined with the price of the same interval.

    ``his-wlk-cal`` reports wind and PV in total, while their units appear partly in ``jg`` (units active in the
    balancing market) and partly in ``jnwrb`` (units outside it). Thermal and other generation is therefore
    ``jg + jnwrb - wind - pv``; net import is the sum of both cross-border exchange balances (+ = import).
    """
    out = pd.DataFrame(index=mix.index)
    out["pv"] = mix["pv"] / 1000
    out["wind"] = mix["wind"] / 1000
    out["other"] = ((mix["jg"] + mix["jnwrb"] - mix["wind"] - mix["pv"]) / 1000).clip(lower=0)
    out["net_import"] = (mix["exch_par"].fillna(0) + mix["exch_nonpar"].fillna(0)) / 1000
    out["demand"] = mix["demand"] / 1000
    out["residual"] = out["demand"] - out["pv"] - out["wind"]
    out["business_date"] = mix["business_date"]
    out["price"] = prices["price"].reindex(out.index)
    return out


def pv_price_bins(mixp: pd.DataFrame, hours: tuple[int, int] = (10, 15), bin_gw: float = 1.0,
                  min_count: int = 40) -> pd.DataFrame:
    """Price distribution in midday intervals (local hours [start, end)) grouped by PV output bins of ``bin_gw`` GW."""
    local = local_index(mixp)
    df = mixp[(local.hour >= hours[0]) & (local.hour < hours[1])].dropna(subset=["pv", "price"])
    if df.empty:
        return pd.DataFrame(columns=["pv_gw", "n", "p25", "median", "p75", "neg_share"])
    bins = np.floor(df["pv"] / bin_gw) * bin_gw
    g = df.groupby(bins.to_numpy())["price"]
    out = pd.DataFrame({
        "n": g.count(), "p25": g.quantile(0.25), "median": g.median(), "p75": g.quantile(0.75),
        "neg_share": g.apply(lambda p: float((p < 0).mean())),
    })
    out = out[out["n"] >= min_count]
    out.insert(0, "pv_gw", out.index + bin_gw / 2)
    return out.reset_index(drop=True)


def day_frame(df: pd.DataFrame, day) -> pd.DataFrame:
    """Rows of one business date (empty frame if absent)."""
    return df[df["business_date"] == day]
