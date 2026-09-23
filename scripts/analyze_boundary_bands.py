"""OSM池境界の内外を距離帯に分け、NISAR時系列と画素差分を診断する。"""

from __future__ import annotations

import argparse
import json
import warnings
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask, shapes
from rasterio.windows import from_bounds
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform as transform_geometry

try:
    from scripts.summarize_backscatter import power_statistics, scene_files
except ModuleNotFoundError:  # `python scripts/analyze_boundary_bands.py` での実行用
    from summarize_backscatter import power_statistics, scene_files


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "work" / "nisar_subsets"
DEFAULT_PONDS = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_CATALOG = REPO_ROOT / "data" / "nisar_catalog" / "catalog.csv"
DEFAULT_SCENES = REPO_ROOT / "data" / "derived" / "nisar_scene_quality.csv"
DEFAULT_WEATHER = REPO_ROOT / "data" / "weather" / "kitsuki_daily_precipitation.csv"

BAND_LABELS = {
    "inside_core": "内側40 m以上",
    "inside_20_40": "内側20–40 m",
    "inside_0_20": "内側0–20 m",
    "outside_0_20": "外側0–20 m",
    "outside_20_50": "外側20–50 m",
    "outside_50_100": "外側50–100 m",
}

POND_27_IMAGERY_REVIEW = {
    1: {
        "classification": "open_water",
        "classification_label": "開放水面",
        "review_note": "北側の開放水面内。境界補正ではなく、水面粗度または局所的な水面状態の変化候補。",
    },
    2: {
        "classification": "roadside_shore",
        "classification_label": "道路沿い岸辺",
        "review_note": "西岸の県道沿いで水際と樹木・法面をまたぐ。境界位置の確認候補。",
    },
    3: {
        "classification": "embankment_shore",
        "classification_label": "東岸・堤体側",
        "review_note": "東岸の草地・堤体状地形と水際をまたぐ。負方向変化で、冠水植生消失または乾燥候補。",
    },
    4: {
        "classification": "wooded_shore",
        "classification_label": "樹木下の岸辺",
        "review_note": "南側の樹冠に覆われた水際。Lバンドで検出したい対象と整合する。",
    },
    5: {
        "classification": "wooded_shore",
        "classification_label": "樹木下の岸辺",
        "review_note": "南東側の樹冠と水際の境界。ポリゴン境界の局所確認候補。",
    },
    6: {
        "classification": "wooded_inlet",
        "classification_label": "樹木下の細い入江",
        "review_note": "南西の細い入江と樹冠境界。75%が負方向で、境界形状と植生下冠水の重点確認候補。",
    },
}


def distance_bands(geometry) -> dict[str, object]:
    """重複しない内外距離帯を作る。"""
    inner20 = geometry.buffer(-20)
    inner40 = geometry.buffer(-40)
    buffer20 = geometry.buffer(20)
    buffer50 = geometry.buffer(50)
    buffer100 = geometry.buffer(100)
    return {
        "inside_core": inner40,
        "inside_20_40": inner20.difference(inner40),
        "inside_0_20": geometry.difference(inner20),
        "outside_0_20": buffer20.difference(geometry),
        "outside_20_50": buffer50.difference(buffer20),
        "outside_50_100": buffer100.difference(buffer50),
    }


def rainfall_lookup(frame: pd.DataFrame) -> dict[pd.Timestamp, float]:
    return {
        pd.Timestamp(row.date).normalize(): float(row.precipitation_mm)
        for row in frame.itertuples()
    }


def antecedent_rain(lookup: dict[pd.Timestamp, float], day: pd.Timestamp, days: int) -> float:
    return round(sum(lookup.get(day - timedelta(days=i), 0.0) for i in range(days)), 1)


