"""Client for the public PSE reports API (https://api.raporty.pse.pl) with a fault-tolerant monthly Parquet cache.

Series (15-minute intervals, business dates in Europe/Warsaw):
    rce   rce-pln       RCE, market price of electricity, PLN/MWh
    sdac  csdac-pln     price from the single day-ahead coupling (SDAC), PLN/MWh
    mix   his-wlk-cal   actual demand, wind, PV, generation and cross-border exchange, MW (near real time)
    fcst  pdgopkd       day-ahead coordinated plan: forecast demand, wind and PV, MW (published the day before)
    imb   crb-rozl      imbalance settlement price CEN, PLN/MWh (published D+1, corrected later)

The API needs no key. ``dtime_utc`` is the END of each interval; frames returned here are indexed by interval START.

Usage:
    from pl_power_market.pse_api import load_series
    df, status = load_series("rce", date(2024, 6, 14), date(2026, 10, 22), cache_dir=Path("data"))
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://api.raporty.pse.pl/api"
INTERVAL = pd.Timedelta("15min")
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


def empty_frame(series: str) -> pd.DataFrame:
    cols = list(SERIES[series].fields.values()) + ["business_date"]
    idx = pd.DatetimeIndex([], tz="UTC", name="ts_utc")
    return pd.DataFrame({c: pd.Series(dtype=object if c == "business_date" else float) for c in cols}, index=idx)


def parse_records(records: list[dict], series: str) -> pd.DataFrame:
    """API records -> frame indexed by interval START (UTC). Rows without any value (future intervals) are dropped."""
    if not records:
        return empty_frame(series)
    spec = SERIES[series]
    df = pd.DataFrame.from_records(records)
    out = pd.DataFrame({col: pd.to_numeric(df[api], errors="coerce") if api in df else float("nan")
                        for api, col in spec.fields.items()})
    out.index = pd.DatetimeIndex(pd.to_datetime(df["dtime_utc"], format="ISO8601"), name="ts_utc").tz_localize("UTC") - INTERVAL
    out["business_date"] = pd.to_datetime(df["business_date"]).dt.date.to_numpy()
    values = list(spec.fields.values())
    out = out.dropna(subset=values, how="all")
    return out[~out.index.duplicated(keep="last")].sort_index()


def _get(session: requests.Session, url: str, params: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise
            log.warning("PSE API request failed (%s), retrying", e)
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def fetch_period(series: str, start: date, end: date, session: requests.Session | None = None) -> pd.DataFrame:
    """Fetch one series for business dates start..end (inclusive), following pagination links."""
    spec = SERIES[series]
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "pl-power-market-live (open-source dashboard)")
    params = {
        "$filter": f"business_date ge '{start:%Y-%m-%d}' and business_date le '{end:%Y-%m-%d}'",
        "$select": ",".join(["dtime_utc", "business_date", *spec.fields]),
        "$first": 20000,
    }
    payload = _get(session, f"{BASE_URL}/{spec.endpoint}", params)
    records = list(payload.get("value", []))
    while payload.get("nextLink"):
        payload = _get(session, payload["nextLink"])
        records += payload.get("value", [])
    return parse_records(records, series)


def load_series(series: str, start: date, end: date, cache_dir: Path, today: date, refresh_days: int = 10,
                session: requests.Session | None = None, max_failures: int = 2,
                offline: bool = False) -> tuple[pd.DataFrame, SeriesStatus]:
    """Load business dates start..end month by month, never raising on API errors.

    Months that ended more than ``refresh_days`` before ``today`` are final and read from cache when present;
    recent months are downloaded again (late publications and corrections). When a download fails, the last cached
    copy of that month is used and the month is reported as stale. After ``max_failures`` failed months the API is
    assumed to be down and the remaining months come from cache only. ``offline`` skips the API altogether.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()
    status = SeriesStatus(series)
    frames, failures = [], 0
    for month_start in pd.date_range(date(start.year, start.month, 1), end, freq="MS"):
        label = f"{month_start:%Y-%m}"
        m_start = month_start.date()
        m_end = (month_start + pd.offsets.MonthEnd(0)).date()
        cache = cache_dir / f"{series}_{label}.parquet"
        final = m_end < today - timedelta(days=refresh_days)
        cached = pd.read_parquet(cache) if cache.exists() else None
        # a final month is trusted only if the cached copy reaches its last day (it may be from a mid-month run)
        if (offline and cached is not None) or (final and cached is not None and len(cached)
                                                and cached["business_date"].max() >= m_end):
            frames.append(cached)
            status.cached.append(label)
            continue
        if offline or failures >= max_failures:
            df = None
        else:
            try:
                df = fetch_period(series, m_start, min(m_end, end), session)
            except (requests.RequestException, ValueError, KeyError) as e:
                log.error("%s %s: download failed (%s)", series, label, e)
                failures += 1
                df = None
        if df is not None:
            if not df.empty:
                df.to_parquet(cache)
            frames.append(df)
            status.fetched.append(label)
            log.info("%s %s: %d intervals", series, label, len(df))
        elif cached is not None:
            frames.append(cached)
            status.stale.append(label)
        else:
            status.missing.append(label)
    frames = [f for f in frames if not f.empty]
    out = pd.concat(frames).sort_index() if frames else empty_frame(series)
    out = out[~out.index.duplicated(keep="last")]
    out = out[(out["business_date"] >= start) & (out["business_date"] <= end)]
    status.last_interval = out.index.max() if len(out) else None
    return out, status
