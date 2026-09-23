"""Sentinel-2 L2AでNISAR開放水面試算を光学検証する。"""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from rasterio.windows import from_bounds
from shapely.geometry import mapping, shape as geometry_shape
from shapely.ops import unary_union

try:
    from scripts.estimate_open_water import (
        adaptive_minimum_pixels,
        boundary_pixels,
        connected_to_seed,
    )
except ModuleNotFoundError:
    from estimate_open_water import adaptive_minimum_pixels, boundary_pixels, connected_to_seed


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEARCH = REPO_ROOT / "work" / "sentinel2_search.json"
DEFAULT_PONDS = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_NISAR = REPO_ROOT / "docs" / "data" / "open_water_27.json"
DEFAULT_CSV = REPO_ROOT / "data" / "derived" / "pond_27_sentinel2_validation.csv"
DEFAULT_JSON = REPO_ROOT / "docs" / "data" / "sentinel2_27.json"
DEFAULT_STAC_URL = "https://earth-search.aws.element84.com/v1/search"

# Sentinel-2 L2A Scene Classification Layer (SCL)の無効分類。
# 0: No data, 1: Saturated/defective, 3: Cloud shadow,
# 8/9: Cloud, 10: Cirrus, 11: Snow/ice
INVALID_SCL = frozenset({0, 1, 3, 8, 9, 10, 11})


def stac_search_payload(bounds, start: str, end: str, maximum_cloud: float) -> dict:
    """Earth Searchへ送るSentinel-2 L2A検索条件を作る。"""
    return {
        "collections": ["sentinel-2-l2a"],
        "bbox": list(bounds),
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": maximum_cloud}},
        "limit": 100,
    }


