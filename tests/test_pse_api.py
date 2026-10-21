from datetime import date

import pandas as pd
import pytest
import requests

from pl_power_market import pse_api

RECORDS = [
    {"dtime_utc": "2025-03-29 23:15:00", "rce_pln": "412.5", "business_date": "2025-03-30"},
    {"dtime_utc": "2025-03-29 23:30", "rce_pln": "-15.0", "business_date": "2025-03-30"},
]


def test_parse_records_uses_interval_start_in_utc():
    df = pse_api.parse_records(RECORDS, "rce")
    assert df.index[0] == pd.Timestamp("2025-03-29 23:00", tz="UTC")
    assert df.index[1] - df.index[0] == pd.Timedelta(minutes=15)
    assert df["price"].tolist() == [412.5, -15.0]
    assert df["business_date"].iloc[0] == date(2025, 3, 30)


def test_parse_drops_future_intervals_without_values():
    recs = [
        {"dtime_utc": "2026-10-21 10:00:00", "business_date": "2026-10-21", "demand": 20000, "pv": 5000, "wi": 3000,
         "jg": 11000, "jnwrb": 9000, "swm_p": -500, "swm_np": 100},
        {"dtime_utc": "2026-10-21 10:15:00", "business_date": "2026-10-21", "demand": None, "pv": None, "wi": None,
         "jg": None, "jnwrb": None, "swm_p": None, "swm_np": None},
    ]
    df = pse_api.parse_records(recs, "mix")
    assert len(df) == 1
    assert df.loc[pd.Timestamp("2026-10-21 09:45", tz="UTC"), "wind"] == 3000


def test_parse_empty():
    df = pse_api.parse_records([], "mix")
    assert df.empty
    assert {"pv", "wind", "business_date"} <= set(df.columns)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Serves one record per business day of the requested month, or fails when ``down``."""

    def __init__(self, down=False, page_size=None):
        self.headers = {}
        self.calls = []
        self.down = down
        self.page_size = page_size

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.down:
            raise requests.ConnectionError("PSE API unreachable")
        if params is None:   # nextLink page
            return FakeResponse({"value": [{"dtime_utc": "2024-07-01 00:15:00", "rce_pln": 1.0,
                                            "business_date": "2024-07-01"}]})
        f = params["$filter"]
        start, end = (pd.Timestamp(s.strip("'")) for s in (f.split()[2], f.split()[6]))
        days = pd.date_range(start, end, freq="D")
        value = [{"dtime_utc": f"{d:%Y-%m-%d} 10:00:00", "rce_pln": 100.0, "business_date": f"{d:%Y-%m-%d}"}
                 for d in days]
        if self.page_size:
            return FakeResponse({"value": value[:self.page_size], "nextLink": "https://api.raporty.pse.pl/api/next"})
        return FakeResponse({"value": value})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pse_api.time, "sleep", lambda s: None)


def test_fetch_period_follows_pagination():
    session = FakeSession(page_size=1)
    df = pse_api.fetch_period("rce", date(2024, 6, 30), date(2024, 6, 30), session=session)
    assert len(session.calls) == 2
    assert session.calls[1] == ("https://api.raporty.pse.pl/api/next", None)
    assert len(df) == 2


def test_load_series_caches_final_months(tmp_path):
    today = date(2024, 9, 15)
    s1 = FakeSession()
    df, st = pse_api.load_series("rce", date(2024, 7, 1), date(2024, 9, 16), tmp_path, today=today, session=s1)
    assert st.fetched == ["2024-07", "2024-08", "2024-09"] and st.ok
    assert len(df) == 31 + 31 + 16
    # second run: July and August are final and complete in cache, September is refreshed
    s2 = FakeSession()
    df2, st2 = pse_api.load_series("rce", date(2024, 7, 1), date(2024, 9, 16), tmp_path, today=today, session=s2)
    assert st2.cached == ["2024-07", "2024-08"] and st2.fetched == ["2024-09"]
    assert len(s2.calls) == 1
    pd.testing.assert_frame_equal(df, df2)


def test_incomplete_final_month_is_downloaded_again(tmp_path):
    # cache written mid-July, read again in September: July must be refetched, not trusted
    pse_api.load_series("rce", date(2024, 7, 1), date(2024, 7, 10), tmp_path, today=date(2024, 7, 9), session=FakeSession())
    s = FakeSession()
    df, st = pse_api.load_series("rce", date(2024, 7, 1), date(2024, 7, 31), tmp_path, today=date(2024, 9, 1), session=s)
    assert st.fetched == ["2024-07"]
    assert len(df) == 31


def test_api_down_uses_stale_cache_and_never_raises(tmp_path):
    pse_api.load_series("rce", date(2024, 8, 1), date(2024, 9, 16), tmp_path, today=date(2024, 9, 15), session=FakeSession())
    down = FakeSession(down=True)
    df, st = pse_api.load_series("rce", date(2024, 8, 1), date(2024, 9, 16), tmp_path, today=date(2024, 9, 15),
                                 session=down)
    assert st.cached == ["2024-08"] and st.stale == ["2024-09"] and not st.ok
    assert len(df) == 31 + 16
    assert st.last_interval == pd.Timestamp("2024-09-16 09:45", tz="UTC")


def test_api_down_without_cache_returns_empty_and_stops_trying(tmp_path):
    down = FakeSession(down=True)
    df, st = pse_api.load_series("rce", date(2024, 1, 1), date(2024, 12, 31), tmp_path, today=date(2025, 1, 1),
                                 session=down, max_failures=2)
    assert df.empty and st.last_interval is None
    assert len(st.missing) == 12
    assert len(down.calls) == 2 * 3   # two months x three attempts, then the API is assumed down


def test_offline_reads_cache_only(tmp_path):
    pse_api.load_series("rce", date(2024, 9, 1), date(2024, 9, 16), tmp_path, today=date(2024, 9, 15), session=FakeSession())
    s = FakeSession()
    df, st = pse_api.load_series("rce", date(2024, 9, 1), date(2024, 10, 2), tmp_path, today=date(2024, 9, 15),
                                 session=s, offline=True)
    assert s.calls == []
    assert st.cached == ["2024-09"] and st.missing == ["2024-10"]
    assert len(df) == 16
