"""Sentinel-2水域を教師にNISAR HH/HV固定閾値を調整・検証する。"""

from __future__ import annotations

import argparse
import json
import warnings
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask, shapes
from rasterio.windows import from_bounds
from shapely.geometry import mapping, shape as geometry_shape
from shapely.ops import unary_union

try:
    from scripts.estimate_open_water import (
        adaptive_minimum_pixels,
        connected_to_seed,
        otsu_threshold,
        pair_observations,
        read_db,
    )
    from scripts.summarize_backscatter import scene_files
except ModuleNotFoundError:
    from estimate_open_water import (
        adaptive_minimum_pixels,
        connected_to_seed,
        otsu_threshold,
        pair_observations,
        read_db,
    )
    from summarize_backscatter import scene_files


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "work" / "nisar_subsets"
DEFAULT_PONDS = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_CATALOG = REPO_ROOT / "data" / "nisar_catalog" / "catalog.csv"
DEFAULT_SENTINEL = REPO_ROOT / "docs" / "data" / "sentinel2_27.json"
DEFAULT_CSV = REPO_ROOT / "data" / "derived" / "pond_27_threshold_validation.csv"
DEFAULT_JSON = REPO_ROOT / "docs" / "data" / "threshold_27.json"


def classification_metrics(prediction: np.ndarray, truth: np.ndarray, valid: np.ndarray) -> dict:
    """二値水域のIoU・適合率・再現率を返す。"""
    pred = prediction & valid
    target = truth & valid
    tp = int(np.sum(pred & target))
    fp = int(np.sum(pred & ~target))
    fn = int(np.sum(~pred & target))
    union = tp + fp + fn
    return {
        "iou": tp / union if union else 0.0,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "tp_pixels": tp,
        "fp_pixels": fp,
        "fn_pixels": fn,
    }


def threshold_values(start: float, stop: float, step: float) -> np.ndarray:
    """終端を含む浮動小数の閾値列を作る。"""
    return np.round(np.arange(start, stop + step / 2, step), 6)


def nanmedian_filter(values: np.ndarray, size: int = 3) -> np.ndarray:
    """NaNを無視する奇数サイズの移動中央値フィルタ。"""
    if size < 1 or size % 2 == 0:
        raise ValueError("中央値フィルタのsizeは正の奇数です")
    if size == 1:
        return values.copy()
    padding = size // 2
    padded = np.pad(values, padding, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (size, size))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmedian(windows, axis=(-2, -1))


def top_threshold_candidates(
    samples: list[dict],
    orbit_key: str,
    hh_values: np.ndarray,
    hv_values: np.ndarray,
    top_n: int,
) -> list[dict]:
    """単一軌道で平均IoUが高いHH/HV組合せを返す。"""
    candidates = []
    for hh_threshold in hh_values:
        for hv_threshold in hv_values:
            scores = []
            errors = []
            for sample in samples:
                orbit = sample[orbit_key]
                prediction = (
                    orbit["valid"]
                    & (orbit["hh"] <= hh_threshold)
                    & (orbit["hv"] <= hv_threshold)
                )
                metric = classification_metrics(prediction, sample["truth"], sample["analysis"])
                scores.append(metric["iou"])
                errors.append(abs(int(prediction.sum()) - int(sample["truth"].sum())))
            candidates.append(
                {
                    "hh_threshold_db": float(hh_threshold),
                    "hv_threshold_db": float(hv_threshold),
                    "mean_iou": float(np.mean(scores)),
                    "mean_pixel_error": float(np.mean(errors)),
                }
            )
    return sorted(
        candidates,
        key=lambda item: (-item["mean_iou"], item["mean_pixel_error"]),
    )[:top_n]


def classify_orbit(orbit: dict, threshold: dict) -> np.ndarray:
    return (
        orbit["valid"]
        & (orbit["hh"] <= threshold["hh_threshold_db"])
        & (orbit["hv"] <= threshold["hv_threshold_db"])
    )


def connected_consensus(
    sample: dict,
    descending_threshold: dict,
    ascending_threshold: dict,
    minimum_pixels: int,
) -> np.ndarray:
    raw = classify_orbit(sample["descending"], descending_threshold) & classify_orbit(
        sample["ascending"], ascending_threshold
    )
    seed = raw & sample["core"]
    if not seed.any():
        return np.zeros(raw.shape, dtype=bool)
    return connected_to_seed(raw, seed, minimum_pixels, connectivity=4)


