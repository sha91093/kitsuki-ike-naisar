"""昇交・降交の一致判定で池の開放水面積を試算する。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

try:
    from scripts.analyze_boundary_bands import antecedent_rain, rainfall_lookup
    from scripts.summarize_backscatter import scene_files
except ModuleNotFoundError:
    from analyze_boundary_bands import antecedent_rain, rainfall_lookup
    from summarize_backscatter import scene_files


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "work" / "nisar_subsets"
DEFAULT_PONDS = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_CATALOG = REPO_ROOT / "data" / "nisar_catalog" / "catalog.csv"
DEFAULT_WEATHER = REPO_ROOT / "data" / "weather" / "kitsuki_daily_precipitation.csv"
DEFAULT_CSV = REPO_ROOT / "data" / "derived" / "pond_27_open_water_buffers.csv"
DEFAULT_JSON = REPO_ROOT / "docs" / "data" / "open_water_27.json"


def otsu_threshold(values: np.ndarray, bins: int = 128) -> float:
    """外れ値を除いた1次元値からOtsu閾値を返す。"""
    valid = np.asarray(values, dtype="float64")
    valid = valid[np.isfinite(valid)]
    if valid.size < 2:
        raise ValueError("Otsu閾値に必要な有効画素がありません")
    lower, upper = np.percentile(valid, [1, 99])
    clipped = valid[(valid >= lower) & (valid <= upper)]
    if np.allclose(clipped.min(), clipped.max()):
        return float(clipped[0])
    counts, edges = np.histogram(clipped, bins=bins)
    probabilities = counts.astype("float64") / counts.sum()
    centers = (edges[:-1] + edges[1:]) / 2
    weights = np.cumsum(probabilities)
    means = np.cumsum(probabilities * centers)
    total_mean = means[-1]
    denominator = weights * (1 - weights)
    variance = np.divide(
        (total_mean * weights - means) ** 2,
        denominator,
        out=np.zeros_like(denominator),
        where=denominator > 0,
    )
    return float(centers[int(np.argmax(variance))])


def pair_observations(frame: pd.DataFrame, maximum_hours: float = 36.0) -> list[tuple[pd.Series, pd.Series]]:
    """各降交観測へ時間的に最も近い未使用の昇交観測を対応付ける。"""
    observations = frame.copy()
    observations["time"] = pd.to_datetime(observations["begin_time"], utc=True)
    ascending = observations[observations["orbit_direction"] == "ASCENDING"].copy()
    descending = observations[observations["orbit_direction"] == "DESCENDING"].copy()
    used: set[str] = set()
    pairs: list[tuple[pd.Series, pd.Series]] = []
    for _, desc in descending.sort_values("time").iterrows():
        available = ascending[~ascending["granule_id"].isin(used)].copy()
        if available.empty:
            continue
        available["distance"] = (available["time"] - desc["time"]).abs()
        asc = available.sort_values(["distance", "time"]).iloc[0]
        if asc["distance"] <= pd.Timedelta(hours=maximum_hours):
            used.add(str(asc["granule_id"]))
            pairs.append((desc, asc))
    return pairs


def connected_to_seed(
    candidate: np.ndarray,
    seed: np.ndarray,
    minimum_pixels: int,
    connectivity: int = 4,
) -> np.ndarray:
    """seedに接する成分だけを残す。"""
    if connectivity not in (4, 8):
        raise ValueError("connectivityは4または8です")
    if candidate.shape != seed.shape:
        raise ValueError("candidateとseedの形状が一致しません")
    offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if connectivity == 8:
        offsets += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    height, width = candidate.shape
    visited = np.zeros(candidate.shape, dtype=bool)
    result = np.zeros(candidate.shape, dtype=bool)
    for row in range(height):
        for column in range(width):
            if visited[row, column] or not candidate[row, column]:
                continue
            stack = [(row, column)]
            visited[row, column] = True
            component = []
            touches_seed = False
            while stack:
                current_row, current_column = stack.pop()
                component.append((current_row, current_column))
                touches_seed |= bool(seed[current_row, current_column])
                for row_offset, column_offset in offsets:
                    next_row = current_row + row_offset
                    next_column = current_column + column_offset
                    if (
                        0 <= next_row < height
                        and 0 <= next_column < width
                        and candidate[next_row, next_column]
                        and not visited[next_row, next_column]
                    ):
                        visited[next_row, next_column] = True
                        stack.append((next_row, next_column))
            if touches_seed and len(component) >= minimum_pixels:
                for component_row, component_column in component:
                    result[component_row, component_column] = True
    return result


def adaptive_minimum_pixels(pond_area_m2: float, pixel_area_m2: float) -> int:
    """池面積の0.5%を目安に1〜3画素へ制限する。"""
    return int(np.clip(np.ceil(pond_area_m2 * 0.005 / pixel_area_m2), 1, 3))


def boundary_pixels(mask: np.ndarray) -> np.ndarray:
    """4近傍でマスク外に接する内側境界画素。"""
    padded = np.pad(mask, 1, constant_values=False)
    interior = (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    return mask & ~interior


def read_db(path: Path, window) -> np.ndarray:
    with rasterio.open(path) as source:
        values = source.read(1, window=window).astype("float64")
    result = np.full(values.shape, np.nan, dtype="float64")
    valid = np.isfinite(values) & (values > 0)
    result[valid] = 10 * np.log10(values[valid])
    return result


def scene_water_mask(
    layers: dict[str, Path],
    window,
    analysis_mask: np.ndarray,
    thresholds: tuple[float, float] | None = None,
) -> tuple[np.ndarray, dict]:
    hh = read_db(layers["HHHH"], window)
    hv = read_db(layers["HVHV"], window)
    valid = analysis_mask & np.isfinite(hh) & np.isfinite(hv)
    if thresholds is None:
        hh_threshold = otsu_threshold(hh[valid])
        hv_threshold = otsu_threshold(hv[valid])
    else:
        hh_threshold, hv_threshold = thresholds
    water = valid & (hh <= hh_threshold) & (hv <= hv_threshold)
    return water, {
        "hh_threshold_db": round(hh_threshold, 3),
        "hv_threshold_db": round(hv_threshold, 3),
        "valid_pixels": int(valid.sum()),
        "candidate_pixels": int(water.sum()),
    }


def estimate(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    scenes = scene_files(args.input_dir)
    if not scenes:
        raise FileNotFoundError(f"HH/HV観測がありません: {args.input_dir}")
    catalog = pd.read_csv(args.catalog, dtype=str).fillna("")
    catalog = catalog[catalog["granule_id"].isin(scenes)].copy()
    pairs = pair_observations(catalog, args.maximum_pair_hours)
    if not pairs:
        raise ValueError("昇交・降交の近接日ペアがありません")

    first_path = scenes[str(pairs[0][0]["granule_id"])]["HHHH"]
    with rasterio.open(first_path) as source:
        crs = source.crs
        transform = source.transform
        pixel_area_m2 = abs(transform.a * transform.e)
    ponds = gpd.read_file(args.ponds).to_crs(crs)
    selected = ponds[ponds[args.id_column].astype(str) == str(args.pond_id)]
    if len(selected) != 1:
        raise ValueError(f"池ID {args.pond_id} が一意に見つかりません")
    pond = selected.geometry.iloc[0]
    analysis_geometry = pond.buffer(max(args.buffers_m))
    window = from_bounds(*analysis_geometry.bounds, transform).round_offsets().round_lengths()
    crop_transform = rasterio.windows.transform(window, transform)
    shape = (int(window.height), int(window.width))
    analysis_mask = geometry_mask(
        [analysis_geometry], out_shape=shape, transform=crop_transform, invert=True
    )
    buffer_masks = {
        distance: geometry_mask(
            [pond.buffer(distance)], out_shape=shape, transform=crop_transform, invert=True
        )
        for distance in args.buffers_m
    }
    core = pond.buffer(-args.seed_inset_m)
    core_mask = geometry_mask([core], out_shape=shape, transform=crop_transform, invert=True)
    minimum_pixels = adaptive_minimum_pixels(float(pond.area), pixel_area_m2)
    rain = rainfall_lookup(pd.read_csv(args.weather))

    baseline_desc, baseline_asc = pairs[0]
    _, baseline_desc_stats = scene_water_mask(
        scenes[str(baseline_desc["granule_id"])], window, analysis_mask
    )
    _, baseline_asc_stats = scene_water_mask(
        scenes[str(baseline_asc["granule_id"])], window, analysis_mask
    )
    baseline_thresholds = {
        "DESCENDING": (
            baseline_desc_stats["hh_threshold_db"],
            baseline_desc_stats["hv_threshold_db"],
        ),
        "ASCENDING": (
            baseline_asc_stats["hh_threshold_db"],
            baseline_asc_stats["hv_threshold_db"],
        ),
    }

    rows = []
    pair_details = []
    for desc, asc in pairs:
        desc_id = str(desc["granule_id"])
        asc_id = str(asc["granule_id"])
        midpoint = desc["time"] + (asc["time"] - desc["time"]) / 2
        local_day = midpoint.tz_convert("Asia/Tokyo").tz_localize(None).normalize()
        pair_id = f"{desc['time'].date()}/{asc['time'].date()}"
        method_details = {}
        for threshold_mode, thresholds in (
            ("per_scene_otsu", None),
            ("fixed_wet_baseline", baseline_thresholds),
        ):
            desc_thresholds = thresholds["DESCENDING"] if thresholds else None
            asc_thresholds = thresholds["ASCENDING"] if thresholds else None
            desc_water, desc_stats = scene_water_mask(
                scenes[desc_id], window, analysis_mask, desc_thresholds
            )
            asc_water, asc_stats = scene_water_mask(
                scenes[asc_id], window, analysis_mask, asc_thresholds
            )
            consensus = desc_water & asc_water
            union = desc_water | asc_water
            seed_consensus = core_mask & consensus
            seed_union = core_mask & union
            seed_valid = bool(seed_consensus.any())
            confirmed = (
                connected_to_seed(consensus, seed_consensus, minimum_pixels, args.connectivity)
                if seed_valid
                else np.zeros(shape, dtype=bool)
            )
            possible = (
                connected_to_seed(union, seed_union, minimum_pixels, args.connectivity)
                if seed_union.any()
                else np.zeros(shape, dtype=bool)
            )
            uncertain = possible & (desc_water ^ asc_water)
            for distance, mask_array in buffer_masks.items():
                area_mask = mask_array & analysis_mask
                water_pixels = int(np.sum(confirmed & area_mask))
                uncertain_pixels = int(np.sum(uncertain & area_mask))
                edge = boundary_pixels(area_mask)
                total_classified = water_pixels + uncertain_pixels
                agreement_ratio = water_pixels / total_classified if total_classified else 0.0
                rows.append(
                    {
                        "pair_id": pair_id,
                        "pair_date": local_day.strftime("%Y-%m-%d"),
                        "descending_date": desc["time"].tz_convert("Asia/Tokyo").strftime("%Y-%m-%d"),
                        "ascending_date": asc["time"].tz_convert("Asia/Tokyo").strftime("%Y-%m-%d"),
                        "threshold_mode": threshold_mode,
                        "buffer_m": distance,
                        "open_water_area_m2": water_pixels * pixel_area_m2,
                        "uncertain_area_m2": uncertain_pixels * pixel_area_m2,
                        "open_water_pixels": water_pixels,
                        "uncertain_pixels": uncertain_pixels,
                        "orbit_agreement_ratio": round(agreement_ratio, 4),
                        "pair_quality_flag": "ok" if agreement_ratio >= 0.5 else "low_orbit_agreement",
                        "seed_valid": seed_valid,
                        "buffer_edge_reached": bool(np.any(confirmed & edge)),
                        "uncertain_edge_reached": bool(np.any(uncertain & edge)),
                        "rain_7d_mm": antecedent_rain(rain, local_day, 7),
                    }
                )
            method_details[threshold_mode] = {
                "descending": {"granule_id": desc_id, **desc_stats},
                "ascending": {"granule_id": asc_id, **asc_stats},
                "seed_valid": seed_valid,
            }
        pair_details.append(
            {
                "pair_id": pair_id,
                "methods": method_details,
            }
        )

    frame = pd.DataFrame(rows).sort_values(["pair_date", "threshold_mode", "buffer_m"])
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output_csv, index=False, float_format="%.3f")
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pond_id": str(args.pond_id),
        "pond_name": str(selected.iloc[0].get("name", "")),
        "algorithm": {
            "classification": ["per_scene_otsu_hh_and_hv", "fixed_wet_baseline_hh_and_hv"],
            "orbit_consensus": "ascending_and_descending",
            "maximum_pair_hours": args.maximum_pair_hours,
            "connectivity": args.connectivity,
            "buffers_m": args.buffers_m,
            "seed_inset_m": args.seed_inset_m,
            "minimum_component_pixels": minimum_pixels,
            "pixel_area_m2": pixel_area_m2,
            "vegetated_inundation_included": False,
            "low_orbit_agreement_threshold": 0.5,
            "fixed_baseline_pair": f"{baseline_desc['time'].date()}/{baseline_asc['time'].date()}",
        },
        "pairs": pair_details,
        "timeseries": frame.to_dict(orient="records"),
        "note": "開放水面の試算値。片軌道のみの候補はuncertainへ分離し、植生下冠水は含めない。",
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return frame, output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="昇交・降交一致による開放水面積の試算")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--ponds", type=Path, default=DEFAULT_PONDS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--weather", type=Path, default=DEFAULT_WEATHER)
    parser.add_argument("--pond-id", default="27")
    parser.add_argument("--id-column", default="simple_id")
    parser.add_argument("--buffers-m", type=float, nargs="+", default=[0.0, 20.0, 50.0])
    parser.add_argument("--seed-inset-m", type=float, default=40.0)
    parser.add_argument("--maximum-pair-hours", type=float, default=36.0)
    parser.add_argument("--connectivity", type=int, choices=[4, 8], default=4)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    return parser.parse_args()


if __name__ == "__main__":
    result, _ = estimate(parse_args())
    print(f"{len(result)}行を出力しました")
