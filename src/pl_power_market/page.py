"""Assemble the static dashboard: KPI tiles, Plotly panels, methodology and data status, plus CSV/JSON downloads.

Every panel is built independently; if one fails (missing series, unexpected data) it is replaced by a short note
and the rest of the page is still published.

Usage:
    from pl_power_market.page import build_site
    build_site(loaded, today=date(2026, 10, 21), now=pd.Timestamp.now(tz="UTC"), out_dir=Path("site"))
"""
from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from string import Template

import numpy as np
import pandas as pd
import plotly.io as pio
from plotly.offline import get_plotlyjs

from pl_power_market import figures, metrics
from pl_power_market.arbitrage import BATTERY, arbitrage_daily, day_schedule
from pl_power_market.metrics import TZ
from pl_power_market.pse_api import SERIES, SeriesStatus

log = logging.getLogger(__name__)
PV_HOURS = (10, 15)


@dataclass
class Panel:
    key: str
    title: str
    subtitle: str = ""
    fig: object = None
    note: str = ""
    csv: str | None = None
    wide: bool = False
    error: str | None = None


def _fmt(x: float | None, unit: str = "", digits: int = 0) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    s = f"{x:,.{digits}f}".replace(",", " ")   # narrow no-break space as thousands separator
    return f"{s} {unit}" if unit else s


def _day_label(day: date, today: date) -> str:
    rel = {today: "today", today + timedelta(days=1): "tomorrow", today - timedelta(days=1): "yesterday"}.get(day)
    if rel:
        return f"{rel}, {day:%a %d %b}"
    return f"{day:%a %d %b}" if day.year == today.year else f"{day:%a %d %b %Y}"