def local_observation(row: pd.Series, rain: dict[pd.Timestamp, float]) -> dict:
    timestamp = pd.Timestamp(row["begin_time"])
    local = timestamp.tz_convert("Asia/Tokyo")
    day = local.tz_localize(None).normalize()
    return {
        "granule_id": str(row["granule_id"]),
        "orbit_direction": str(row["orbit_direction"]),
        "date": day.strftime("%Y-%m-%d"),
        "time_jst": local.strftime("%Y-%m-%d %H:%M"),
        "rain_7d_mm": antecedent_rain(rain, day, 7),
    }


def geometry_paths(geometry) -> list[list[list[float]]]:
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    return [[[round(x, 2), round(y, 2)] for x, y in polygon.exterior.coords] for polygon in polygons]


def nanmedian_filter(values: np.ndarray, size: int = 3) -> np.ndarray:
    """NaNを無視した正方形移動中央値。SAR差分の孤立スペックルを抑える。"""
    if size < 1 or size % 2 == 0:
        raise ValueError("sizeは正の奇数にしてください")
    radius = size // 2
    padded = np.pad(values, radius, mode="constant", constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(padded, (size, size))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmedian(windows, axis=(-2, -1))


def select_composite_groups(
    observations: pd.DataFrame,
    wet_min_rain_mm: float,
    dry_max_rain_mm: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """降雨量条件で多雨・少雨の複数観測を選ぶ。足りなければ上下2観測を使う。"""
    ordered = observations.sort_values(["rain_7d_mm", "date"])
    wet = ordered[ordered["rain_7d_mm"] >= wet_min_rain_mm]
    dry = ordered[ordered["rain_7d_mm"] <= dry_max_rain_mm]
    if len(wet) < 2:
        wet = ordered.tail(min(2, len(ordered)))
    if len(dry) < 2:
        dry = ordered.head(min(2, len(ordered)))
    return wet.sort_values("date"), dry.sort_values("date")


def composite_info(frame: pd.DataFrame) -> dict:
    observations = frame[["granule_id", "date", "time_jst", "rain_7d_mm"]].to_dict(
        orient="records"
    )
    return {
        "count": len(observations),
        "dates": [item["date"] for item in observations],
        "rain_7d_mm": [float(item["rain_7d_mm"]) for item in observations],
        "observations": observations,
    }


def connected_to_seed(
    candidate: np.ndarray,
    seed: np.ndarray,
    minimum_pixels: int = 3,
) -> tuple[np.ndarray, list[int]]:
    """8近傍成分のうちseedに触れ、最小画素数以上の領域だけを返す。"""
    if candidate.shape != seed.shape:
        raise ValueError("candidateとseedの形状が一致しません")
    height, width = candidate.shape
    visited = np.zeros(candidate.shape, dtype=bool)
    connected = np.zeros(candidate.shape, dtype=bool)
    sizes: list[int] = []
    for row in range(height):
        for column in range(width):
            if not candidate[row, column] or visited[row, column]:
                continue
            stack = [(row, column)]
            visited[row, column] = True
            component: list[tuple[int, int]] = []
            touches_seed = False
            while stack:
                current_row, current_column = stack.pop()
                component.append((current_row, current_column))
                touches_seed = touches_seed or bool(seed[current_row, current_column])
                for row_offset in (-1, 0, 1):
                    for column_offset in (-1, 0, 1):
                        if row_offset == 0 and column_offset == 0:
                            continue
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
                sizes.append(len(component))
                for component_row, component_column in component:
                    connected[component_row, component_column] = True
    return connected, sorted(sizes, reverse=True)


def mask_paths(mask_array: np.ndarray, transform) -> list[list[list[float]]]:
    """True画素をポリゴン化し、Plotly描画用の外周座標を返す。"""
    paths: list[list[list[float]]] = []
    for geometry, value in shapes(
        mask_array.astype("uint8"), mask=mask_array, transform=transform, connectivity=8
    ):
        if value != 1:
            continue
        paths.extend(geometry_paths(shape(geometry).simplify(2.0, preserve_topology=True)))
    return paths


def mask_geometries(mask_array: np.ndarray, transform) -> list[object]:
    """True画素の8近傍成分をポリゴンとして返す。"""
    return [
        shape(geometry)
        for geometry, value in shapes(
            mask_array.astype("uint8"),
            mask=mask_array,
            transform=transform,
            connectivity=8,
        )
        if value == 1
    ]


def component_geojson(
    component_mask: np.ndarray,
    positive_mask: np.ndarray,
    negative_mask: np.ndarray,
    transform,
    crs,
) -> dict:
    """航空画像QA用に、変化成分をWGS84 GeoJSONへ変換する。"""
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    positive = mask_geometries(positive_mask, transform)
    negative = mask_geometries(negative_mask, transform)
    features = []
    for component_id, geometry in enumerate(mask_geometries(component_mask, transform), 1):
        positive_area = sum(geometry.intersection(item).area for item in positive)
        negative_area = sum(geometry.intersection(item).area for item in negative)
        if positive_area >= geometry.area * 0.99:
            sign = "positive"
        elif negative_area >= geometry.area * 0.99:
            sign = "negative"
        else:
            sign = "mixed"
        wgs84 = transform_geometry(transformer.transform, geometry)
        centroid = wgs84.centroid
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "component_id": component_id,
                    "area_m2": round(float(geometry.area), 1),
                    "sign": sign,
                    "positive_share": round(positive_area / geometry.area, 3),
                    "negative_share": round(negative_area / geometry.area, 3),
                    "latitude": round(centroid.y, 7),
                    "longitude": round(centroid.x, 7),
                },
                "geometry": mapping(wgs84),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def analyze(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    scenes = scene_files(args.input_dir)
    if not scenes:
        raise FileNotFoundError(f"HH/HV観測がありません: {args.input_dir}")
    catalog = pd.read_csv(args.catalog, dtype=str).fillna("")
    weather = pd.read_csv(args.weather)
    rain = rainfall_lookup(weather)
    scene_quality = pd.read_csv(args.scene_quality)
    scene_flags = {
        (str(row.granule_id), str(row.layer)): str(row.scene_quality_flag)
        for row in scene_quality.itertuples()
    }
    metadata = {
        str(row["granule_id"]): local_observation(row, rain)
        for _, row in catalog.iterrows()
        if str(row["granule_id"]) in scenes
    }

    first_path = next(iter(next(iter(scenes.values())).values()))
    with rasterio.open(first_path) as source:
        crs = source.crs
        transform = source.transform
        raster_shape = source.shape
    ponds = gpd.read_file(args.ponds).to_crs(crs)
    selected = ponds[ponds[args.id_column].astype(str) == str(args.pond_id)]
    if selected.empty:
        raise ValueError(f"池ID {args.pond_id} がありません")
    pond = selected.iloc[0].geometry
    bands = {key: geom for key, geom in distance_bands(pond).items() if not geom.is_empty}
    band_masks = {
        key: geometry_mask(
            [mapping(geom)], out_shape=raster_shape, transform=transform, invert=True, all_touched=False
        )
        for key, geom in bands.items()
    }

    records: list[dict] = []
    arrays: dict[tuple[str, str], np.ndarray] = {}
    for granule_id, layer_paths in sorted(scenes.items()):
        if granule_id not in metadata:
            continue
        for layer, path in sorted(layer_paths.items()):
            with rasterio.open(path) as source:
                values = source.read(1).astype("float64")
            arrays[(granule_id, layer)] = values
            for band, mask_array in band_masks.items():
                sample = values[mask_array]
                stats = power_statistics(sample)
                record = {
                    "pond_id": str(args.pond_id),
                    **metadata[granule_id],
                    "layer": layer,
                    "band": band,
                    "band_label": BAND_LABELS[band],
                    "scene_flag": scene_flags.get((granule_id, layer), "ok"),
                    "band_area_m2": float(bands[band].area),
                }
                record.update(stats)
                records.append(record)

    frame = pd.DataFrame(records).sort_values(
        ["orbit_direction", "date", "band", "layer"]
    )
    frame["baseline_median_db"] = frame.groupby(
        ["orbit_direction", "layer", "band"]
    )["median_db"].transform(
        lambda values: float(np.nanmedian(values))
    )
    frame["delta_from_baseline_db"] = frame["median_db"] - frame["baseline_median_db"]
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output_csv, index=False, float_format="%.6f")

    comparison = frame[
        (frame["orbit_direction"] == args.comparison_orbit)
        & (frame["layer"] == "HHHH")
        & (frame["band"] == "outside_0_20")
        & (frame["scene_flag"] == "ok")
    ][["granule_id", "date", "time_jst", "rain_7d_mm"]].drop_duplicates()
    wet, dry = select_composite_groups(
        comparison, args.wet_min_rain_mm, args.dry_max_rain_mm
    )

    buffer100 = pond.buffer(100)
    with rasterio.open(first_path) as source:
        window = from_bounds(*buffer100.bounds, transform=source.transform).round_offsets().round_lengths()
        row_slice, col_slice = window.toslices()
        crop_transform = source.window_transform(window)
    crop_mask = geometry_mask(
        [mapping(buffer100)],
        out_shape=(row_slice.stop - row_slice.start, col_slice.stop - col_slice.start),
        transform=crop_transform,
        invert=True,
        all_touched=False,
    )
    grids: dict[str, list] = {}
    difference_arrays: dict[str, np.ndarray] = {}
    changed_fractions: list[dict] = []
    for layer in ("HHHH", "HVHV"):
        def make_composite(rows: pd.DataFrame) -> np.ndarray:
            stack = []
            for granule_id in rows["granule_id"]:
                values = arrays[(str(granule_id), layer)][row_slice, col_slice]
                db = np.full(values.shape, np.nan, dtype="float64")
                valid = crop_mask & np.isfinite(values) & (values > 0)
                db[valid] = 10 * np.log10(values[valid])
                stack.append(db)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                return np.nanmedian(np.stack(stack), axis=0)

        wet_db = make_composite(wet)
        dry_db = make_composite(dry)
        difference = nanmedian_filter(dry_db, 3) - nanmedian_filter(wet_db, 3)
        difference[~crop_mask] = np.nan
        difference_arrays[layer] = difference
        grids[layer] = [
            [None if not np.isfinite(value) else round(float(value), 2) for value in row]
            for row in difference
        ]
        full_difference = np.full(raster_shape, np.nan, dtype="float64")
        full_difference[row_slice, col_slice] = difference
        for band, mask_array in band_masks.items():
            vals = full_difference[mask_array]
            vals = vals[np.isfinite(vals)]
            changed_fractions.append(
                {
                    "layer": layer,
                    "band": band,
                    "band_label": BAND_LABELS[band],
                    "pixels": int(vals.size),
                    "changed_fraction_abs_1_5_db": round(float(np.mean(np.abs(vals) >= 1.5)), 4)
                    if vals.size
                    else None,
                    "median_change_db": round(float(np.median(vals)), 3) if vals.size else None,
                }
            )

    hh_difference = difference_arrays["HHHH"]
    hv_difference = difference_arrays["HVHV"]
    jointly_valid = np.isfinite(hh_difference) & np.isfinite(hv_difference)
    threshold = args.change_threshold_db
    both_changed = jointly_valid & (np.abs(hh_difference) >= threshold) & (
        np.abs(hv_difference) >= threshold
    )
    positive_both = jointly_valid & (hh_difference >= threshold) & (hv_difference >= threshold)
    negative_both = jointly_valid & (hh_difference <= -threshold) & (hv_difference <= -threshold)
    seed_mask = geometry_mask(
        [mapping(pond.buffer(10))],
        out_shape=crop_mask.shape,
        transform=crop_transform,
        invert=True,
        all_touched=True,
    )
    connected_both, component_sizes = connected_to_seed(
        both_changed, seed_mask, args.minimum_component_pixels
    )
    connected_positive, positive_sizes = connected_to_seed(
        positive_both, seed_mask, args.minimum_component_pixels
    )
    connected_negative, negative_sizes = connected_to_seed(
        negative_both, seed_mask, args.minimum_component_pixels
    )
    connected_mixed = connected_both & ~connected_positive & ~connected_negative
    connected_metrics: list[dict] = []
    for band, full_mask in band_masks.items():
        band_mask = full_mask[row_slice, col_slice] & crop_mask
        pixel_count = int(np.sum(band_mask))
        connected_metrics.append(
            {
                "band": band,
                "band_label": BAND_LABELS[band],
                "pixels": pixel_count,
                "connected_both_fraction": round(
                    float(np.sum(connected_both & band_mask) / pixel_count), 4
                )
                if pixel_count
                else None,
                "connected_positive_fraction": round(
                    float(np.sum(connected_positive & band_mask) / pixel_count), 4
                )
                if pixel_count
                else None,
                "connected_negative_fraction": round(
                    float(np.sum(connected_negative & band_mask) / pixel_count), 4
                )
                if pixel_count
                else None,
                "connected_mixed_fraction": round(
                    float(np.sum(connected_mixed & band_mask) / pixel_count), 4
                )
                if pixel_count
                else None,
            }
        )

    height, width = crop_mask.shape
    xs = [round(crop_transform.c + (column + 0.5) * crop_transform.a, 2) for column in range(width)]
    ys = [round(crop_transform.f + (row + 0.5) * crop_transform.e, 2) for row in range(height)]
    summary = (
        frame.groupby(["orbit_direction", "layer", "band", "band_label"], as_index=False)
        .agg(
            median_min_db=("median_db", "min"),
            median_max_db=("median_db", "max"),
            observation_count=("date", "nunique"),
        )
    )
    summary["median_range_db"] = summary["median_max_db"] - summary["median_min_db"]
    fraction_lookup = {
        (item["band"], item["layer"]): item["changed_fraction_abs_1_5_db"]
        for item in changed_fractions
    }
    edge_excess = {
        layer: round(
            float(
                fraction_lookup[("outside_0_20", layer)]
                - fraction_lookup[("outside_50_100", layer)]
            ),
            4,
        )
        for layer in ("HHHH", "HVHV")
    }
    edge_vs_core = {
        layer: round(
            float(
                fraction_lookup[("outside_0_20", layer)]
                - fraction_lookup[("inside_core", layer)]
            ),
            4,
        )
        for layer in ("HHHH", "HVHV")
    }
    if edge_excess["HHHH"] >= 0.10 and edge_excess["HVHV"] >= 0.10:
        assessment = "境界外側に明瞭な変化集中"
    elif (
        edge_vs_core["HHHH"] >= 0.10
        and edge_vs_core["HVHV"] >= 0.10
        and edge_excess["HHHH"] >= 0.03
        and edge_excess["HVHV"] >= 0.03
    ):
        assessment = "境界周辺に変化集中・外側拡張は弱い"
    elif edge_excess["HHHH"] >= 0.05 or edge_excess["HVHV"] >= 0.05:
        assessment = "弱い境界外変化・偏波間の一致を要確認"
    else:
        assessment = "境界外側への明瞭な変化集中なし"
    connected_lookup = {item["band"]: item["connected_both_fraction"] for item in connected_metrics}
    if (
        connected_lookup["inside_core"] == 0
        and connected_lookup["outside_50_100"] == 0
        and connected_lookup["inside_0_20"] > 0
        and connected_lookup["outside_0_20"] > 0
    ):
        connectivity_assessment = "小規模な境界接続変化を検出"
    else:
        connectivity_assessment = "境界接続変化は未確定"
    qa_components = component_geojson(
        connected_both,
        connected_positive,
        connected_negative,
        crop_transform,
        crs,
    )
    if str(args.pond_id) == "27":
        for feature in qa_components["features"]:
            component_id = feature["properties"]["component_id"]
            review = POND_27_IMAGERY_REVIEW.get(component_id)
            if review:
                feature["properties"].update(review)
    wgs84_transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    pond_wgs84 = transform_geometry(wgs84_transformer.transform, pond)
    output = {
        "pond_id": str(args.pond_id),
        "crs": str(crs),
        "comparison": {
            "orbit": args.comparison_orbit.lower(),
            "wet": composite_info(wet),
            "dry": composite_info(dry),
            "difference": "dry_composite_minus_wet_composite_db",
            "temporal_composite": "pixelwise_median",
            "wet_min_rain_mm": args.wet_min_rain_mm,
            "dry_max_rain_mm": args.dry_max_rain_mm,
            "spatial_filter": "3x3_median_approximately_30m",
        },
        "band_labels": BAND_LABELS,
        "band_summary": json.loads(summary.to_json(orient="records")),
        "changed_fractions": changed_fractions,
        "assessment": {
            "label": assessment,
            "outer_0_20_excess_vs_50_100": edge_excess,
            "outer_0_20_excess_vs_inside_core": edge_vs_core,
            "note": "変化画素率を外側0–20 m、池中央、外側50–100 mで比較した一次判定",
        },
        "connectivity": {
            "assessment": connectivity_assessment,
            "threshold_db": threshold,
            "minimum_component_pixels": args.minimum_component_pixels,
            "seed": "pond_polygon_and_10m_outer_buffer",
            "pixel_area_m2": abs(transform.a * transform.e),
            "component_sizes_pixels": component_sizes,
            "component_areas_m2": [
                round(size * abs(transform.a * transform.e), 1) for size in component_sizes
            ],
            "positive_component_sizes_pixels": positive_sizes,
            "negative_component_sizes_pixels": negative_sizes,
            "metrics": connected_metrics,
            "connected_both_paths": mask_paths(connected_both, crop_transform),
            "connected_positive_paths": mask_paths(connected_positive, crop_transform),
            "connected_negative_paths": mask_paths(connected_negative, crop_transform),
            "connected_mixed_paths": mask_paths(connected_mixed, crop_transform),
        },
        "imagery_qa": {
            "status": "preliminary_reviewed",
            "source": "GSI seamless aerial imagery",
            "pond_geometry": mapping(pond_wgs84),
            "components": qa_components,
            "note": "航空画像で地物を目視分類済み。撮影時期はNISAR観測日と一致しないため、土地被覆の照合に限定する。元の池ポリゴンは変更していない。",
        },
        "grid": {"x": xs, "y": ys, "HHHH": grids["HHHH"], "HVHV": grids["HVHV"]},
        "pond_boundary_paths": geometry_paths(pond),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return frame, output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="池境界の内外距離帯をNISARで診断する")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--ponds", type=Path, default=DEFAULT_PONDS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--scene-quality", type=Path, default=DEFAULT_SCENES)
    parser.add_argument("--weather", type=Path, default=DEFAULT_WEATHER)
    parser.add_argument("--pond-id", default="27")
    parser.add_argument("--id-column", default="simple_id")
    parser.add_argument("--comparison-orbit", choices=["ASCENDING", "DESCENDING"], default="ASCENDING")
    parser.add_argument("--wet-min-rain-mm", type=float, default=100.0)
    parser.add_argument("--dry-max-rain-mm", type=float, default=20.0)
    parser.add_argument("--change-threshold-db", type=float, default=1.5)
    parser.add_argument("--minimum-component-pixels", type=int, default=3)
    parser.add_argument(
        "--output-csv", type=Path, default=REPO_ROOT / "data" / "derived" / "pond_27_boundary_bands.csv"
    )
    parser.add_argument(
        "--output-json", type=Path, default=REPO_ROOT / "docs" / "data" / "boundary_27.json"
    )
    return parser.parse_args()


if __name__ == "__main__":
    stats, diagnostic = analyze(parse_args())
    wet_dates = ",".join(diagnostic["comparison"]["wet"]["dates"])
    dry_dates = ",".join(diagnostic["comparison"]["dry"]["dates"])
    print(f"{len(stats)}行 / 多雨 [{wet_dates}] → 少雨 [{dry_dates}]")
