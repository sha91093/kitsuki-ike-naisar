"""Open-Meteo Archive APIから杵築市の日降水量を取得する。"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "weather" / "kitsuki_daily_precipitation.csv"
API_URL = "https://archive-api.open-meteo.com/v1/archive"


def response_to_frame(payload: dict) -> pd.DataFrame:
    daily = payload.get("daily", {})
    dates = daily.get("time", [])
    values = daily.get("precipitation_sum", [])
    if not dates or len(dates) != len(values):
        raise ValueError("Open-Meteoの応答に日降水量がありません")
    return pd.DataFrame(
        {
            "date": dates,
            "precipitation_mm": [0.0 if value is None else float(value) for value in values],
        }
    )


def fetch(args: argparse.Namespace) -> pd.DataFrame:
    params = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "start_date": args.start,
        "end_date": args.end,
        "daily": "precipitation_sum",
        "timezone": "Asia/Tokyo",
    }
    with urlopen(f"{API_URL}?{urlencode(params)}", timeout=60) as response:  # noqa: S310
        payload = json.load(response)
    frame = response_to_frame(payload)
    frame["latitude"] = args.latitude
    frame["longitude"] = args.longitude
    frame["source"] = "Open-Meteo Archive API"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output, index=False, float_format="%.1f")
    return frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="杵築市の日降水量を取得する")
    parser.add_argument("--start", default="2026-06-17")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--latitude", type=float, default=33.42)
    parser.add_argument("--longitude", type=float, default=131.62)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


if __name__ == "__main__":
    data = fetch(parse_args())
    print(f"{len(data)}日分を出力しました")