def load_or_search_catalog(args: argparse.Namespace, pond) -> dict:
    """保存済みSTAC結果を読み、なければ公開APIを検索する。"""
    if args.search_json.exists():
        return json.loads(args.search_json.read_text())
    payload = stac_search_payload(pond.bounds, args.start, args.end, args.maximum_cloud)
    request = urllib.request.Request(
        args.stac_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "kitsuki-ike-nisar/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a-b)/(a+b)をゼロ除算なしで計算する。"""
    denominator = a + b
    return np.divide(
        a - b,
        denominator,
        out=np.full(a.shape, np.nan, dtype="float64"),
        where=np.isfinite(denominator) & (denominator != 0),
    )


def valid_scl_mask(scl: np.ndarray) -> np.ndarray:
    """SCLから雲・雲影・欠損等を除いた有効画素マスクを返す。"""
    return ~np.isin(scl, tuple(INVALID_SCL))


def choose_nearest_observation(
    target_date: pd.Timestamp,
    observations: pd.DataFrame,
    maximum_days: int,
    used_ids: set[str] | None = None,
) -> pd.Series | None:
    """品質条件を通った未使用の光学観測から最近傍日を選ぶ。"""
    candidates = observations[
        (observations["quality_flag"] == "ok") & observations["seed_valid"]
    ].copy()
    if used_ids:
        candidates = candidates[~candidates["item_id"].isin(used_ids)]
    if candidates.empty:
        return None
    candidates["day_offset"] = (candidates["observation_date"] - target_date).dt.days
    candidates["absolute_days"] = candidates["day_offset"].abs()
    candidates = candidates[candidates["absolute_days"] <= maximum_days]
    if candidates.empty:
        return None
    return candidates.sort_values(["absolute_days", "observation_date"]).iloc[0]


def _read_matched_asset(
    href: str,
    crs,
    bounds,
    width: int,
    height: int,
    resampling: Resampling,
) -> np.ndarray:
    with rasterio.open(href) as source:
        if source.crs != crs:
            raise ValueError(f"バンド間でCRSが異なります: {source.crs} != {crs}")
        window = from_bounds(*bounds, source.transform)
        return source.read(
            1,
            window=window,
            out_shape=(height, width),
            boundless=True,
            fill_value=0,
            resampling=resampling,
        ).astype("float64")


def analyze_item(
    feature: dict,
    pond_wgs84,
    buffers_m: list[float],
    seed_inset_m: float,
    minimum_valid_ratio: float,
    map_buffer_m: float,
) -> list[dict]:
    """1つのSentinel-2 STAC itemを池周辺だけ解析する。"""
    assets = feature["assets"]
    green_href = assets["green"]["href"]
    with rasterio.open(green_href) as green_source:
        pond = gpd.GeoSeries([pond_wgs84], crs="EPSG:4326").to_crs(green_source.crs).iloc[0]
        analysis_geometry = pond.buffer(max(buffers_m))
        window = from_bounds(*analysis_geometry.bounds, green_source.transform)
        window = window.round_offsets().round_lengths()
        green = green_source.read(1, window=window, boundless=True, fill_value=0).astype("float64")
        transform = rasterio.windows.transform(window, green_source.transform)
        crs = green_source.crs
        crop_bounds = rasterio.windows.bounds(window, green_source.transform)
    height, width = green.shape
    nir = _read_matched_asset(
        assets["nir"]["href"], crs, crop_bounds, width, height, Resampling.bilinear
    )
    swir = _read_matched_asset(
        assets["swir16"]["href"], crs, crop_bounds, width, height, Resampling.bilinear
    )
    scl = _read_matched_asset(
        assets["scl"]["href"], crs, crop_bounds, width, height, Resampling.nearest
    ).astype("uint8")

    shape = green.shape
    analysis_mask = geometry_mask(
        [analysis_geometry], out_shape=shape, transform=transform, invert=True
    )
    buffer_masks = {
        float(distance): geometry_mask(
            [pond.buffer(distance)], out_shape=shape, transform=transform, invert=True
        )
        for distance in buffers_m
    }
    core = pond.buffer(-seed_inset_m)
    if core.is_empty:
        core = pond.representative_point().buffer(10)
    core_mask = geometry_mask([core], out_shape=shape, transform=transform, invert=True)
    reflectance_valid = (green > 0) & (nir > 0) & (swir > 0)
    valid = analysis_mask & valid_scl_mask(scl) & reflectance_valid
    analysis_pixels = int(analysis_mask.sum())
    valid_ratio = float(valid.sum() / analysis_pixels) if analysis_pixels else 0.0

    ndwi = normalized_difference(green, nir)
    mndwi = normalized_difference(green, swir)
    candidate = valid & (ndwi > 0) & (mndwi > 0)
    pixel_area_m2 = abs(transform.a * transform.e)
    minimum_pixels = adaptive_minimum_pixels(float(pond.area), pixel_area_m2)
    seed = candidate & core_mask
    seed_valid = bool(seed.any())
    water = (
        connected_to_seed(candidate, seed, minimum_pixels, connectivity=4)
        if seed_valid
        else np.zeros(shape, dtype=bool)
    )
    scl_candidate = valid & (scl == 6)
    scl_seed = scl_candidate & core_mask
    scl_water = (
        connected_to_seed(scl_candidate, scl_seed, minimum_pixels, connectivity=4)
        if scl_seed.any()
        else np.zeros(shape, dtype=bool)
    )
    item_id = feature["id"]
    date = pd.Timestamp(feature["properties"]["datetime"]).tz_convert("Asia/Tokyo")
    quality = "ok" if valid_ratio >= minimum_valid_ratio else "cloud_or_shadow"
    rows = []
    for distance, buffer_mask in buffer_masks.items():
        area_mask = analysis_mask & buffer_mask
        edge = boundary_pixels(area_mask)
        water_values = water & area_mask
        scl_values = scl_water & area_mask
        index_pixels = int(water_values.sum())
        rows.append(
            {
                "item_id": item_id,
                "observation_date": date.tz_localize(None).normalize(),
                "tile": item_id.split("_")[1] if "_" in item_id else "",
                "eo_cloud_cover_pct": float(feature["properties"].get("eo:cloud_cover", np.nan)),
                "local_valid_ratio": round(valid_ratio, 4),
                "quality_flag": quality,
                "buffer_m": distance,
                "open_water_area_m2": index_pixels * pixel_area_m2,
                "scl_water_area_m2": int(scl_values.sum()) * pixel_area_m2,
                "open_water_pixels": index_pixels,
                "seed_valid": seed_valid,
                "buffer_edge_reached": bool(np.any(water_values & edge)),
                "ndwi_median_water": round(float(np.nanmedian(ndwi[water_values])), 4)
                if index_pixels
                else np.nan,
                "mndwi_median_water": round(float(np.nanmedian(mndwi[water_values])), 4)
                if index_pixels
                else np.nan,
                "pixel_area_m2": pixel_area_m2,
                "minimum_component_pixels": minimum_pixels,
                "water_geometry": (
                    mapping(
                        gpd.GeoSeries(
                            [
                                unary_union(
                                    [
                                        geometry_shape(geometry)
                                        for geometry, value in shapes(
                                            water_values.astype("uint8"),
                                            mask=water_values,
                                            transform=transform,
                                        )
                                        if value == 1
                                    ]
                                )
                            ],
                            crs=crs,
                        )
                        .to_crs("EPSG:4326")
                        .iloc[0]
                    )
                    if distance == map_buffer_m and index_pixels
                    else None
                ),
            }
        )
    return rows


def load_nisar_rows(path: Path, buffer_m: float) -> pd.DataFrame:
    data = json.loads(path.read_text())
    rows = [
        row
        for row in data["timeseries"]
        if row["threshold_mode"] == "per_scene_otsu" and float(row["buffer_m"]) == buffer_m
    ]
    frame = pd.DataFrame(rows)
    frame["pair_date"] = pd.to_datetime(frame["pair_date"])
    return frame.sort_values("pair_date")


def validate(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    ponds = gpd.read_file(args.ponds).to_crs("EPSG:4326")
    selected = ponds[ponds[args.id_column].astype(str) == str(args.pond_id)]
    if len(selected) != 1:
        raise ValueError(f"池ID {args.pond_id} が一意に見つかりません")
    pond = selected.geometry.iloc[0]
    search = load_or_search_catalog(args, pond)

    features_by_datetime: dict[str, dict] = {}
    for feature in search["features"]:
        keys = feature.get("assets", {})
        if all(name in keys for name in ("green", "nir", "swir16", "scl")):
            acquisition_date = feature["properties"]["datetime"][:10]
            current = features_by_datetime.get(acquisition_date)
            # スネコスリ溜池は52SGB/52SGCの重複域にある。南端まで余裕のある52SGCを優先する。
            if current is None or ("52SGC" in feature["id"] and "52SGC" not in current["id"]):
                features_by_datetime[acquisition_date] = feature
    features = list(features_by_datetime.values())
    rows: list[dict] = []
    with rasterio.Env(AWS_NO_SIGN_REQUEST="YES", GDAL_HTTP_MULTIRANGE="YES"):
        for feature in sorted(features, key=lambda item: item["properties"]["datetime"]):
            try:
                rows.extend(
                    analyze_item(
                        feature,
                        pond,
                        args.buffers_m,
                        args.seed_inset_m,
                        args.minimum_valid_ratio,
                        args.comparison_buffer_m,
                    )
                )
            except rasterio.errors.RasterioIOError as error:
                print(f"SKIP {feature['id']}: {error}")
    if not rows:
        raise ValueError("解析できるSentinel-2観測がありません")
    frame = pd.DataFrame(rows).sort_values(["observation_date", "buffer_m"])

    comparison_buffer = float(args.comparison_buffer_m)
    optical = frame[frame["buffer_m"] == comparison_buffer].copy()
    nisar = load_nisar_rows(args.nisar_json, comparison_buffer)
    used: set[str] = set()
    comparisons = []
    for _, nisar_row in nisar.iterrows():
        match = choose_nearest_observation(
            nisar_row["pair_date"], optical, args.maximum_match_days, used
        )
        record = {
            "nisar_pair_id": nisar_row["pair_id"],
            "nisar_date": nisar_row["pair_date"].strftime("%Y-%m-%d"),
            "nisar_area_m2": float(nisar_row["open_water_area_m2"]),
            "nisar_quality_flag": nisar_row["pair_quality_flag"],
            "sentinel2_item_id": None,
            "sentinel2_date": None,
            "day_offset": None,
            "sentinel2_area_m2": None,
            "difference_m2": None,
            "difference_pct_of_sentinel2": None,
            "sentinel2_local_valid_ratio": None,
        }
        if match is not None:
            used.add(str(match["item_id"]))
            optical_area = float(match["open_water_area_m2"])
            difference = float(nisar_row["open_water_area_m2"]) - optical_area
            record.update(
                {
                    "sentinel2_item_id": str(match["item_id"]),
                    "sentinel2_date": match["observation_date"].strftime("%Y-%m-%d"),
                    "day_offset": int((match["observation_date"] - nisar_row["pair_date"]).days),
                    "sentinel2_area_m2": optical_area,
                    "difference_m2": difference,
                    "difference_pct_of_sentinel2": round(
                        difference / optical_area * 100, 2
                    )
                    if optical_area
                    else None,
                    "sentinel2_local_valid_ratio": float(match["local_valid_ratio"]),
                }
            )
        comparisons.append(record)

    csv_frame = frame.drop(columns="water_geometry").copy()
    csv_frame["observation_date"] = csv_frame["observation_date"].dt.strftime("%Y-%m-%d")
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_frame.to_csv(args.output_csv, index=False, float_format="%.4f")
    json_frame = frame.copy()
    json_frame["observation_date"] = json_frame["observation_date"].dt.strftime("%Y-%m-%d")
    optical_records = json_frame.replace({np.nan: None}).to_dict(orient="records")
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pond_id": str(args.pond_id),
        "pond_name": str(selected.iloc[0].get("name", "")),
        "algorithm": {
            "source": "Sentinel-2 L2A",
            "classification": "NDWI > 0 and MNDWI > 0",
            "invalid_scl_classes": sorted(INVALID_SCL),
            "connectivity": 4,
            "buffers_m": args.buffers_m,
            "seed_inset_m": args.seed_inset_m,
            "minimum_valid_ratio": args.minimum_valid_ratio,
            "comparison_buffer_m": comparison_buffer,
            "maximum_match_days": args.maximum_match_days,
        },
        "observations": optical_records,
        "comparisons": comparisons,
        "note": "Sentinel-2は光学検証値。雲・雲影をSCLで除外し、NDWIとMNDWIがともに正で中央シードへ接続する水域を集計した。",
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return frame, output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sentinel-2によるNISAR開放水面検証")
    parser.add_argument("--search-json", type=Path, default=DEFAULT_SEARCH)
    parser.add_argument("--stac-url", default=DEFAULT_STAC_URL)
    parser.add_argument("--start", default="2026-06-17")
    parser.add_argument("--end", default="2026-09-23")
    parser.add_argument("--maximum-cloud", type=float, default=50.0)
    parser.add_argument("--ponds", type=Path, default=DEFAULT_PONDS)
    parser.add_argument("--nisar-json", type=Path, default=DEFAULT_NISAR)
    parser.add_argument("--pond-id", default="27")
    parser.add_argument("--id-column", default="simple_id")
    parser.add_argument("--buffers-m", type=float, nargs="+", default=[0.0, 20.0, 50.0])
    parser.add_argument("--seed-inset-m", type=float, default=40.0)
    parser.add_argument("--minimum-valid-ratio", type=float, default=0.95)
    parser.add_argument("--comparison-buffer-m", type=float, default=20.0)
    parser.add_argument("--maximum-match-days", type=int, default=10)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    return parser.parse_args()


if __name__ == "__main__":
    result, payload = validate(parse_args())
    valid = {
        row["item_id"]
        for row in payload["observations"]
        if row["quality_flag"] == "ok" and row["buffer_m"] == 20.0
    }
    print(f"{len(result)}行、品質良好{len(valid)}観測を出力しました")
