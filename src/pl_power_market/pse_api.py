"""PSE series of the dashboard, read with the pse-opendata client (https://pypi.org/project/pse-opendata/).

Series (15-minute intervals, business dates in Europe/Warsaw):
    rce   rce-pln       RCE, market price of electricity, PLN/MWh
    sdac  csdac-pln     price from the single day-ahead coupling (SDAC), PLN/MWh
    mix   his-wlk-cal   actual demand, wind, PV, generation and cross-border exchange, MW (near real time)
    fcst  pdgopkd       day-ahead coordinated plan: forecast demand, wind and PV, MW (published the day before)
    imb   crb-rozl      imbalance settlement price CEN, PLN/MWh (published D+1, corrected later)

pse-opendata downloads the public PSE reports API (no key) and caches whole months as Parquet files in ``cache_dir``
(``api/<report>/<YYYY-MM>.parquet``); a month is downloaded again until the revision window of its report has
passed. Frames returned here are indexed by interval START (UTC).

Usage:
    from pl_power_market.pse_api import load_series
    df, status = load_series("rce", date(2024, 6, 14), date(2026, 10, 22), cache_dir=Path("data"))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import pse_opendata as pse

INTERVAL = pd.Timedelta("15min")
# resolution pandas gives time stamps parsed from text (ns in pandas 2, us in pandas 3); the frames keep it, so the
# page serialises times as before
TIME_UNIT = pd.to_datetime(pd.Series(["2024-06-14 00:15:00"]), format="ISO8601").dt.unit
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Series:
    endpoint: str
    fields: dict[str, str]   # API field -> column name
    label: str


SERIES = {
    "rce": Series("rce-pln", {"rce_pln": "price"}, "RCE price"),
    "sdac": Series("csdac-pln", {"csdac_pln": "price"}, "SDAC price"),
    "mix": Series("his-wlk-cal", {"demand": "demand", "pv": "pv", "wi": "wind", "jg": "jg", "jnwrb": "jnwrb",
                                  "swm_p": "exch_par", "swm_np": "exch_nonpar"}, "Generation mix (actual)"),
    "fcst": Series("pdgopkd", {"kse_pow_dem": "demand", "gen_fv": "pv", "gen_wi": "wind"}, "Day-ahead forecast"),
    "imb": Series("crb-rozl", {"cen_cost": "price"}, "Imbalance price CEN"),
}


@dataclass
class SeriesStatus:
    """What happened while loading one series; rendered on the dashboard as data freshness."""
    name: str
    fetched: list[str] = field(default_factory=list)   # months downloaded in this run
    cached: list[str] = field(default_factory=list)    # final months read from cache
    stale: list[str] = field(default_factory=list)     # download failed, older cached copy used
    missing: list[str] = field(default_factory=list)   # download failed and no cache
    last_interval: pd.Timestamp | None = None

    @property
    def ok(self) -> bool:
        return not self.stale and not self.missing


STATUS = {"downloaded": "fetched", "cached": "cached", "stale": "stale"}   # pse-opendata status -> SeriesStatus list


def empty_frame(series: str) -> pd.DataFrame:
    cols = list(SERIES[series].fields.values()) + ["business_date"]
    idx = pd.DatetimeIndex([], tz="UTC", name="ts_utc")
    return pd.DataFrame({c: pd.Series(dtype=object if c == "business_date" else float) for c in cols}, index=idx)


def to_frame(raw: pd.DataFrame, series: str) -> pd.DataFrame:
    """pse-opendata frame -> frame indexed by interval START (UTC) with the column names of ``SERIES``."""
    spec = SERIES[series]
    if raw.empty:
        return empty_frame(series)
    out = raw.set_index("start_utc")[list(spec.fields)].rename(columns=spec.fields)
    out.index = out.index.as_unit(TIME_UNIT).rename("ts_utc")
    out["business_date"] = raw["business_date"].to_numpy()
    return out


def load_series(series: str, start: date, end: date, cache_dir: Path, max_failures: int = 2, offline: bool = False,
                client: pse.Client | None = None) -> tuple[pd.DataFrame, SeriesStatus]:
    """Load business dates start..end month by month, never raising on API errors.

    Whole months are requested, so that pse-opendata caches them and an offline run rebuilds the same result.
    When a download fails, the cached copy of that month is used and the month is reported as stale. After
    ``max_failures`` failed months the API is assumed to be down and the remaining months come from cache only
    (reported as stale). Rows without any value (future intervals) are dropped. ``offline`` skips the API altogether.
    """
    spec = SERIES[series]
    client = client or pse.Client(cache_dir=cache_dir, offline=offline, allow_stale=True)
    status = SeriesStatus(series)
    frames, failures = [], 0
    for month_start in pd.date_range(date(start.year, start.month, 1), end, freq="MS"):
        label = f"{month_start:%Y-%m}"
        m_end = (month_start + pd.offsets.MonthEnd(0)).date()
        api_down = not offline and failures >= max_failures
        client.offline = offline or api_down
        loaded = len(client.loads)
        try:
            raw = client.get(spec.endpoint, month_start.date(), m_end, fields=list(spec.fields), dropna=True)
        except pse.PseError as e:
            log.error("%s %s: download failed (%s)", series, label, e)
            failures += 1
            status.missing.append(label)
            continue
        for load in client.loads[loaded:]:
            stale = load.status == "stale" or api_down
            failures += load.status == "stale"
            getattr(status, "stale" if stale else STATUS[load.status]).append(label)
        log.info("%s %s: %d intervals", series, label, len(raw))
        frames.append(to_frame(raw, series))
    frames = [f for f in frames if not f.empty]
    out = pd.concat(frames).sort_index() if frames else empty_frame(series)
    out = out[~out.index.duplicated(keep="last")]
    out = out[(out["business_date"] >= start) & (out["business_date"] <= end)]
    status.last_interval = out.index.max() if len(out) else None
    return out, status
