"""NISAR GCOVのHH/HVを池周辺だけGeoTIFFへ切り出す。

元のHDF5（数GB）は保存せず、GDAL /vsicurl/ のRange Requestで必要範囲だけ読む。
Earthdata Login認証には ~/.netrc または EARTHDATA_USERNAME / PASSWORD を使う。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = REPO_ROOT / "data" / "nisar_catalog" / "catalog.csv"
DEFAULT_GEOJSON = REPO_ROOT / "data" / "static" / "kitsuki_ponds_final.geojson"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "work" / "nisar_subsets"
DATASET_ROOT = "//science/LSAR/GCOV/grids/frequencyA"
SUPPORTED_LAYERS = {"HHHH", "HVHV", "mask", "numberOfLooks"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def gdal_version() -> tuple[int, int, int]:
    executable = shutil.which("gdalinfo")
    if not executable:
        raise RuntimeError("gdalinfoが見つかりません。GDALをインストールしてください")
    output = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True
    ).stdout
    match = re.search(r"GDAL\s+(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        raise RuntimeError(f"GDALのバージョンを判定できません: {output.strip()}")
    return tuple(int(value) for value in match.groups())


def dataset_source(url: str, layer: str, version: tuple[int, int, int]) -> str:
    if layer not in SUPPORTED_LAYERS:
        raise ValueError(f"未対応レイヤーです: {layer}")
    driver = "HDF5" if version >= (3, 13, 0) else "NETCDF"
    return f'{driver}:"/vsicurl/{url}":{DATASET_ROOT}/{layer}'


def load_catalog(
    catalog_path: Path,
    orbit: str | None,
    date_value: str | None,
    granule_id: str | None,
    limit: int | None,
) -> pd.DataFrame:
    if not catalog_path.exists():
        raise FileNotFoundError(f"観測カタログが見つかりません: {catalog_path}")
    catalog = pd.read_csv(catalog_path, dtype=str).fillna("")
    required = {"granule_id", "begin_time", "orbit_direction", "download_url"}
    missing = required - set(catalog.columns)
    if missing:
        raise ValueError(f"観測カタログに必要なカラムがありません: {sorted(missing)}")

    if orbit:
        catalog = catalog[catalog["orbit_direction"].str.upper() == orbit.upper()]
    if date_value:
        catalog = catalog[catalog["begin_time"].str.startswith(date_value)]
    if granule_id:
        catalog = catalog[catalog["granule_id"] == granule_id]
    catalog = catalog.sort_values(["begin_time", "granule_id"])
    if limit is not None:
        catalog = catalog.head(limit)
    if catalog.empty:
        raise ValueError("指定条件に一致するNISAR観測がありません")
    return catalog


def buffered_cutline_geometry(geojson_path: Path, id_column: str, buffer_m: float):
    if not geojson_path.exists():
        raise FileNotFoundError(f"池GeoJSONが見つかりません: {geojson_path}")
    ponds = gpd.read_file(geojson_path)
    if ponds.empty or ponds.crs is None:
        raise ValueError("池GeoJSONが空、またはCRSがありません")
    if id_column not in ponds.columns:
        raise ValueError(f"池IDカラム '{id_column}' がありません")
    ponds = ponds[ponds.geometry.notna() & ~ponds.geometry.is_empty].copy()
    metric_crs = ponds.estimate_utm_crs()
    if metric_crs is None:
        raise ValueError("バッファー計算用の投影座標系を決定できません")
    buffered = ponds.to_crs(metric_crs).geometry.buffer(buffer_m)
    cutline = buffered.union_all().simplify(2.0, preserve_topology=True)
    cutline_wgs84 = gpd.GeoSeries([cutline], crs=metric_crs).to_crs("EPSG:4326").iloc[0]
    return cutline_wgs84


def build_gdalwarp_command(
    source: str,
    output_path: Path,
    cutline_path: Path,
    cookie_file: Path,
    predictor: str = "3",
) -> list[str]:
    return [
        "gdalwarp",
        "-of", "GTiff",
        source,
        str(output_path),
        "-cutline", str(cutline_path),
        "-crop_to_cutline",
        "-dstalpha",
        "-multi",
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-co", "TILED=YES",
        "-co", "COMPRESS=DEFLATE",
        "-co", f"PREDICTOR={predictor}",
        "-co", "BIGTIFF=IF_SAFER",
        "--config", "CPL_VSIL_CURL_CHUNK_SIZE", "2097152",
        "--config", "CPL_VSIL_CURL_CACHE_SIZE", "67108864",
        "--config", "GDAL_CACHEMAX", "256",
        "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "TRUE",
        "--config", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES",
        "--config", "GDAL_HTTP_MULTIPLEX", "YES",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
        "--config", "GDAL_HTTP_NETRC", "YES",
        "--config", "GDAL_HTTP_COOKIEFILE", str(cookie_file),
        "--config", "GDAL_HTTP_COOKIEJAR", str(cookie_file),
    ]


@contextmanager
def earthdata_environment(dry_run: bool):
    """環境変数の資格情報を一時netrcへ変換し、GDAL用環境を返す。"""
    username = os.getenv("EARTHDATA_USERNAME")
    password = os.getenv("EARTHDATA_PASSWORD")
    existing_netrc = Path.home() / ".netrc"

    with tempfile.TemporaryDirectory(prefix="nisar-auth-") as temp_dir:
        temp_path = Path(temp_dir)
        env = os.environ.copy()
        cookie_file = temp_path / "gdal_cookies.txt"

        if username and password:
            netrc_path = temp_path / ".netrc"
            netrc_path.write_text(
                f"machine urs.earthdata.nasa.gov\n  login {username}\n  password {password}\n",
                encoding="utf-8",
            )
            netrc_path.chmod(0o600)
            env["HOME"] = str(temp_path)
        elif not existing_netrc.exists() and not dry_run:
            raise RuntimeError(
                "Earthdata認証がありません。EARTHDATA_USERNAME/PASSWORDを設定するか、"
                "~/.netrcを作成してください"
            )

        yield env, cookie_file


def output_path_for(row: pd.Series, layer: str, output_dir: Path) -> Path:
    timestamp = pd.to_datetime(row["begin_time"], utc=True)
    orbit = row["orbit_direction"].lower()
    return (
        output_dir
        / timestamp.strftime("%Y/%m/%Y%m%d")
        / orbit
        / str(row["granule_id"])
        / f"frequencyA_{layer}.tif"
    )


def run(args: argparse.Namespace) -> list[dict]:
    if args.buffer_m < 0:
        raise ValueError("--buffer-m は0以上にしてください")
    layers = [item.strip() for item in args.layers.split(",") if item.strip()]
    unknown = set(layers) - SUPPORTED_LAYERS
    if unknown:
        raise ValueError(f"未対応レイヤー: {sorted(unknown)}")

    catalog = load_catalog(args.catalog, args.orbit, args.date, args.granule_id, args.limit)
    cutline_geometry = buffered_cutline_geometry(args.geojson, args.id_column, args.buffer_m)
    version = gdal_version()
    logger.info("GDAL %s.%s.%s / %d観測 / レイヤー %s", *version, len(catalog), layers)

    manifest: list[dict] = []
    with earthdata_environment(args.dry_run) as (env, cookie_file):
        cutline_path = cookie_file.parent / "pond_buffers.geojson"
        gpd.GeoDataFrame(
            {"name": ["pond_buffers"]}, geometry=[cutline_geometry], crs="EPSG:4326"
        ).to_file(cutline_path, driver="GeoJSON")
        for _, row in catalog.iterrows():
            for layer in layers:
                output_path = output_path_for(row, layer, args.output_dir)
                source = dataset_source(row["download_url"], layer, version)
                predictor = "2" if layer == "mask" else "3"
                command = build_gdalwarp_command(
                    source,
                    output_path,
                    cutline_path,
                    cookie_file,
                    predictor=predictor,
                )

                record = {
                    "granule_id": row["granule_id"],
                    "begin_time": row["begin_time"],
                    "orbit_direction": row["orbit_direction"],
                    "layer": layer,
                    "output": str(output_path.relative_to(REPO_ROOT)),
                    "status": "planned" if args.dry_run else "pending",
                }
                if args.dry_run:
                    logger.info("DRY RUN: %s", " ".join(command[:5]) + " ...")
                    manifest.append(record)
                    continue
                if output_path.exists() and not args.overwrite:
                    logger.info("存在するためスキップ: %s", output_path)
                    record["status"] = "skipped"
                    manifest.append(record)
                    continue

                output_path.parent.mkdir(parents=True, exist_ok=True)
                logger.info("切り出し開始: %s %s", row["granule_id"], layer)
                subprocess.run(command, check=True, env=env)
                record["status"] = "created"
                record["size_bytes"] = output_path.stat().st_size
                manifest.append(record)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "buffer_m": args.buffer_m,
                "items": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("マニフェスト: %s", manifest_path)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NISAR GCOVを池周辺だけGeoTIFFへ切り出す")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--geojson", type=Path, default=DEFAULT_GEOJSON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--id-column", default="simple_id")
    parser.add_argument("--buffer-m", type=float, default=100.0)
    parser.add_argument("--layers", default="HHHH,HVHV")
    parser.add_argument("--orbit", choices=["ASCENDING", "DESCENDING"])
    parser.add_argument("--date", help="観測日（YYYY-MM-DD）")
    parser.add_argument("--granule-id")
    parser.add_argument("--limit", type=int, default=1, help="処理する観測数。既定は1")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
