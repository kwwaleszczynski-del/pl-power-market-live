from datetime import UTC, date, datetime

import pandas as pd
import pse_opendata as pse
import requests

from pl_power_market import pse_api

RECORDS = [
    {"dtime_utc": "2025-03-29 23:15:00", "rce_pln": "412.5", "business_date": "2025-03-30"},
    {"dtime_utc": "2025-03-29 23:30", "rce_pln": "-15.0", "business_date": "2025-03-30"},
]


class FakeResponse:
    def __init__(self, payload):
        self.status_code, self.headers, self.reason = 200, {}, "OK"
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Serves ``records`` for the requested business dates, or one record per day; fails when ``down``."""

    def __init__(self, down=False, records=None):
        self.calls = []
        self.down = down
        self.records = records

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        if self.down:
            raise requests.ConnectionError("PSE API unreachable")
        f = params["$filter"]
        start, end = (s.strip("'") for s in (f.split()[2], f.split()[6]))
        if self.records is not None:
            return FakeResponse({"value": [r for r in self.records if start <= r["business_date"] <= end]})
        days = pd.date_range(start, end, freq="D")
        return FakeResponse({"value": [{"dtime_utc": f"{d:%Y-%m-%d} 10:00:00", "rce_pln": 100.0,
                                        "business_date": f"{d:%Y-%m-%d}"} for d in days]})


def client(session, tmp_path, today: date) -> pse.Client:
    return pse.Client(session=session, cache_dir=tmp_path, now=lambda: datetime(*today.timetuple()[:3], 12, tzinfo=UTC),
                      sleep=lambda s: None, min_interval=0, retries=2, allow_stale=True)


def load(series, start, end, tmp_path, today, session, **kwargs):
    return pse_api.load_series(series, start, end, tmp_path, client=client(session, tmp_path, today), **kwargs)


def test_frames_are_indexed_by_interval_start_in_utc(tmp_path):
    session = FakeSession(records=RECORDS)
    df, st = load("rce", date(2025, 3, 30), date(2025, 3, 30), tmp_path, date(2025, 3, 30), session)
    assert df.index[0] == pd.Timestamp("2025-03-29 23:00", tz="UTC") and df.index.name == "ts_utc"
    assert df.index[1] - df.index[0] == pd.Timedelta(minutes=15)
    assert df["price"].tolist() == [412.5, -15.0]
    assert df["business_date"].iloc[0] == date(2025, 3, 30)
    assert st.fetched == ["2025-03"] and st.ok


def test_future_intervals_without_values_are_dropped(tmp_path):
    recs = [
        {"dtime_utc": "2026-10-21 10:00:00", "business_date": "2026-10-21", "demand": 20000, "pv": 5000, "wi": 3000,
         "jg": 11000, "jnwrb": 9000, "swm_p": -500, "swm_np": 100},
        {"dtime_utc": "2026-10-21 10:15:00", "business_date": "2026-10-21", "demand": None, "pv": None, "wi": None,
         "jg": None, "jnwrb": None, "swm_p": None, "swm_np": None},
    ]
    df, _ = load("mix", date(2026, 10, 21), date(2026, 10, 21), tmp_path, date(2026, 10, 21), FakeSession(records=recs))
    assert len(df) == 1
    assert df.loc[pd.Timestamp("2026-10-21 09:45", tz="UTC"), "wind"] == 3000


def test_empty_frame():
    df = pse_api.empty_frame("mix")
    assert df.empty
    assert {"pv", "wind", "business_date"} <= set(df.columns)


def test_load_series_caches_final_months(tmp_path):
    today = date(2024, 9, 15)
    df, st = load("rce", date(2024, 7, 1), date(2024, 9, 16), tmp_path, today, FakeSession())
    assert st.fetched == ["2024-07", "2024-08", "2024-09"] and st.ok
    assert len(df) == 31 + 31 + 16
    # second run: July and August are past the revision window of prices, September is downloaded again
    s2 = FakeSession()
    df2, st2 = load("rce", date(2024, 7, 1), date(2024, 9, 16), tmp_path, today, s2)
    assert st2.cached == ["2024-07", "2024-08"] and st2.fetched == ["2024-09"]
    assert len(s2.calls) == 1
    pd.testing.assert_frame_equal(df, df2)


def test_month_cached_too_early_is_downloaded_again(tmp_path):
    # July cached on 9 July, read again in September: the copy is not final, so July is downloaded again
    load("rce", date(2024, 7, 1), date(2024, 7, 10), tmp_path, date(2024, 7, 9), FakeSession())
    df, st = load("rce", date(2024, 7, 1), date(2024, 7, 31), tmp_path, date(2024, 9, 1), FakeSession())
    assert st.fetched == ["2024-07"]
    assert len(df) == 31


def test_api_down_uses_stale_cache_and_never_raises(tmp_path):
    load("rce", date(2024, 8, 1), date(2024, 9, 16), tmp_path, date(2024, 9, 15), FakeSession())
    df, st = load("rce", date(2024, 8, 1), date(2024, 9, 16), tmp_path, date(2024, 9, 15), FakeSession(down=True))
    assert st.cached == ["2024-08"] and st.stale == ["2024-09"] and not st.ok
    assert len(df) == 31 + 16
    assert st.last_interval == pd.Timestamp("2024-09-16 09:45", tz="UTC")


def test_api_down_without_cache_returns_empty_and_stops_trying(tmp_path):
    down = FakeSession(down=True)
    df, st = load("rce", date(2024, 7, 1), date(2024, 12, 31), tmp_path, date(2025, 1, 1), down, max_failures=2)
    assert df.empty and st.last_interval is None
    assert len(st.missing) == 6
    assert len(down.calls) == 2 * 3   # two months x three attempts, then the API is assumed down


def test_api_down_after_failures_serves_the_rest_from_cache_as_stale(tmp_path):
    load("rce", date(2024, 9, 1), date(2024, 9, 30), tmp_path, date(2024, 9, 15), FakeSession())
    down = FakeSession(down=True)
    df, st = load("rce", date(2024, 7, 1), date(2024, 9, 30), tmp_path, date(2024, 9, 20), down, max_failures=2)
    assert st.missing == ["2024-07", "2024-08"] and st.stale == ["2024-09"]
    assert len(down.calls) == 2 * 3 and len(df) == 30


def test_offline_reads_cache_only(tmp_path):
    load("rce", date(2024, 9, 1), date(2024, 9, 16), tmp_path, date(2024, 9, 15), FakeSession())
    s = FakeSession()
    df, st = load("rce", date(2024, 9, 1), date(2024, 10, 2), tmp_path, date(2024, 9, 15), s, offline=True)
    assert s.calls == []
    assert st.cached == ["2024-09"] and st.missing == ["2024-10"]
    assert len(df) == 30     # whole months are cached