class Dashboard:
    """Computes everything shown on the page from the loaded series."""

    def __init__(self, loaded: dict, today: date, now: pd.Timestamp, price_series: str, cache_dir: Path | None):
        self.today, self.now, self.price_series = today, now, price_series
        self.data = {k: v[0] for k, v in loaded.items()}
        self.status: dict[str, SeriesStatus] = {k: v[1] for k, v in loaded.items()}
        self.prices = self.data[price_series]
        self.daily = metrics.daily_prices(self.prices)
        try:
            self.arb = arbitrage_daily(self.prices, cache=cache_dir / f"bess_daily_{price_series}.parquet" if cache_dir else None)
        except Exception:   # noqa: BLE001 - battery panels degrade to notes, the market panels still render
            log.exception("battery dispatch failed")
            self.arb = pd.DataFrame(columns=["revenue_pln", "cycles", "avg_buy_price", "avg_sell_price"])
        tomorrow = today + timedelta(days=1)
        complete = set(self.daily.index.date)
        # the "focus" day is the next delivery day with published prices
        self.focus = tomorrow if tomorrow in complete else (today if today in complete or not complete else max(complete))
        self.tomorrow_published = tomorrow in complete
        self.panels: list[Panel] = []
        self.kpis: list[dict] = []
        self.csvs: dict[str, pd.DataFrame] = {}

    # ---------- KPI tiles ----------
    def build_kpis(self):
        d, f, today = self.daily, pd.Timestamp(self.focus), self.today
        prev = f - pd.Timedelta(days=1)
        label = _day_label(self.focus, today)
        tiles = self.kpis   # appended in place: tiles built before an error are kept
        if f in d.index:
            row = d.loc[f]
            delta = (row["mean"] / d.loc[prev, "mean"] - 1) * 100 if prev in d.index else None
            tiles.append(dict(label=f"Average price, {label}", value=_fmt(row["mean"], "PLN/MWh"),
                              sub=(f"{'▲' if delta >= 0 else '▼'} {abs(delta):.0f}% vs previous day" if delta is not None else "")))
            tiles.append(dict(label=f"Daily spread, {label}", value=_fmt(row["spread"], "PLN/MWh"),
                              sub=f"min {_fmt(row['min'])} · max {_fmt(row['max'])}"))
        if f in self.arb.index:
            rev = self.arb.loc[f, "revenue_pln"]
            last30 = self.arb.loc[(self.arb.index < f) & (self.arb.index >= f - pd.Timedelta(days=30)), "revenue_pln"]
            tiles.append(dict(label=f"Battery 1 MW / 2 h, {label}", value=_fmt(rev, "PLN"),
                              sub=f"previous 30 days: {_fmt(last30.mean(), 'PLN/day')} on average"))
        neg = metrics.negative_hours_monthly(self.prices)
        cur = pd.Period(today, "M")
        if cur in neg.index:
            ly = cur - 12
            sub = f"{ly.strftime('%b %Y')}: {_fmt(neg.loc[ly, 'negative_h'])} h" if ly in neg.index else ""
            tiles.append(dict(label=f"Negative-price hours, {cur.strftime('%B')} to date", value=_fmt(neg.loc[cur, "negative_h"], "h"),
                              sub=sub))
        fcst = self.data["fcst"]
        # tomorrow's plan comes about two hours after tomorrow's prices; fall back to the latest planned day
        fc_day = max((d for d in fcst["business_date"].unique() if d <= self.focus), default=None)
        fc = metrics.day_frame(fcst, fc_day)
        if len(fc):
            tiles.append(dict(label=f"PV peak forecast, {_day_label(fc_day, today)}", value=_fmt(fc["pv"].max() / 1000, "GW", 1),
                              sub=f"wind peak {_fmt(fc['wind'].max() / 1000, 'GW', 1)}"))
        imb = self.data["imb"]
        if len(imb):
            last = imb["business_date"].max()
            day = metrics.day_frame(imb, last)
            if len(day) >= metrics.MIN_INTERVALS:
                ref = self.prices["price"].reindex(day.index)
                gap = (day["price"] - ref).abs().mean()
                tiles.append(dict(label=f"Imbalance price CEN, {_day_label(last, today)}", value=_fmt(day["price"].mean(), "PLN/MWh"),
                                  sub=f"mean |CEN − {self.price_series.upper()}| {_fmt(gap)} PLN/MWh"))

    # ---------- panels ----------
    def _panel(self, key: str, title: str, builder, wide: bool = False):
        try:
            p = builder()
            p.key, p.title, p.wide = key, title, wide
        except Exception as e:   # noqa: BLE001 - one broken panel must not take the page down
            log.exception("panel %s failed", key)
            p = Panel(key, title, wide=wide, error=f"{type(e).__name__}: {e}")
        self.panels.append(p)

    def p_today(self) -> Panel:
        prices = {"rce": self.data["rce"], "sdac": self.data["sdac"]}
        fig = figures.today_tomorrow(prices, self.data["mix"], self.data["fcst"], self.today, self.now)
        day = self.focus
        p = metrics.day_frame(self.prices, day)["price"]
        loc = p.index.tz_convert(TZ)
        sub = (f"{self.price_series.upper()} {_day_label(day, self.today)}: from {_fmt(p.min())} PLN/MWh at "
               f"{loc[p.argmin()]:%H:%M} to {_fmt(p.max())} PLN/MWh at {loc[p.argmax()]:%H:%M}.")
        if not self.tomorrow_published:
            sub += " Tomorrow's prices are not published yet (PSE publishes them in the early afternoon)."
        start, end = pd.Timestamp(self.today, tz=TZ), pd.Timestamp(self.today + timedelta(days=2), tz=TZ)
        idx = pd.date_range(start, end, freq="15min", inclusive="left").tz_convert("UTC")
        frame = pd.DataFrame({
            "ts_local": idx.tz_convert(TZ).strftime("%Y-%m-%d %H:%M"),
            "rce_pln_mwh": self.data["rce"]["price"].reindex(idx).to_numpy(),
            "sdac_pln_mwh": self.data["sdac"]["price"].reindex(idx).to_numpy(),
            "pv_actual_mw": self.data["mix"]["pv"].reindex(idx).to_numpy(),
            "wind_actual_mw": self.data["mix"]["wind"].reindex(idx).to_numpy(),
            "pv_forecast_mw": self.data["fcst"]["pv"].reindex(idx).to_numpy(),
            "wind_forecast_mw": self.data["fcst"]["wind"].reindex(idx).to_numpy(),
        })
        self.csvs["today_tomorrow.csv"] = frame
        note = "Solid lines: actual output so far today (his-wlk-cal). Dotted: PSE day-ahead plan (pdgopkd)."
        if self.tomorrow_published and not len(metrics.day_frame(self.data["fcst"], self.today + timedelta(days=1))):
            note += " The plan for tomorrow is published around 15:45 and appears in the next refresh."
        return Panel("", "", sub, fig, note, "today_tomorrow.csv")

    def p_bess_day(self) -> Panel:
        s = day_schedule(self.prices, self.focus)
        if s is None:
            raise ValueError(f"no complete price day for {self.focus}")
        res = self.arb.loc[pd.Timestamp(self.focus)]
        sub = (f"Optimal schedule for {_day_label(self.focus, self.today)}: buys at {_fmt(res['avg_buy_price'])} and sells at "
               f"{_fmt(res['avg_sell_price'])} PLN/MWh on average, {res['cycles']:.2f} cycles, "
               f"{_fmt(res['revenue_pln'])} PLN after losses and degradation cost.")
        return Panel("", "", sub, figures.bess_day(s), "Negative bars: charging. Positive bars: discharging.")

    def p_bess_history(self) -> Panel:
        a = self.arb
        last30 = a["revenue_pln"].iloc[-30:]
        prev_year = a.loc[(a.index > a.index[-1] - pd.Timedelta(days=395)) & (a.index <= a.index[-1] - pd.Timedelta(days=365)), "revenue_pln"]
        # the sum of the last 365 days, not 30 days times 365: spreads are seasonal
        last12 = a.loc[a.index > a.index[-1] - pd.Timedelta(days=365), "revenue_pln"]
        sub = (f"Last 30 days: {_fmt(last30.mean())} PLN/day on average"
               + (f" (same 30 days a year earlier: {_fmt(prev_year.mean())} PLN/day)" if len(prev_year) else "")
               + f". Last 12 months ({len(last12)} days): {_fmt(last12.sum() / 1000)} k PLN per MW in total.")
        return Panel("", "", sub, figures.bess_history(a), "", "daily.csv")

    def p_spreads(self) -> Panel:
        d = self.daily
        last30 = d["spread"].iloc[-30:].mean()
        ago = d.loc[(d.index > d.index[-1] - pd.Timedelta(days=395)) & (d.index <= d.index[-1] - pd.Timedelta(days=365)), "spread"]
        sub = f"30-day mean of the daily max − min spread: {_fmt(last30)} PLN/MWh"
        sub += f" (a year earlier: {_fmt(ago.mean())})." if len(ago) else "."
        return Panel("", "", sub, figures.spreads(d), "", "daily.csv")

    def p_negative(self) -> Panel:
        neg = metrics.negative_hours_monthly(self.prices)
        self.csvs["negative_hours_monthly.csv"] = neg.rename_axis("month").reset_index().astype({"month": str})
        years = neg["negative_h"].groupby(neg.index.year).sum()
        sub = " · ".join(f"{y}: {_fmt(v)} h" for y, v in years.items()) + " (first and current year partial)."
        return Panel("", "", sub, figures.negative_hours(neg, pd.Period(self.today, "M")), "", "negative_hours_monthly.csv")

    def p_heatmap(self) -> Panel:
        prof = metrics.hour_month_profile(self.prices)
        out = prof.copy()
        out.columns = [str(c) for c in out.columns]
        self.csvs["hour_month_profile.csv"] = out.reset_index()
        cur = prof.iloc[:, -1].dropna()
        sub = (f"{prof.columns[-1].strftime('%B %Y')}: cheapest hour on average {cur.idxmin()}:00 ({_fmt(cur.min())} PLN/MWh), "
               f"most expensive {cur.idxmax()}:00 ({_fmt(cur.max())} PLN/MWh).")
        return Panel("", "", sub, figures.heatmap(prof), "Colour scale clipped to the 2nd-98th percentile.",
                     "hour_month_profile.csv")

    def p_mix(self) -> Panel:
        mixp = metrics.mix_with_price(self.data["mix"], self.prices)
        start = pd.Timestamp(self.today - timedelta(days=7), tz=TZ)
        week = mixp[mixp.index >= start].dropna(subset=["pv", "wind", "other"])
        if week.empty:
            raise ValueError("no generation data for the last 7 days")
        gen = week[["pv", "wind", "other"]].sum()
        share = gen / gen.sum() * 100
        sub = (f"Last 7 days: PV {share['pv']:.0f}%, wind {share['wind']:.0f}%, thermal and other {share['other']:.0f}% of "
               f"domestic generation; the price fell to {_fmt(week['price'].min())} PLN/MWh at the lowest.")
        return Panel("", "", sub, figures.mix_week(week),
                     "Thermal & other = all generation minus wind and PV (coal, gas, biomass, hydro, storage discharge). "
                     "The gap to the demand line is net import.")

    def p_pv_price(self) -> Panel:
        mixp = metrics.mix_with_price(self.data["mix"], self.prices)
        bins = metrics.pv_price_bins(mixp, PV_HOURS)
        if bins.empty:
            raise ValueError("no PV generation data")
        self.csvs["pv_price_bins.csv"] = bins
        over = bins[bins["median"] < 0.5 * bins["median"].iloc[0]]
        sub = "Midday quarter-hours since June 2024, grouped by national PV output."
        if len(over):
            first = over.iloc[0]
            high = bins[bins["pv_gw"] >= first["pv_gw"]]
            sub = (f"Once PV exceeds about {first['pv_gw'] - 0.5:.0f} GW the median midday price is less than half of the "
                   f"low-PV level, and {np.average(high['neg_share'], weights=high['n']) * 100:.0f}% of those quarter-hours "
                   f"clear below zero.")
        return Panel("", "", sub, figures.pv_price(bins, PV_HOURS, self.price_series),
                     f"Quarter-hours between {PV_HOURS[0]}:00 and {PV_HOURS[1]}:00 local time; bins with at least 40 values.",
                     "pv_price_bins.csv")

    def p_imbalance(self) -> Panel:
        imb = self.data["imb"]
        if imb.empty:
            raise ValueError("no imbalance prices")
        start = pd.Timestamp(imb["business_date"].max() - timedelta(days=6), tz=TZ)
        imb = imb[imb.index >= start]
        ref = self.prices[(self.prices.index >= start) & (self.prices.index <= imb.index.max())]
        gap = (imb["price"] - ref["price"].reindex(imb.index)).abs().mean()
        sub = (f"Last 7 settled days: imbalance price CEN deviates from {self.price_series.upper()} by "
               f"{_fmt(gap)} PLN/MWh on average (absolute), ranging {_fmt(imb['price'].min())} to {_fmt(imb['price'].max())}.")
        return Panel("", "", sub, figures.imbalance(imb, ref, self.price_series),
                     "CEN is published for the previous day and may be corrected in later settlement runs.")

    def build(self):
        try:
            self.build_kpis()
        except Exception:   # noqa: BLE001
            log.exception("KPI tiles incomplete")
        t = _day_label(self.focus, self.today)
        self._panel("today", "Prices today and tomorrow", self.p_today, wide=True)
        self._panel("bess-day", f"Battery arbitrage, {t}", self.p_bess_day)
        self._panel("bess-history", "Battery arbitrage potential, daily", self.p_bess_history)
        self._panel("spreads", "Daily price spread", self.p_spreads)
        self._panel("negative", "Negative-price hours per month", self.p_negative)
        self._panel("heatmap", "Average price by hour and month", self.p_heatmap, wide=True)
        self._panel("mix", "Generation mix and price, last 7 days", self.p_mix, wide=True)
        self._panel("pv-price", "When PV pushes prices down", self.p_pv_price)
        self._panel("imbalance", "Imbalance price vs day-ahead price", self.p_imbalance)
        daily = self.daily.join(self.arb[["revenue_pln", "cycles"]].rename(columns=lambda c: f"bess_{c}"), how="left")
        self.csvs["daily.csv"] = daily.drop(columns="n").round(2).reset_index().astype({"date": str})
        return self


