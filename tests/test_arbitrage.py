from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import make_frame

from pl_power_market import arbitrage


def prices_two_days():
    flat = make_frame([date(2026, 5, 1)], price=400.0)
    duck = make_frame([date(2026, 5, 2)], price=lambda h, d: np.where((h >= 10) & (h < 15), -50.0, 700.0))
    return pd.concat([flat, duck])


def test_flat_day_earns_nothing_spread_day_earns():
    daily = arbitrage.arbitrage_daily(prices_two_days())
    assert daily.loc[pd.Timestamp("2026-05-01"), "revenue_pln"] == pytest.approx(0.0, abs=1e-6)
    assert daily.loc[pd.Timestamp("2026-05-02"), "revenue_pln"] > 1000
    assert daily.loc[pd.Timestamp("2026-05-02"), "avg_buy_price"] == pytest.approx(-50.0)


def test_cache_skips_solved_days_and_resolves_changed_ones(tmp_path, monkeypatch):
    cache = tmp_path / "bess.parquet"
    prices = prices_two_days()
    first = arbitrage.arbitrage_daily(prices, cache=cache)

    def boom(*args, **kwargs):
        raise AssertionError("optimizer must not run for cached days")

    monkeypatch.setattr(arbitrage, "run_backtest", boom)
    again = arbitrage.arbitrage_daily(prices, cache=cache)
    pd.testing.assert_frame_equal(first, again, check_freq=False)

    monkeypatch.undo()
    solved = []
    real = arbitrage.run_backtest

    def counting(p, params):
        solved.extend(p["business_date"].unique())
        return real(p, params)

    monkeypatch.setattr(arbitrage, "run_backtest", counting)
    revised = prices.copy()
    revised.loc[revised["business_date"] == date(2026, 5, 1), "price"] += 1.0
    arbitrage.arbitrage_daily(revised, cache=cache)
    assert solved == [date(2026, 5, 1)]


def test_incomplete_day_is_skipped():
    prices = make_frame([date(2026, 5, 3)], price=100.0).iloc[:50]
    assert arbitrage.arbitrage_daily(prices).empty
    assert arbitrage.day_schedule(prices, date(2026, 5, 3)) is None


def test_day_schedule_charges_in_the_negative_window():
    s = arbitrage.day_schedule(prices_two_days(), date(2026, 5, 2))
    local_hour = s.index.tz_convert("Europe/Warsaw").hour
    midday = (local_hour >= 10) & (local_hour < 15)
    assert s.loc[midday, "charge_mw"].sum() > 0
    assert s.loc[midday, "discharge_mw"].sum() == pytest.approx(0.0, abs=1e-9)