def select_joint_thresholds(
    samples: list[dict],
    descending_candidates: list[dict],
    ascending_candidates: list[dict],
    minimum_pixels: int,
) -> tuple[dict, dict, dict]:
    """上位候補の直積から両軌道一致の平均IoUが最大の組を選ぶ。"""
    results = []
    for descending in descending_candidates:
        for ascending in ascending_candidates:
            metrics = []
            area_errors = []
            for sample in samples:
                prediction = connected_consensus(
                    sample, descending, ascending, minimum_pixels
                )
                metric = classification_metrics(prediction, sample["truth"], sample["analysis"])
                metrics.append(metric["iou"])
                area_errors.append(abs(int(prediction.sum()) - int(sample["truth"].sum())))
            results.append(
                {
                    "descending": descending,
                    "ascending": ascending,
                    "mean_iou": float(np.mean(metrics)),
                    "mean_pixel_error": float(np.mean(area_errors)),
                }
            )
    best = sorted(results, key=lambda item: (-item["mean_iou"], item["mean_pixel_error"]))[0]
    return best["descending"], best["ascending"], best


def classify_temporal_orbit(
    orbit: dict,
    absolute_threshold: dict,
    change_threshold: dict,
) -> np.ndarray:
    """絶対値水域のうち多雨期からの増加量が上限以下の画素を残す。"""
    return (
        classify_orbit(orbit, absolute_threshold)
        & orbit["delta_valid"]
        & (orbit["delta_hh"] <= change_threshold["delta_hh_max_db"])
        & (orbit["delta_hv"] <= change_threshold["delta_hv_max_db"])
    )


def top_temporal_candidates(
    samples: list[dict],
    orbit_key: str,
    absolute_threshold: dict,
    delta_values: np.ndarray,
    top_n: int,
) -> list[dict]:
    """単一軌道で平均IoUが高いΔHH/ΔHV上限を返す。"""
    candidates = []
    for delta_hh in delta_values:
        for delta_hv in delta_values:
            scores = []
            errors = []
            threshold = {
                "delta_hh_max_db": float(delta_hh),
                "delta_hv_max_db": float(delta_hv),
            }
            for sample in samples:
                prediction = classify_temporal_orbit(
                    sample[orbit_key], absolute_threshold, threshold
                )
                metric = classification_metrics(prediction, sample["truth"], sample["analysis"])
                scores.append(metric["iou"])
                errors.append(abs(int(prediction.sum()) - int(sample["truth"].sum())))
            candidates.append(
                {
                    **threshold,
                    "mean_iou": float(np.mean(scores)),
                    "mean_pixel_error": float(np.mean(errors)),
                }
            )
    return sorted(
        candidates,
        key=lambda item: (-item["mean_iou"], item["mean_pixel_error"]),
    )[:top_n]


def connected_temporal_consensus(
    sample: dict,
    descending_absolute: dict,
    ascending_absolute: dict,
    descending_change: dict,
    ascending_change: dict,
    minimum_pixels: int,
) -> np.ndarray:
    raw = classify_temporal_orbit(
        sample["descending"], descending_absolute, descending_change
    ) & classify_temporal_orbit(sample["ascending"], ascending_absolute, ascending_change)
    seed = raw & sample["core"]
    if not seed.any():
        return np.zeros(raw.shape, dtype=bool)
    return connected_to_seed(raw, seed, minimum_pixels, connectivity=4)


def select_joint_temporal_thresholds(
    samples: list[dict],
    descending_absolute: dict,
    ascending_absolute: dict,
    descending_candidates: list[dict],
    ascending_candidates: list[dict],
    minimum_pixels: int,
) -> tuple[dict, dict, dict]:
    results = []
    for descending in descending_candidates:
        for ascending in ascending_candidates:
            scores = []
            errors = []
            for sample in samples:
                prediction = connected_temporal_consensus(
                    sample,
                    descending_absolute,
                    ascending_absolute,
                    descending,
                    ascending,
                    minimum_pixels,
                )
                metric = classification_metrics(prediction, sample["truth"], sample["analysis"])
                scores.append(metric["iou"])
                errors.append(abs(int(prediction.sum()) - int(sample["truth"].sum())))
            results.append(
                {
                    "descending": descending,
                    "ascending": ascending,
                    "mean_iou": float(np.mean(scores)),
                    "mean_pixel_error": float(np.mean(errors)),
                }
            )
    best = sorted(results, key=lambda item: (-item["mean_iou"], item["mean_pixel_error"]))[0]
    return best["descending"], best["ascending"], best