# ---------- HTML ----------
STATUS_ICON = {"ok": ("●", "OK", "good"), "stale": ("▲", "stale, cached copy used", "warning"),
               "missing": ("✕", "missing", "critical")}


def _status_rows(dash: Dashboard) -> str:
    rows = []
    for name, st in dash.status.items():
        state = "missing" if st.missing else "stale" if st.stale else "ok"
        icon, text, cls = STATUS_ICON[state]
        last = st.last_interval.tz_convert(TZ).strftime("%Y-%m-%d %H:%M") if st.last_interval is not None else "n/a"
        detail = ", ".join(st.stale + st.missing)
        rows.append(f"<tr><td>{html.escape(SERIES[name].label)}</td><td><code>{SERIES[name].endpoint}</code></td>"
                    f"<td class=num>{last}</td><td><span class='st {cls}'>{icon}</span> {text}"
                    f"{' (' + detail + ')' if detail else ''}</td></tr>")
    return "\n".join(rows)


def _panel_html(p: Panel, fig_id: str) -> str:
    cls = "card wide" if p.wide else "card"
    links = f"<a href='data/{p.csv}' download>Data (CSV)</a>" if p.csv and not p.error else ""
    if p.error:
        body = (f"<div class='unavailable'>This panel could not be built in the latest refresh "
                f"(<code>{html.escape(p.error[:200])}</code>). The rest of the dashboard is up to date.</div>")
    else:
        body = (f"<div class='chart' id='{fig_id}' data-fig='{fig_id}-json'>"
                f"<p class='nojs'>Interactive chart needs JavaScript; the data are available as CSV.</p></div>")
    note = f"<p class='note'>{html.escape(p.note)}</p>" if p.note and not p.error else ""
    return (f"<section class='{cls}' id='{p.key}'><header><h2>{html.escape(p.title)}</h2>"
            f"<p class='takeaway'>{html.escape(p.subtitle)}</p></header>{body}"
            f"<footer>{note}<span class='links'>{links}</span></footer></section>")


