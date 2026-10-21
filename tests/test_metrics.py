from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import make_frame

from pl_power_market import metrics


def test_daily_prices_handles_dst_and_drops_incomplete_days():
    days = [date(2026, 3, 28), date(2026, 3, 29), date(2026, 10, 25)]   # normal, 23 h, 25 h
    prices = make_frame(days, price=lambda h, d: np.where(h < 12, -10.0, 100.0))
    incomplete = make_frame([date(2026, 3, 30)], price=50.0).iloc[:40]
    daily = metrics.daily_prices(pd.concat([prices, incomplete]))
    assert list(daily.index.date) == days
    assert daily["n"].tolist() == [96, 92, 100]
    assert daily["spread"].tolist() == [110.0, 110.0, 110.0]
    # negative before local noon: 12 h, 11 h on the spring DST day, 13 h on the autumn one
    assert daily["neg_hours"].tolist() == [12.0, 11.0, 13.0]
    assert daily["spread_2h"].tolist() == [110.0, 110.0, 110.0]


def test_negative_hours_monthly():
    prices = make_frame([date(2026, 5, 1), date(2026, 5, 2), date(2026, 6, 1)],
                        price=lambda h, d: np.where((h >= 11) & (h < 14), -5.0, 0.0 if d.month == 6 else 80.0))
    neg = metrics.negative_hours_monthly(prices)
    assert neg.loc[pd.Period("2026-05", "M"), "negative_h"] == 6.0
    assert neg.loc[pd.Period("2026-05", "M"), "days"] == 2
    assert neg.loc[pd.Period("2026-06", "M"), "negative_h"] == 3.0
    assert neg.loc[pd.Period("2026-06", "M"), "nonpositive_h"] == 24.0


def test_hour_month_profile_uses_local_time():
    # 1 July local midnight is 30 June 22:00 UTC; it must land in hour 0 of July
    prices = make_frame([date(2026, 7, 1)], price=lambda h, d: h)
    prof = metrics.hour_month_profile(prices)
    assert list(prof.columns) == [pd.Period("2026-07", "M")]
    assert prof.index.tolist() == list(range(24))
    assert prof.iloc[0, 0] == pytest.approx(0.375)    # mean of 0, 0.25, 0.5, 0.75
    assert prof.iloc[23, 0] == pytest.approx(23.375)


def test_mix_with_price_derives_thermal_and_import():
    days = [date(2026, 6, 1)]
    mix = make_frame(days, demand=20000.0, pv=6000.0, wind=2000.0, jg=11000.0, jnwrb=9500.0,
                     exch_par=-700.0, exch_nonpar=200.0)
    prices = make_frame(days, price=123.0)
    out = metrics.mix_with_price(mix, prices)
    row = out.iloc[0]
    assert row["other"] == pytest.approx(12.5)          # 11 + 9.5 - 2 - 6 GW
    assert row["net_import"] == pytest.approx(-0.5)
    assert row["residual"] == pytest.approx(12.0)
    assert row["price"] == 123.0


def test_pv_price_bins_groups_midday_by_pv_output():
    days = [date(2026, 6, d) for d in range(1, 21)]
    mix = make_frame(days, demand=20000.0, pv=lambda h, d: np.full(len(h), 2500.0 if d.day <= 10 else 9500.0),
                     wind=0.0, jg=0.0, jnwrb=0.0, exch_par=0.0, exch_nonpar=0.0)
    prices = make_frame(days, price=lambda h, d: np.full(len(h), 400.0 if d.day <= 10 else -20.0))
    bins = metrics.pv_price_bins(metrics.mix_with_price(mix, prices), hours=(10, 15), bin_gw=1.0)
    assert bins["pv_gw"].tolist() == [2.5, 9.5]
    assert bins["n"].tolist() == [10 * 20, 10 * 20]     # 20 quarter-hours between 10:00 and 15:00 per day
    assert bins["median"].tolist() == [400.0, -20.0]
    assert bins["neg_share"].tolist() == [0.0, 1.0]
