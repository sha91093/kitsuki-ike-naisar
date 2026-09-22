"""GeoJSON、後方散乱時系列、降水量をWeb診断画面用JSONへまとめる。"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PONDS = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_NAMES = REPO_ROOT / "杵築池リスト名前入り.csv"
DEFAULT_TIMESERIES = REPO_ROOT / "data" / "derived" / "nisar_backscatter_timeseries.csv"
DEFAULT_VARIABILITY = REPO_ROOT / "data" / "derived" / "nisar_backscatter_variability.csv"
DEFAULT_SCENES = REPO_ROOT / "data" / "derived" / "nisar_scene_quality.csv"
DEFAULT_WEATHER = REPO_ROOT / "data" / "weather" / "kitsuki_daily_precipitation.csv"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "data.json"


def rainfall_lookup(weather: pd.DataFrame) -> dict[pd.Timestamp, float]:
    return {
        pd.Timestamp(row.date).normalize(): float(row.precipitation_mm)
        for row in weather.itertuples()
    }


def antecedent_rain(lookup: dict[pd.Timestamp, float], day: pd.Timestamp, days: int) -> float:
    return round(
        sum(lookup.get(day - timedelta(days=offset), 0.0) for offset in range(days)),
        1,
    )


def clean_number(value, digits: int = 3):
    if pd.isna(value):
        return None
    return round(float(value), digits)


def build(args: argparse.Namespace) -> dict:
    ponds = gpd.read_file(args.ponds).to_crs("EPSG:4326")
    names = pd.read_csv(args.names, dtype={"simple_id": str})
    timeseries = pd.read_csv(args.timeseries, dtype={"pond_id": str})
    variability = pd.read_csv(args.variability, dtype={"pond_id": str})
    scenes = pd.read_csv(args.scenes)
    weather = pd.read_csv(args.weather)

    names_by_id = names.set_index("simple_id").to_dict(orient="index")
    rain = rainfall_lookup(weather)
    scene_map = {
        (str(row.granule_id), str(row.layer)): row
        for row in scenes.itertuples()
    }

    points_by_pond: dict[str, list[dict]] = {}
    for row in timeseries.itertuples():
        timestamp = pd.Timestamp(row.observation_time)
        local_time = timestamp.tz_convert("Asia/Tokyo")
        local_day = local_time.tz_localize(None).normalize()
        scene = scene_map[(str(row.granule_id), str(row.layer))]
        points_by_pond.setdefault(str(row.pond_id), []).append(
            {
                "date": local_day.strftime("%Y-%m-%d"),
                "time_jst": local_time.strftime("%Y-%m-%d %H:%M"),
                "orbit": str(row.orbit_direction).lower(),
                "layer": str(row.layer),
                "zone": str(row.zone),
                "median_db": clean_number(row.median_db),
                "p10_db": clean_number(row.p10_db),
                "p90_db": clean_number(row.p90_db),
                "positive_ratio": clean_number(row.positive_ratio),
                "scene_shift_db": clean_number(scene.scene_shift_db),
                "scene_flag": str(scene.scene_quality_flag),
                "rain_1d_mm": antecedent_rain(rain, local_day, 1),
                "rain_3d_mm": antecedent_rain(rain, local_day, 3),
                "rain_7d_mm": antecedent_rain(rain, local_day, 7),
            }
        )

    variability_by_pond: dict[str, list[dict]] = {}
    for row in variability.itertuples():
        variability_by_pond.setdefault(str(row.pond_id), []).append(
            {
                "orbit": str(row.orbit_direction).lower(),
                "layer": str(row.layer),
                "zone": str(row.zone),
                "range_db": clean_number(row.median_range_db),
                "range_without_common_shift_db": clean_number(
                    row.median_range_without_common_shift_db
                ),
                "std_db": clean_number(row.median_std_db),
                "observations": int(row.observation_count),
            }
        )

    web_ponds = []
    for pond in ponds.itertuples():
        pond_id = str(pond.simple_id)
        info = names_by_id.get(pond_id, {})
        centroid = pond.geometry.representative_point()
        web_ponds.append(
            {
                "id": pond_id,
                "name": info.get("name", f"池 {pond_id}"),
                "tiiki": info.get("tiiki", ""),
                "ooaza": info.get("ooaza", ""),
                "area_m2": clean_number(getattr(pond, "area_m2", None), 1),
                "area_ha": clean_number(info.get("area_ha"), 2),
                "lat": clean_number(info.get("latitude", centroid.y), 7),
                "lng": clean_number(info.get("longitude", centroid.x), 7),
                "geometry": mapping(pond.geometry),
                "timeseries": sorted(
                    points_by_pond.get(pond_id, []),
                    key=lambda item: (item["date"], item["orbit"], item["zone"], item["layer"]),
                ),
                "variability": variability_by_pond.get(pond_id, []),
            }
        )

    result = {
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "status": "NISAR L-band SAR 後方散乱診断（面積推定前）",
        "classification_defaults": {
            "hh_change_db": 1.5,
            "hh_minus_hv_change_db": 0.5,
            "wet_7d_mm": 20.0,
            "description": "候補抽出用の暫定値。確定した水域分類条件ではありません。",
        },
        "rainfall": [
            {"date": str(row.date), "mm": clean_number(row.precipitation_mm, 1)}
            for row in weather.itertuples()
        ],
        "ponds": sorted(web_ponds, key=lambda item: int(item["id"])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NISAR診断画面用JSONを生成する")
    parser.add_argument("--ponds", type=Path, default=DEFAULT_PONDS)
    parser.add_argument("--names", type=Path, default=DEFAULT_NAMES)
    parser.add_argument("--timeseries", type=Path, default=DEFAULT_TIMESERIES)
    parser.add_argument("--variability", type=Path, default=DEFAULT_VARIABILITY)
    parser.add_argument("--scenes", type=Path, default=DEFAULT_SCENES)
    parser.add_argument("--weather", type=Path, default=DEFAULT_WEATHER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


if __name__ == "__main__":
    dashboard = build(parse_args())
    print(f"{len(dashboard['ponds'])}池を出力しました")
