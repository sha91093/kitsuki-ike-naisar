"""GeoJSONの池ポリゴンを使ってNISAR L2 GCOVを検索する。

このスクリプトは画像をダウンロードしない。CMR/Earthdataの検索結果だけを
CSV/JSONへ保存し、杵築市で利用できる観測日・プロダクトを確認する。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import earthaccess
import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from shapely.geometry.polygon import orient


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GEOJSON = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "nisar_catalog"
COLLECTION = "NISAR_L2_GCOV_PROVISIONAL_V1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_search_aoi(geojson: Path, id_column: str, buffer_m: float):
    """池ポリゴンを読み込み、バッファー付き検索領域をWGS84で返す。"""
    if not geojson.exists():
        raise FileNotFoundError(
            f"GeoJSONが見つかりません: {geojson}\n"
            "池ポリゴンを data/static/kitsuki_ponds_final.geojson に配置してください。"
        )

    ponds = gpd.read_file(geojson)
    if ponds.empty:
        raise ValueError(f"GeoJSONに地物がありません: {geojson}")
    if ponds.crs is None:
        raise ValueError("GeoJSONに座標参照系（CRS）が設定されていません")
    if id_column not in ponds.columns:
        raise ValueError(
            f"池IDカラム '{id_column}' がありません。利用可能: {list(ponds.columns)}"
        )

    ponds = ponds[ponds.geometry.notna() & ~ponds.geometry.is_empty].copy()
    if ponds.empty:
        raise ValueError("有効な池ポリゴンがありません")
    if not ponds.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("GeoJSONにはPolygonまたはMultiPolygonが必要です")

    # 距離バッファーは地域に適したUTM座標系で計算する。
    metric_crs = ponds.estimate_utm_crs()
    if metric_crs is None:
        raise ValueError("バッファー計算用の投影座標系を決定できません")
    buffered = ponds.to_crs(metric_crs).geometry.buffer(buffer_m)
    aoi_metric = buffered.union_all()
    aoi = gpd.GeoSeries([aoi_metric], crs=metric_crs).to_crs("EPSG:4326").iloc[0]
    return ponds, aoi


def polygon_coordinates(geometry) -> list[tuple[float, float]]:
    """CMR検索用に単一の外周座標列を作る。

    分散した複数池を確実に包含するため凸包を使い、点数を抑える。
    """
    hull = orient(geometry.convex_hull, sign=1.0)
    coords = list(hull.exterior.coords)
    if len(coords) > 300:
        hull = orient(hull.simplify(0.0001, preserve_topology=True), sign=1.0)
        coords = list(hull.exterior.coords)
    return [(float(x), float(y)) for x, y in coords]


def _additional_attributes(umm: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in umm.get("AdditionalAttributes", []) or []:
        name = str(item.get("Name", "")).strip()
        raw_values = item.get("Values", []) or []
        if name:
            values[name] = ",".join(str(v) for v in raw_values)
    return values


def _range_time(umm: dict[str, Any]) -> tuple[str, str]:
    temporal = umm.get("TemporalExtent", {}) or {}
    value = temporal.get("RangeDateTime", {}) or {}
    return value.get("BeginningDateTime", ""), value.get("EndingDateTime", "")


def result_to_row(result: Any) -> dict[str, Any]:
    """earthaccessの検索結果を安定した表形式へ変換する。"""
    record = dict(result)
    meta = record.get("meta", {}) or {}
    umm = record.get("umm", {}) or {}
    attrs = _additional_attributes(umm)
    begin, end = _range_time(umm)

    links: list[str] = []
    try:
        links = list(result.data_links())
    except Exception:
        for item in umm.get("RelatedUrls", []) or []:
            url = item.get("URL")
            if url:
                links.append(url)

    granule_id = (
        umm.get("GranuleUR")
        or meta.get("native-id")
        or meta.get("concept-id")
        or ""
    )
    return {
        "granule_id": granule_id,
        "concept_id": meta.get("concept-id", ""),
        "begin_time": begin,
        "end_time": end,
        "orbit_direction": attrs.get("ASCENDING_DESCENDING", attrs.get("ORBIT_DIRECTION", "")),
        "track_number": attrs.get("TRACK_NUMBER", ""),
        "frame_number": attrs.get("FRAME_NUMBER", ""),
        "frequency_a_polarization": attrs.get(
            "FREQUENCY_A_POLARIZATION_CONCAT",
            attrs.get("FREQUENCY_A_POLARIZATION", ""),
        ),
        "frequency_a_bandwidth_mhz": attrs.get("FREQUENCY_A_RANGE_BANDWIDTH", ""),
        "frequency_b_polarization": attrs.get(
            "FREQUENCY_B_POLARIZATION_CONCAT",
            attrs.get("FREQUENCY_B_POLARIZATION", ""),
        ),
        "frequency_b_bandwidth_mhz": attrs.get("FREQUENCY_B_RANGE_BANDWIDTH", ""),
        "processing_level": attrs.get("PROCESSING_LEVEL", ""),
        "product_version": attrs.get("PRODUCT_VERSION", ""),
        "size_mb": umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation", [{}])[0].get("Size", "")
        if umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation")
        else "",
        "download_url": next((url for url in links if url.endswith((".h5", ".hdf5"))), links[0] if links else ""),
    }


def authenticate_if_configured() -> None:
    if os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD"):
        earthaccess.login(strategy="environment", persist=False)
        logger.info("Earthdata Loginの環境変数で認証しました")
    else:
        logger.info("認証情報なしで公開メタデータを検索します")


def search(aoi, start: str, end: str, count: int) -> list[Any]:
    coords = polygon_coordinates(aoi)
    logger.info("NISAR GCOVを検索: %s ～ %s", start, end)
    return list(
        earthaccess.search_data(
            short_name=COLLECTION,
            temporal=(start, end),
            polygon=coords,
            count=count,
        )
    )


def write_outputs(results: list[Any], aoi, output_dir: Path) -> pd.DataFrame:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [result_to_row(result) for result in results]
    df = pd.DataFrame(rows)
    if not df.empty and "begin_time" in df:
        df = df.sort_values(["begin_time", "granule_id"]).reset_index(drop=True)
        rows = df.to_dict(orient="records")

    df.to_csv(output_dir / "catalog.csv", index=False)
    with open(output_dir / "catalog.json", "w", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)

    aoi_gdf = gpd.GeoDataFrame(
        {"name": ["nisar_search_aoi"]}, geometry=[aoi], crs="EPSG:4326"
    )
    aoi_gdf.to_file(output_dir / "search_aoi.geojson", driver="GeoJSON")
    return df


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GeoJSONを使ってNISAR GCOVを検索")
    parser.add_argument("--geojson", type=Path, default=DEFAULT_GEOJSON)
    parser.add_argument("--id-column", default="simple_id")
    parser.add_argument("--buffer-m", type=float, default=100.0)
    parser.add_argument("--start", default="2026-06-17")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.buffer_m < 0:
        raise ValueError("--buffer-m は0以上にしてください")
    ponds, aoi = load_search_aoi(args.geojson, args.id_column, args.buffer_m)
    logger.info("池ポリゴン: %d件", len(ponds))
    authenticate_if_configured()
    results = search(aoi, args.start, args.end, args.count)
    df = write_outputs(results, aoi, args.output_dir)
    logger.info("検索結果: %d件 → %s", len(df), args.output_dir)


if __name__ == "__main__":
    main()
