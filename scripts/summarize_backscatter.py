"""池の全体・岸辺・中央部ごとにNISAR HH/HV後方散乱を集計する。

この段階では水域を分類しない。まず時系列の変化がどこに現れるかを確認し、
渇水時の樹木周辺・岸辺の変動を捉えられるか診断するための集計である。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.mask import mask


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = REPO_ROOT / "work" / "nisar_subsets"
DEFAULT_GEOJSON = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_CATALOG = REPO_ROOT / "data" / "nisar_catalog" / "catalog.csv"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "derived" / "nisar_backscatter_timeseries.csv"
DEFAULT_SUMMARY_OUTPUT = REPO_ROOT / "data" / "derived" / "nisar_backscatter_variability.csv"
DEFAULT_SCENE_OUTPUT = REPO_ROOT / "data" / "derived" / "nisar_scene_quality.csv"
LAYERS = ("HHHH", "HVHV")


def power_statistics(values: np.ndarray) -> dict[str, float | int]:
    """線形パワーをdBへ変換して、外れ値に強い分位点を返す。"""
    array = np.asarray(values, dtype="float64")
    finite = array[np.isfinite(array)]
    positive = finite[finite > 0]
    result: dict[str, float | int] = {
        "sampled_pixels": int(finite.size),
        "positive_pixels": int(positive.size),
        "positive_ratio": float(positive.size / finite.size) if finite.size else 0.0,
    }
    names = ("min_db", "p10_db", "p25_db", "median_db", "p75_db", "p90_db", "max_db")
    if positive.size == 0:
        result.update({name: np.nan for name in names})
        result["mean_db"] = np.nan
        return result

    db = 10.0 * np.log10(positive)
    quantiles = np.percentile(db, [0, 10, 25, 50, 75, 90, 100])
    result.update(dict(zip(names, (float(value) for value in quantiles), strict=True)))
    result["mean_db"] = float(np.mean(db))
    return result


def pond_zones(
    ponds: gpd.GeoDataFrame,
    shoreline_width_m: float,
    minimum_zone_area_m2: float = 0.0,
) -> list[dict[str, object]]:
    """池全体、内側岸辺帯、中央部の解析ジオメトリを作る。"""
    if shoreline_width_m <= 0:
        raise ValueError("--shoreline-width-m は0より大きくしてください")
    rows: list[dict[str, object]] = []
    for _, pond in ponds.iterrows():
        geometry = pond.geometry
        if geometry is None or geometry.is_empty:
            continue
        core = geometry.buffer(-shoreline_width_m)
        zones = [("whole", geometry)]
        if core.is_empty or core.area < minimum_zone_area_m2:
            zones.append(("shoreline", geometry))
        else:
            zones.extend([("shoreline", geometry.difference(core)), ("core", core)])
        for zone, zone_geometry in zones:
            if zone_geometry.is_empty:
                continue
            rows.append(
                {
                    "pond_id": str(pond["simple_id"]),
                    "pond_name": str(pond.get("name", "")),
                    "zone": zone,
                    "geometry": zone_geometry,
                    "geometry_area_m2": float(zone_geometry.area),
                }
            )
    return rows


def scene_files(input_dir: Path) -> dict[str, dict[str, Path]]:
    """granule_idごとにHH/HV GeoTIFFを対応付ける。"""
    scenes: dict[str, dict[str, Path]] = {}
    for path in sorted(input_dir.rglob("frequencyA_*.tif")):
        layer = path.stem.removeprefix("frequencyA_")
        if layer in LAYERS:
            scenes.setdefault(path.parent.name, {})[layer] = path
    return {key: value for key, value in scenes.items() if set(value) == set(LAYERS)}


def scene_quality(frame: pd.DataFrame, review_threshold_db: float) -> pd.DataFrame:
    """全池に共通するシーン単位の急変を検出する。"""
    shoreline = frame[frame["zone"] == "shoreline"]
    quality = (
        shoreline.groupby(
            ["granule_id", "observation_time", "observation_date", "orbit_direction", "layer"],
            as_index=False,
        )
        .agg(
            pond_count=("pond_id", "nunique"),
            scene_median_db=("median_db", "median"),
            scene_p10_db=("median_db", lambda values: values.quantile(0.10)),
            scene_p90_db=("median_db", lambda values: values.quantile(0.90)),
        )
    )
    quality["orbit_layer_reference_db"] = quality.groupby(
        ["orbit_direction", "layer"]
    )["scene_median_db"].transform("median")
    quality["scene_shift_db"] = (
        quality["scene_median_db"] - quality["orbit_layer_reference_db"]
    )
    quality["scene_quality_flag"] = np.where(
        quality["scene_shift_db"].abs() > review_threshold_db, "review", "ok"
    )
    return quality.sort_values(["observation_time", "layer"])


def summarize(args: argparse.Namespace) -> pd.DataFrame:
    scenes = scene_files(args.input_dir)
    if not scenes:
        raise FileNotFoundError(f"HH/HVがそろった観測がありません: {args.input_dir}")

    catalog = pd.read_csv(args.catalog, dtype=str).fillna("")
    catalog_by_granule = catalog.set_index("granule_id").to_dict(orient="index")
    ponds = gpd.read_file(args.geojson)
    if ponds.crs is None or "simple_id" not in ponds.columns:
        raise ValueError("池GeoJSONにはCRSとsimple_idカラムが必要です")

    first_path = next(iter(next(iter(scenes.values())).values()))
    with rasterio.open(first_path) as first:
        raster_crs = first.crs
        minimum_zone_area_m2 = abs(first.transform.a * first.transform.e)
    if raster_crs is None:
        raise ValueError("GeoTIFFにCRSがありません")
    projected_ponds = ponds.to_crs(raster_crs)
    zones = pond_zones(projected_ponds, args.shoreline_width_m, minimum_zone_area_m2)

    records: list[dict[str, object]] = []
    for granule_id, layers in sorted(scenes.items()):
        metadata = catalog_by_granule.get(granule_id, {})
        for layer, path in sorted(layers.items()):
            with rasterio.open(path) as source:
                if source.crs != raster_crs:
                    raise ValueError(f"CRSが混在しています: {path}")
                pixel_area_m2 = abs(source.transform.a * source.transform.e)
                for zone in zones:
                    clipped, _ = mask(
                        source,
                        [zone["geometry"]],
                        indexes=1,
                        crop=True,
                        filled=False,
                        all_touched=False,
                    )
                    values = clipped.compressed()
                    record = {
                        "pond_id": zone["pond_id"],
                        "pond_name": zone["pond_name"],
                        "granule_id": granule_id,
                        "observation_time": metadata.get("begin_time", ""),
                        "observation_date": str(metadata.get("begin_time", ""))[:10],
                        "orbit_direction": metadata.get("orbit_direction", ""),
                        "layer": layer,
                        "zone": zone["zone"],
                        "geometry_area_m2": zone["geometry_area_m2"],
                        "pixel_area_m2": pixel_area_m2,
                    }
                    record.update(power_statistics(values))
                    records.append(record)

    result = pd.DataFrame.from_records(records).sort_values(
        ["observation_time", "orbit_direction", "pond_id", "zone", "layer"]
    )
    quality = scene_quality(result, args.scene_shift_review_db)
    result = result.merge(
        quality[["granule_id", "layer", "scene_shift_db", "scene_quality_flag"]],
        on=["granule_id", "layer"],
        how="left",
        validate="many_to_one",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False, float_format="%.6f")
    args.scene_output.parent.mkdir(parents=True, exist_ok=True)
    quality.to_csv(args.scene_output, index=False, float_format="%.6f")
    usable = result[result["scene_quality_flag"] == "ok"]
    variability = (
        usable.groupby(
            ["pond_id", "pond_name", "orbit_direction", "layer", "zone"],
            as_index=False,
        )
        .agg(
            observation_count=("observation_date", "nunique"),
            first_observation=("observation_date", "min"),
            last_observation=("observation_date", "max"),
            median_min_db=("median_db", "min"),
            median_max_db=("median_db", "max"),
            median_mean_db=("median_db", "mean"),
            median_std_db=("median_db", "std"),
        )
    )
    variability["median_range_db"] = (
        variability["median_max_db"] - variability["median_min_db"]
    )
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    variability.to_csv(args.summary_output, index=False, float_format="%.6f")
    metadata_path = args.output.with_suffix(".metadata.json")
    metadata_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "input_dir": str(args.input_dir),
                "observations": len(scenes),
                "ponds": int(len(projected_ponds)),
                "shoreline_width_m": args.shoreline_width_m,
                "rows": int(len(result)),
                "variability_rows": int(len(variability)),
                "scene_shift_review_db": args.scene_shift_review_db,
                "review_scene_layers": int((quality["scene_quality_flag"] == "review").sum()),
                "note": "診断用の後方散乱統計。水面積の推定値ではない。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="池ごとのNISAR HH/HV後方散乱を時系列集計する")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--geojson", type=Path, default=DEFAULT_GEOJSON)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--scene-output", type=Path, default=DEFAULT_SCENE_OUTPUT)
    parser.add_argument("--shoreline-width-m", type=float, default=50.0)
    parser.add_argument("--scene-shift-review-db", type=float, default=3.0)
    return parser.parse_args()


if __name__ == "__main__":
    frame = summarize(parse_args())
    print(f"{len(frame)}行を出力しました")
