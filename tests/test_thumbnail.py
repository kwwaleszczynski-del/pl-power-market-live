import pandas as pd
from PIL import Image

from pl_power_market.page import build_site
from pl_power_market.thumbnail import HEIGHT, WIDTH, write_thumbnail

NOW = pd.Timestamp("2026-06-10 13:30", tz="UTC")


def test_thumbnail_from_built_site(tmp_path, synthetic_loaded):
    today, loaded = synthetic_loaded
    build_site(loaded, today=today, now=NOW, out_dir=tmp_path, cache_dir=None)
    path = write_thumbnail(tmp_path)
    with Image.open(path) as im:
        assert im.format == "WEBP" and im.size == (WIDTH, HEIGHT)
