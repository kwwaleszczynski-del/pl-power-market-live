"""Command line interface.

Usage:
    python -m pl_power_market fetch                    # download / refresh the data cache only
    python -m pl_power_market build --out site         # refresh data and render the dashboard
    python -m pl_power_market build --today 2026-10-21 --offline   # rebuild from cache, fixed "today"

Data are cached as monthly Parquet files in ./data (override with --data-dir or PLPM_DATA_DIR). The build never
fails on API errors as long as price data are available (from the API or the cache); without any price data it
exits with code 2 so that a scheduled deployment keeps the previously published page.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from pl_power_market.metrics import TZ
from pl_power_market.pse_api import load_series

log = logging.getLogger("pl_power_market")
HISTORY_START = date(2024, 6, 14)   # 15-minute RCE and the reformed balancing market start here


def _date(s: str) -> date:
    return date.fromisoformat(s)


def load_all(args, today: date):
    """All series needed by the dashboard: name -> (frame, status)."""
    end = today + timedelta(days=1)
    starts = {
        "rce": HISTORY_START, "sdac": HISTORY_START, "mix": HISTORY_START,
        "imb": today - timedelta(days=45), "fcst": today - timedelta(days=2),
    }
    out = {}
    for name, start in starts.items():
        out[name] = load_series(name, start, end, args.data_dir, today=today, offline=args.offline)
        df, status = out[name]
        log.info("%s: %d intervals, last %s, fetched %d, cached %d, stale %s, missing %s", name, len(df),
                 status.last_interval, len(status.fetched), len(status.cached), status.stale, status.missing)
    return out


def cmd_fetch(args, today: date):
    return load_all(args, today)


def cmd_build(args, today: date) -> int:
    from pl_power_market.page import build_site

    loaded = load_all(args, today)
    if loaded[args.price_series][0].empty:
        log.error("no %s price data from the API or the cache - not building", args.price_series)
        return 2
    build_site(loaded, today=today, now=pd.Timestamp.now(tz="UTC"), out_dir=args.out,
               price_series=args.price_series, repo_url=args.repo_url)
    log.info("dashboard written to %s", args.out / "index.html")
    try:
        from pl_power_market.thumbnail import write_thumbnail
        write_thumbnail(args.out)
    except Exception:  # the preview image must never block the dashboard
        log.exception("thumbnail not written")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pl-power-market", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "build"):
        p = sub.add_parser(name)
        p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("PLPM_DATA_DIR", "data")))
        p.add_argument("--today", type=_date, default=None, help="override today's date (Europe/Warsaw)")
        p.add_argument("--offline", action="store_true", help="use the cache only, no API calls")
        if name == "build":
            p.add_argument("--out", type=Path, default=Path("site"), help="output folder for the static site")
            p.add_argument("--price-series", choices=("rce", "sdac"), default="rce",
                           help="price series for history panels and the battery model")
            p.add_argument("--repo-url", default="https://github.com/kwwaleszczynski-del/pl-power-market-live")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    today = args.today or pd.Timestamp.now(tz=TZ).date()
    if args.cmd == "fetch":
        cmd_fetch(args, today)
        return
    sys.exit(cmd_build(args, today))


if __name__ == "__main__":
    main()