def render_html(dash: Dashboard, repo_url: str) -> str:
    tiles = "\n".join(
        f"<div class='tile'><div class='tile-label'>{html.escape(t['label'])}</div>"
        f"<div class='tile-value'>{html.escape(t['value'])}</div><div class='tile-sub'>{html.escape(t['sub'])}</div></div>"
        for t in dash.kpis)
    cards, fig_json = [], []
    for i, p in enumerate(dash.panels):
        fid = f"fig-{p.key or i}"
        cards.append(_panel_html(p, fid))
        if p.fig is not None and not p.error:
            payload = pio.to_json(p.fig, validate=False).replace("</", "<\\/")
            fig_json.append(f"<script type='application/json' id='{fid}-json'>{payload}</script>")
    updated = dash.now.tz_convert(TZ)
    latest = dash.prices.index.max().tz_convert(TZ) + pd.Timedelta(minutes=15)
    tpl = Template((Path(__file__).parent / "template.html").read_text(encoding="utf-8"))
    return tpl.substitute(
        updated=f"{updated:%Y-%m-%d %H:%M} {updated:%Z}",
        updated_iso=dash.now.isoformat(),
        prices_until=f"{latest:%a %d %b %Y, %H:%M}",
        tiles=tiles, cards="\n".join(cards), figures="\n".join(fig_json),
        status_rows=_status_rows(dash), dark_map=json.dumps(figures.DARK_MAP),
        health="" if all(s.ok for s in dash.status.values()) else "warning",
        health_note="" if all(s.ok for s in dash.status.values()) else " · <a href='#methods'>some data stale</a>",
        repo_url=html.escape(repo_url), series=dash.price_series.upper(),
        battery=(f"{BATTERY.power_mw:g} MW / {BATTERY.energy_mwh:g} MWh, round-trip efficiency "
                 f"{BATTERY.round_trip_efficiency:.0%}, usable state of charge {BATTERY.soc_min:.0%}-{BATTERY.soc_max:.0%}, "
                 f"at most {BATTERY.max_cycles_per_day:g} full cycles per day, degradation cost "
                 f"{BATTERY.degradation_cost:g} PLN per MWh discharged"),
    )


def build_site(loaded: dict, today: date, now: pd.Timestamp, out_dir: Path, price_series: str = "rce",
               repo_url: str = "", cache_dir: Path | None = Path("data")) -> Dashboard:
    dash = Dashboard(loaded, today, now, price_series, cache_dir).build()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data").mkdir(exist_ok=True)
    (out_dir / "index.html").write_text(render_html(dash, repo_url), encoding="utf-8")
    (out_dir / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    for name, df in dash.csvs.items():
        df.to_csv(out_dir / "data" / name, index=False)
    (out_dir / "data" / "latest.json").write_text(json.dumps({
        "generated_utc": now.isoformat(), "today": str(today), "focus_day": str(dash.focus),
        "price_series": price_series, "kpis": dash.kpis,
        "status": {k: {"ok": s.ok, "last_interval_utc": str(s.last_interval), "stale": s.stale, "missing": s.missing}
                   for k, s in dash.status.items()},
        "panel_errors": {p.key: p.error for p in dash.panels if p.error},
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return dash
