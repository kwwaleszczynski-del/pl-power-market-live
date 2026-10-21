import json
from datetime import timedelta

import pandas as pd
import pytest

from pl_power_market import cli
from pl_power_market.page import build_site
from pl_power_market.pse_api import SeriesStatus

PANELS = ["today", "bess-day", "bess-history", "spreads", "negative", "heatmap", "mix", "pv-price", "imbalance"]
NOW = pd.Timestamp("2026-06-10 13:30", tz="UTC")


def test_full_site_from_synthetic_data(tmp_path, synthetic_loaded):
    today, loaded = synthetic_loaded
    dash = build_site(loaded, today=today, now=NOW, out_dir=tmp_path, cache_dir=None)
    assert dash.focus == today + timedelta(days=1)            # tomorrow's prices are published
    assert [p.key for p in dash.panels] == PANELS
    assert not [p.key for p in dash.panels if p.error]
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    for key in PANELS:
        assert f"id='{key}'" in html and f"id='fig-{key}-json'" in html
    assert "$" not in html.split("<script type='application/json'")[0]   # every template field was filled
    assert (tmp_path / "plotly.min.js").stat().st_size > 1_000_000
    latest = json.loads((tmp_path / "data" / "latest.json").read_text(encoding="utf-8"))
    assert "some data stale" not in html
    assert latest["focus_day"] == "2026-06-11" and len(latest["kpis"]) == 6 and latest["panel_errors"] == {}
    daily = pd.read_csv(tmp_path / "data" / "daily.csv")
    assert {"spread", "neg_hours", "bess_revenue_pln"} <= set(daily.columns)


def test_missing_series_degrade_to_notes_not_failures(tmp_path, synthetic_loaded):
    today, loaded = synthetic_loaded
    for name in ("mix", "imb", "fcst"):
        df, _ = loaded[name]
        loaded[name] = (df.iloc[0:0], SeriesStatus(name, missing=["2026-06"]))
    # tomorrow not published yet
    rce, st = loaded["rce"]
    loaded["rce"] = (rce[rce["business_date"] <= today], st)
    dash = build_site(loaded, today=today, now=NOW, out_dir=tmp_path, cache_dir=None)
    assert dash.focus == today
    failed = {p.key for p in dash.panels if p.error}
    assert failed == {"mix", "pv-price", "imbalance"}
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert html.count("could not be built") == 3
    assert "not published yet" in html
    assert "missing (2026-06)" in html
    assert "some data stale" in html


def test_build_without_any_price_data_exits_2(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["build", "--offline", "--data-dir", str(tmp_path / "empty"), "--out", str(tmp_path / "site"),
                  "--today", "2026-06-10"])
    assert e.value.code == 2
    assert not (tmp_path / "site" / "index.html").exists()


def test_battery_failure_keeps_market_panels(tmp_path, synthetic_loaded, monkeypatch):
    from pl_power_market import page

    def broken(*args, **kwargs):
        raise RuntimeError("MILP failed")

    monkeypatch.setattr(page, "arbitrage_daily", broken)
    today, loaded = synthetic_loaded
    dash = build_site(loaded, today=today, now=NOW, out_dir=tmp_path, cache_dir=None)
    assert {p.key for p in dash.panels if p.error} == {"bess-day", "bess-history"}
    assert len(dash.kpis) == 5