def mask_geometry(mask: np.ndarray, transform, crs) -> dict | None:
    polygons = [
        geometry_shape(geometry)
        for geometry, value in shapes(mask.astype("uint8"), mask=mask, transform=transform)
        if value == 1
    ]
    if not polygons:
        return None
    merged = unary_union(polygons)
    projected = gpd.GeoSeries([merged], crs=crs).to_crs("EPSG:4326").iloc[0]
    return mapping(projected)


def region_statistics(values: np.ndarray, region: np.ndarray) -> dict:
    selected = values[region & np.isfinite(values)]
    if selected.size == 0:
        return {"pixels": 0, "p25_db": None, "median_db": None, "p75_db": None}
    p25, median, p75 = np.percentile(selected, [25, 50, 75])
    return {
        "pixels": int(selected.size),
        "p25_db": round(float(p25), 3),
        "median_db": round(float(median), 3),
        "p75_db": round(float(p75), 3),
    }


def find_optical_geometry(sentinel: dict, date: str, buffer_m: float) -> dict:
    matches = [
        row
        for row in sentinel["observations"]
        if row["observation_date"] == date
        and float(row["buffer_m"]) == buffer_m
        and row["quality_flag"] == "ok"
        and row.get("water_geometry")
    ]
    if len(matches) != 1:
        raise ValueError(f"Sentinel-2水域が一意に見つかりません: {date}")
    return matches[0]["water_geometry"]


