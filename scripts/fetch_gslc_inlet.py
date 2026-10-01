"""Fetch small NISAR GSLC HH/HV cutouts around one pond for local comparison.

Dates are UTC acquisition dates. The complex samples are preserved in GeoTIFF;
amplitude, calibration and classification are deliberately left for analysis.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import earthaccess
import geopandas as gpd

from fetch_nisar import earthdata_environment, gdal_version
from search_nisar import result_to_row


ROOT = Path(__file__).resolve().parents[1]
COLLECTION = "NISAR_L2_GSLC_PROVISIONAL_V1"
LOG = logging.getLogger(__name__)


def pond_bounds(geojson: Path, pond_id: str, buffer_m: float) -> tuple[float, ...]:
    ponds = gpd.read_file(geojson)
    matches = ponds[ponds["simple_id"].astype(str) == pond_id]
    if len(matches) != 1:
        raise ValueError(f"pond ID {pond_id}: expected one polygon, got {len(matches)}")
    metric = matches.estimate_utm_crs()
    buffered = matches.to_crs(metric).geometry.iloc[0].buffer(buffer_m)
    return tuple(gpd.GeoSeries([buffered], crs=metric).to_crs(4326).total_bounds)


def select_scenes(results: list, dates: set[str], orbit: str) -> list[dict]:
    rows = [result_to_row(result) for result in results]
    rows = [row for row in rows if row["begin_time"][:10] in dates
            and row["orbit_direction"].upper() == orbit
            and row["frequency_a_bandwidth_mhz"] == "40"
            and all(pol in row["frequency_a_polarization"] for pol in ("HH", "HV"))
            and row["download_url"].endswith(".h5")]
    selected = {}
    for row in rows:
        key = row["begin_time"][:10]
        if key not in selected or row["processing_revision"] > selected[key]["processing_revision"]:
            selected[key] = row
    missing = dates - selected.keys()
    if missing:
        raise ValueError(f"No 40 MHz HH/HV GSLC for UTC date(s): {sorted(missing)}")
    return [selected[day] for day in sorted(selected)]


def fetch(args: argparse.Namespace) -> None:
    dates = {datetime.strptime(day.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
             for day in args.dates.split(",") if day.strip()}
    if not dates or args.buffer_m < 0:
        raise ValueError("Provide UTC dates and a nonnegative buffer")
    west, south, east, north = pond_bounds(args.geojson, args.pond_id, args.buffer_m)
    results = earthaccess.search_data(
        short_name=COLLECTION,
        temporal=(min(dates), max(dates) + "T23:59:59Z"),
        bounding_box=(west, south, east, north),
        count=200,
    )
    scenes = select_scenes(list(results), dates, args.orbit)
    version = gdal_version()
    driver = "HDF5" if version >= (3, 13, 0) else "NETCDF"
    manifest = []
    with earthdata_environment(False) as (env, cookies):
        for scene in scenes:
            day = scene["begin_time"][:10]
            folder = args.output_dir / day / args.orbit.lower()
            folder.mkdir(parents=True, exist_ok=True)
            for pol in ("HH", "HV"):
                target = folder / f"{pol}.tif"
                source = (f'{driver}:"/vsicurl/{scene["download_url"]}":'
                          f'//science/LSAR/GSLC/grids/frequencyA/{pol}')
                command = [
                    "gdal_translate", "-of", "GTiff", "-projwin",
                    str(west), str(north), str(east), str(south),
                    "-projwin_srs", "EPSG:4326",
                    "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE",
                    source, str(target),
                    "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "TRUE",
                    "--config", "CPL_VSIL_CURL_CHUNK_SIZE", "2097152",
                    "--config", "CPL_VSIL_CURL_CACHE_SIZE", "67108864",
                    "--config", "GDAL_HTTP_NETRC", "YES",
                    "--config", "GDAL_HTTP_COOKIEFILE", str(cookies),
                    "--config", "GDAL_HTTP_COOKIEJAR", str(cookies),
                ]
                LOG.info("Extracting %s %s", day, pol)
                subprocess.run(command, check=True, env=env)
                manifest.append({"utc_date": day, "orbit": args.orbit,
                                 "polarization": pol, "granule_id": scene["granule_id"],
                                 "file": str(target.relative_to(args.output_dir)),
                                 "bytes": target.stat().st_size})
    (args.output_dir / "manifest.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pond_id": args.pond_id,
        "bounds_wgs84": [west, south, east, north],
        "items": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", required=True, help="Comma separated UTC acquisition dates")
    parser.add_argument("--orbit", choices=("ASCENDING", "DESCENDING"), default="ASCENDING")
    parser.add_argument("--pond-id", default="27")
    parser.add_argument("--buffer-m", type=float, default=50)
    parser.add_argument("--geojson", type=Path, default=ROOT / "data/static/kitsuki_ponds_final.geojson")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "work/gslc_inlet")
    fetch(parser.parse_args())
