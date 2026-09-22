import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from scripts.summarize_backscatter import pond_zones, power_statistics, scene_quality


def test_power_statistics_converts_linear_power_to_db():
    result = power_statistics(np.array([0.0, 0.01, 0.1, 1.0, np.nan]))

    assert result["sampled_pixels"] == 4
    assert result["positive_pixels"] == 3
    assert result["positive_ratio"] == 0.75
    assert result["min_db"] == -20.0
    assert result["median_db"] == -10.0
    assert result["max_db"] == 0.0


def test_pond_zones_splits_inner_shoreline_and_core():
    ponds = gpd.GeoDataFrame(
        {"simple_id": ["pond-1"], "name": ["試験池"]},
        geometry=[box(0, 0, 200, 200)],
        crs="EPSG:32652",
    )

    zones = pond_zones(ponds, 50.0)
    by_name = {item["zone"]: item for item in zones}

    assert set(by_name) == {"whole", "shoreline", "core"}
    assert by_name["whole"]["geometry_area_m2"] == 40000.0
    assert by_name["core"]["geometry_area_m2"] == 10000.0
    assert by_name["shoreline"]["geometry_area_m2"] == 30000.0


def test_scene_quality_flags_common_shift():
    records = []
    for date, value in [("2026-01-01", -15.0), ("2026-01-13", -14.5), ("2026-01-25", -7.0)]:
        for pond_id in ("1", "2"):
            records.append(
                {
                    "granule_id": date,
                    "observation_time": f"{date}T00:00:00Z",
                    "observation_date": date,
                    "orbit_direction": "DESCENDING",
                    "layer": "HHHH",
                    "zone": "shoreline",
                    "pond_id": pond_id,
                    "median_db": value,
                }
            )

    quality = scene_quality(pd.DataFrame(records), 3.0)

    assert quality.iloc[-1]["scene_quality_flag"] == "common_shift"
    assert quality.iloc[0]["scene_quality_flag"] == "ok"