def tune(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    scenes = scene_files(args.input_dir)
    if not scenes:
        raise FileNotFoundError(f"HH/HV観測がありません: {args.input_dir}")
    catalog = pd.read_csv(args.catalog, dtype=str).fillna("")
    catalog = catalog[catalog["granule_id"].isin(scenes)].copy()
    pairs = pair_observations(catalog, args.maximum_pair_hours)
    pair_by_id = {f"{desc['time'].date()}/{asc['time'].date()}": (desc, asc) for desc, asc in pairs}
    sentinel = json.loads(args.sentinel_json.read_text())
    comparisons = {
        row["sentinel2_date"]: row
        for row in sentinel["comparisons"]
        if row.get("sentinel2_date")
    }
    selected_dates = list(dict.fromkeys(args.calibration_dates + args.validation_dates))
    selected_comparisons = [comparisons[date] for date in selected_dates]

    first_pair = pair_by_id[selected_comparisons[0]["nisar_pair_id"]]
    first_path = scenes[str(first_pair[0]["granule_id"])]["HHHH"]
    with rasterio.open(first_path) as source:
        crs = source.crs
        transform = source.transform
        pixel_area_m2 = abs(transform.a * transform.e)
    ponds = gpd.read_file(args.ponds).to_crs(crs)
    selected = ponds[ponds[args.id_column].astype(str) == str(args.pond_id)]
    if len(selected) != 1:
        raise ValueError(f"池ID {args.pond_id} が一意に見つかりません")
    pond = selected.geometry.iloc[0]
    analysis_geometry = pond.buffer(args.buffer_m)
    window = from_bounds(*analysis_geometry.bounds, transform).round_offsets().round_lengths()
    crop_transform = rasterio.windows.transform(window, transform)
    raster_shape = (int(window.height), int(window.width))
    analysis_mask = geometry_mask(
        [analysis_geometry], out_shape=raster_shape, transform=crop_transform, invert=True
    )
    core = pond.buffer(-args.seed_inset_m)
    core_mask = geometry_mask([core], out_shape=raster_shape, transform=crop_transform, invert=True)
    minimum_pixels = adaptive_minimum_pixels(float(pond.area), pixel_area_m2)

    samples = []
    for optical_date in selected_dates:
        comparison = comparisons[optical_date]
        desc, asc = pair_by_id[comparison["nisar_pair_id"]]
        truth_geometry = gpd.GeoSeries(
            [geometry_shape(find_optical_geometry(sentinel, optical_date, args.buffer_m))],
            crs="EPSG:4326",
        ).to_crs(crs).iloc[0]
        truth = geometry_mask(
            [truth_geometry], out_shape=raster_shape, transform=crop_transform, invert=True
        ) & analysis_mask
        sample = {
            "optical_date": optical_date,
            "nisar_date": comparison["nisar_date"],
            "pair_id": comparison["nisar_pair_id"],
            "truth": truth,
            "analysis": analysis_mask,
            "core": core_mask,
        }
        for key, row in (("descending", desc), ("ascending", asc)):
            layers = scenes[str(row["granule_id"])]
            hh = read_db(layers["HHHH"], window)
            hv = read_db(layers["HVHV"], window)
            sample[key] = {
                "hh": hh,
                "hv": hv,
                "hh_smooth": nanmedian_filter(hh, args.temporal_median_size),
                "hv_smooth": nanmedian_filter(hv, args.temporal_median_size),
                "valid": analysis_mask & np.isfinite(hh) & np.isfinite(hv),
                "granule_id": str(row["granule_id"]),
            }
        samples.append(sample)

    calibration = [sample for sample in samples if sample["optical_date"] in args.calibration_dates]
    hh_values = threshold_values(args.hh_min, args.hh_max, args.step_db)
    hv_values = threshold_values(args.hv_min, args.hv_max, args.step_db)
    descending_candidates = top_threshold_candidates(
        calibration, "descending", hh_values, hv_values, args.top_candidates
    )
    ascending_candidates = top_threshold_candidates(
        calibration, "ascending", hh_values, hv_values, args.top_candidates
    )
    desc_threshold, asc_threshold, joint = select_joint_thresholds(
        calibration, descending_candidates, ascending_candidates, minimum_pixels
    )

    wet_sample = next(sample for sample in samples if sample["optical_date"] == args.calibration_dates[0])
    for sample in samples:
        for orbit in ("descending", "ascending"):
            sample[orbit]["delta_hh"] = (
                sample[orbit]["hh_smooth"] - wet_sample[orbit]["hh_smooth"]
            )
            sample[orbit]["delta_hv"] = (
                sample[orbit]["hv_smooth"] - wet_sample[orbit]["hv_smooth"]
            )
            sample[orbit]["delta_valid"] = sample[orbit]["valid"] & wet_sample[orbit]["valid"]
    delta_values = threshold_values(args.delta_min, args.delta_max, args.step_db)
    descending_change_candidates = top_temporal_candidates(
        calibration, "descending", desc_threshold, delta_values, args.top_candidates
    )
    ascending_change_candidates = top_temporal_candidates(
        calibration, "ascending", asc_threshold, delta_values, args.top_candidates
    )
    desc_change, asc_change, temporal_joint = select_joint_temporal_thresholds(
        calibration,
        desc_threshold,
        asc_threshold,
        descending_change_candidates,
        ascending_change_candidates,
        minimum_pixels,
    )
    drought_sample = next(sample for sample in samples if sample["optical_date"] == args.calibration_dates[1])
    diagnostic_regions = {
        "persistent_optical_water": wet_sample["truth"] & drought_sample["truth"],
        "optical_drawdown_zone": wet_sample["truth"] & ~drought_sample["truth"] & analysis_mask,
    }
    drought_distributions = {}
    for region_name, region in diagnostic_regions.items():
        drought_distributions[region_name] = {
            orbit: {
                band: region_statistics(drought_sample[orbit][band], region)
                for band in ("hh", "hv")
            }
            for orbit in ("descending", "ascending")
        }

    rows = []
    map_features = []
    for sample in samples:
        tuned = connected_consensus(sample, desc_threshold, asc_threshold, minimum_pixels)
        temporal = connected_temporal_consensus(
            sample,
            desc_threshold,
            asc_threshold,
            desc_change,
            asc_change,
            minimum_pixels,
        )
        otsu_thresholds = {}
        orbit_masks = []
        for orbit_key in ("descending", "ascending"):
            orbit = sample[orbit_key]
            hh_threshold = otsu_threshold(orbit["hh"][orbit["valid"]])
            hv_threshold = otsu_threshold(orbit["hv"][orbit["valid"]])
            otsu_thresholds[orbit_key] = {
                "hh_threshold_db": hh_threshold,
                "hv_threshold_db": hv_threshold,
            }
            orbit_masks.append(
                orbit["valid"] & (orbit["hh"] <= hh_threshold) & (orbit["hv"] <= hv_threshold)
            )
        raw_otsu = orbit_masks[0] & orbit_masks[1]
        otsu_seed = raw_otsu & core_mask
        otsu = (
            connected_to_seed(raw_otsu, otsu_seed, minimum_pixels, connectivity=4)
            if otsu_seed.any()
            else np.zeros(raster_shape, dtype=bool)
        )
        split = "calibration" if sample["optical_date"] in args.calibration_dates else "validation"
        for method, prediction in (
            ("temporal_change", temporal),
            ("tuned_fixed", tuned),
            ("per_scene_otsu", otsu),
        ):
            metric = classification_metrics(prediction, sample["truth"], analysis_mask)
            rows.append(
                {
                    "split": split,
                    "optical_date": sample["optical_date"],
                    "nisar_date": sample["nisar_date"],
                    "pair_id": sample["pair_id"],
                    "method": method,
                    "water_area_m2": int(prediction.sum()) * pixel_area_m2,
                    "optical_area_m2": int(sample["truth"].sum()) * pixel_area_m2,
                    "area_error_m2": (int(prediction.sum()) - int(sample["truth"].sum()))
                    * pixel_area_m2,
                    **{name: round(value, 4) if isinstance(value, float) else value for name, value in metric.items()},
                }
            )
        tp = temporal & sample["truth"]
        fp = temporal & ~sample["truth"] & analysis_mask
        fn = ~temporal & sample["truth"] & analysis_mask
        map_features.append(
            {
                "optical_date": sample["optical_date"],
                "nisar_date": sample["nisar_date"],
                "split": split,
                "true_positive": mask_geometry(tp, crop_transform, crs),
                "false_positive": mask_geometry(fp, crop_transform, crs),
                "false_negative": mask_geometry(fn, crop_transform, crs),
            }
        )

    frame = pd.DataFrame(rows).sort_values(["optical_date", "method"])
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output_csv, index=False, float_format="%.4f")
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pond_id": str(args.pond_id),
        "pond_name": str(selected.iloc[0].get("name", "")),
        "search": {
            "step_db": args.step_db,
            "hh_range_db": [args.hh_min, args.hh_max],
            "hv_range_db": [args.hv_min, args.hv_max],
            "calibration_dates": args.calibration_dates,
            "validation_dates": args.validation_dates,
            "top_candidates_per_orbit": args.top_candidates,
            "buffer_m": args.buffer_m,
            "connectivity": 4,
            "minimum_component_pixels": minimum_pixels,
            "temporal_median_size": args.temporal_median_size,
            "delta_range_db": [args.delta_min, args.delta_max],
        },
        "selected_thresholds": {
            "descending": desc_threshold,
            "ascending": asc_threshold,
            "calibration_joint_mean_iou": joint["mean_iou"],
        },
        "selected_temporal_thresholds": {
            "descending": desc_change,
            "ascending": asc_change,
            "calibration_joint_mean_iou": temporal_joint["mean_iou"],
            "baseline_optical_date": args.calibration_dates[0],
        },
        "drought_feature_distributions": drought_distributions,
        "results": frame.to_dict(orient="records"),
        "map_features": map_features,
        "note": "6/29と8/23を調整に使用し、8/13と9/22は閾値選択に使わず検証した。",
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return frame, output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sentinel-2教師によるNISAR閾値探索")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--ponds", type=Path, default=DEFAULT_PONDS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--sentinel-json", type=Path, default=DEFAULT_SENTINEL)
    parser.add_argument("--pond-id", default="27")
    parser.add_argument("--id-column", default="simple_id")
    parser.add_argument("--buffer-m", type=float, default=20.0)
    parser.add_argument("--seed-inset-m", type=float, default=40.0)
    parser.add_argument("--maximum-pair-hours", type=float, default=36.0)
    parser.add_argument("--calibration-dates", nargs="+", default=["2026-06-29", "2026-08-23"])
    parser.add_argument("--validation-dates", nargs="+", default=["2026-08-13", "2026-09-22"])
    parser.add_argument("--hh-min", type=float, default=-18.0)
    parser.add_argument("--hh-max", type=float, default=-3.0)
    parser.add_argument("--hv-min", type=float, default=-25.0)
    parser.add_argument("--hv-max", type=float, default=-8.0)
    parser.add_argument("--step-db", type=float, default=0.25)
    parser.add_argument("--delta-min", type=float, default=-4.0)
    parser.add_argument("--delta-max", type=float, default=8.0)
    parser.add_argument("--temporal-median-size", type=int, default=3)
    parser.add_argument("--top-candidates", type=int, default=25)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    return parser.parse_args()


if __name__ == "__main__":
    result, payload = tune(parse_args())
    print(json.dumps(payload["selected_thresholds"], ensure_ascii=False, indent=2))
    print(result.to_string(index=False))
