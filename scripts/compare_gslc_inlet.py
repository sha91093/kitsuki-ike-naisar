"""Compare matching 2026 ascending GSLC and GCOV scenes at pond 27's SW inlet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from scipy.ndimage import generic_filter
from shapely.geometry import box, shape


ROOT = Path(__file__).resolve().parents[1]
UTC_DATES = ("2026-06-27", "2026-07-21", "2026-08-14", "2026-08-26")
GCOV_DATES = dict(zip(UTC_DATES, UTC_DATES))
ROI = box(131.6072, 33.39745, 131.60855, 33.39865)


def read_db(path: Path, product: str):
    with rasterio.open(path) as src:
        data = src.read(1)
        valid = np.isfinite(data) & (np.abs(data) > 0)
        power = np.abs(data) ** 2 if product == "GSLC" else data
        db = np.where(valid & (power > 0), 10 * np.log10(np.maximum(power, 1e-12)), np.nan)
        return db, src.transform, src.crs, src.res


def local_median(values):
    return np.nanmedian(values) if np.isfinite(values).any() else np.nan


def zone_geometries(pond, crs):
    projected = gpd.GeoSeries([pond, ROI], crs=4326).to_crs(crs)
    pond_m, roi_m = projected.iloc[0], projected.iloc[1]
    buffer_20, buffer_50 = pond_m.buffer(20), pond_m.buffer(50)
    return {
        "inlet": roi_m.intersection(pond_m),  # Original OSM-restricted analysis.
        "outside_0_20m": roi_m.intersection(buffer_20.difference(pond_m)),
        "outside_20_50m": roi_m.intersection(buffer_50.difference(buffer_20)),
        "outside_beyond_50m": roi_m.difference(buffer_50),
        "roi_all": roi_m,
        "roi_within_20m": roi_m.intersection(buffer_20),
        "roi_within_50m": roi_m.intersection(buffer_50),
        "core": pond_m.buffer(-40),
    }


def masks(shape, transform, crs, pond):
    return {key: geometry_mask([geo], out_shape=shape, transform=transform, invert=True)
            for key, geo in zone_geometries(pond, crs).items()}


def path_for_gcov(directory: Path, date: str, polarization: str):
    matches = list(directory.glob(f"2026/{date[5:7]}/{date.replace('-', '')}/ascending/*/frequencyA_{polarization}{polarization}.tif"))
    if len(matches) != 1:
        raise ValueError(f"Expected one GCOV {date} {polarization}, got {len(matches)}")
    return matches[0]


def outline(ax, geometry, color, width=1.4):
    for polygon in (list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]):
        xx, yy = polygon.exterior.xy
        ax.plot(xx, yy, color=color, linewidth=width)


def plot_maps(data, pond, output):
    fig, axes = plt.subplots(4, len(UTC_DATES), figsize=(17, 15), layout="constrained")
    for row, (product, pol) in enumerate((("GCOV", "HH"), ("GSLC", "HH"),
                                          ("GCOV", "HV"), ("GSLC", "HV"))):
        scenes = [data[(product, day, pol)] for day in UTC_DATES]
        limits = np.nanpercentile(np.concatenate([s["display"].ravel() for s in scenes]), [5, 95])
        for col, (day, scene) in enumerate(zip(UTC_DATES, scenes)):
            ax = axes[row, col]
            arr, transform, crs = scene["display"], scene["transform"], scene["crs"]
            bounds = rasterio.transform.array_bounds(*arr.shape, transform)
            im = ax.imshow(arr, extent=(bounds[0], bounds[2], bounds[1], bounds[3]),
                           cmap="viridis_r", vmin=limits[0], vmax=limits[1], origin="upper")
            projected = gpd.GeoSeries([pond, ROI], crs=4326).to_crs(crs)
            outline(ax, projected.iloc[0], "white")
            outline(ax, projected.iloc[1], "#f44336", 1.8)
            ax.set_xlim(*gpd.GeoSeries([projected.iloc[0].buffer(12)], crs=crs).total_bounds[[0, 2]])
            ax.set_ylim(*gpd.GeoSeries([projected.iloc[0].buffer(12)], crs=crs).total_bounds[[1, 3]])
            ax.set_aspect("equal")
            ax.set_xticks([]); ax.set_yticks([])
            ax.set_title(f"{product} {pol} / {day} UTC ({int(scene['res'][0])} m)")
        fig.colorbar(im, ax=axes[row, :], shrink=.68, label=f"{product} {pol} dB")
    fig.suptitle("Pond 27 SW inlet | red: approximate photo-based ROI | white: OSM polygon")
    fig.savefig(output / "gslc_gcov_maps.png", dpi=190)
    plt.close(fig)


def plot_change(data, pond, output, early_date):
    fig, axes = plt.subplots(2, 2, figsize=(10, 10), layout="constrained")
    for row, product in enumerate(("GCOV", "GSLC")):
        for col, pol in enumerate(("HH", "HV")):
            early, late = (data[(product, day, pol)] for day in (early_date, UTC_DATES[-1]))
            delta = late["display"] - early["display"]
            ax = axes[row, col]
            bounds = rasterio.transform.array_bounds(*delta.shape, early["transform"])
            im = ax.imshow(delta, extent=(bounds[0], bounds[2], bounds[1], bounds[3]),
                           cmap="RdBu_r", vmin=-8, vmax=8, origin="upper")
            projected = gpd.GeoSeries([pond, ROI], crs=4326).to_crs(early["crs"])
            outline(ax, projected.iloc[0], "black")
            outline(ax, projected.iloc[1], "#ffcc00", 1.8)
            region = projected.iloc[0].buffer(12).bounds
            ax.set_xlim(region[0], region[2]); ax.set_ylim(region[1], region[3])
            ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
            ax.set_title(f"{product} {pol}: Aug 26 minus {early_date[5:]} (UTC)")
    fig.colorbar(im, ax=axes, shrink=.7, label="dB; red = brighter in August")
    fig.savefig(output / f"gslc_gcov_change_from_{early_date}.png", dpi=190)
    plt.close(fig)


def plot_zone_map(data, pond, output):
    fig, axes = plt.subplots(1, 3, figsize=(14, 5), layout="constrained")
    for ax, day in zip(axes, (UTC_DATES[0], UTC_DATES[1], UTC_DATES[-1])):
        scene = data[("GSLC", day, "HH")]
        arr, transform, crs = scene["display"], scene["transform"], scene["crs"]
        bounds = rasterio.transform.array_bounds(*arr.shape, transform)
        im = ax.imshow(arr, extent=(bounds[0], bounds[2], bounds[1], bounds[3]),
                       cmap="viridis_r", vmin=-16, vmax=-3, origin="upper")
        zones = zone_geometries(pond, crs)
        outline(ax, zones["roi_all"], "#ff3344", 2)
        outline(ax, zones["inlet"], "white", 2)
        outline(ax, zones["roi_within_20m"], "#00f5d4", 1.5)
        outline(ax, zones["roi_within_50m"], "#ffb703", 1.5)
        region = zones["roi_all"].buffer(15).bounds
        ax.set_xlim(region[0], region[2]); ax.set_ylim(region[1], region[3])
        ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(f"GSLC HH / {day} UTC")
    fig.colorbar(im, ax=axes, shrink=.7, label="GSLC HH dB")
    fig.suptitle("SW inlet | red: approximate ROI, white: OSM, cyan: +20m, orange: +50m")
    fig.savefig(output / "gslc_expanded_zones.png", dpi=190)
    plt.close(fig)


def compare(args):
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    ponds = gpd.read_file(ROOT / "data/static/kitsuki_ponds_final.geojson")
    pond = ponds.loc[ponds.simple_id.astype(str) == "27"].to_crs(4326).geometry.iloc[0]
    data = {}
    records = []
    for product in ("GSLC", "GCOV"):
        for day in UTC_DATES:
            for pol in ("HH", "HV"):
                gslc_dir = args.gslc_june if day == "2026-06-27" else args.gslc
                path = (gslc_dir / day / "ascending" / f"{pol}.tif" if product == "GSLC"
                        else path_for_gcov(args.gcov, GCOV_DATES[day], pol))
                db, transform, crs, resolution = read_db(path, product)
                smoothed = generic_filter(db, local_median, size=3, mode="nearest") if product == "GSLC" else db
                zone = masks(db.shape, transform, crs, pond)
                data[(product, day, pol)] = dict(raw=db, display=smoothed,
                                                  transform=transform, crs=crs, res=resolution,
                                                  masks=zone)
                for name, mask in zone.items():
                    usable = mask & np.isfinite(db)
                    records.append(dict(product=product, utc_date=day, polarization=pol, zone=name,
                                        pixel_spacing_m=resolution[0], valid_pixels=int(usable.sum()),
                                        raw_median_db=float(np.nanmedian(db[usable])),
                                        smoothed_median_db=float(np.nanmedian(smoothed[usable])),
                                        smoothed_p10_db=float(np.nanpercentile(smoothed[usable], 10)),
                                        smoothed_p90_db=float(np.nanpercentile(smoothed[usable], 90))))
    plot_maps(data, pond, output)
    for early_date in (UTC_DATES[0], UTC_DATES[1]):
        plot_change(data, pond, output, early_date)
    plot_zone_map(data, pond, output)
    changes = []
    for early_date in (UTC_DATES[0], UTC_DATES[1]):
        for product in ("GSLC", "GCOV"):
            for pol in ("HH", "HV"):
                early, late = (data[(product, day, pol)] for day in (early_date, UTC_DATES[-1]))
                if early["raw"].shape != late["raw"].shape or early["transform"] != late["transform"]:
                    raise ValueError(f"Grid changed between dates: {product} {pol}")
                for zone, mask in early["masks"].items():
                    valid = mask & np.isfinite(early["display"]) & np.isfinite(late["display"])
                    delta = late["display"][valid] - early["display"][valid]
                    changes.append(dict(product=product, polarization=pol, zone=zone,
                                        from_utc=early_date, to_utc=UTC_DATES[-1],
                                        median_pixel_change_db=float(np.nanmedian(delta)),
                                        brightened_3db_fraction=float(np.mean(delta > 3)),
                                        darkened_3db_fraction=float(np.mean(delta < -3))))
    zones = zone_geometries(pond, data[("GSLC", UTC_DATES[0], "HH")]["crs"])
    zone_areas = {name: round(geometry.area, 1) for name, geometry in zones.items()}
    optical = json.loads((ROOT / "docs/data/sentinel2_27.json").read_text())
    optical_reference = []
    for observation in optical["observations"]:
        if (observation["buffer_m"] != 20 or observation["quality_flag"] != "ok"
                or not observation.get("water_geometry")):
            continue
        if observation["observation_date"] not in ("2026-06-29", "2026-07-14", "2026-08-23"):
            continue
        water = gpd.GeoSeries([shape(observation["water_geometry"])], crs=4326).to_crs(
            data[("GSLC", UTC_DATES[0], "HH")]["crs"]).iloc[0]
        optical_reference.append({"date": observation["observation_date"],
                                  "note": "Existing NDWI/MNDWI mask; not manually verified water",
                                  "zone_water_area_m2": {
                                      name: round(water.intersection(geometry).area, 1)
                                      for name, geometry in zones.items()}})
    result = {"notes": [
        "UTC dates correspond to Jun 28, Jul 22, Aug 15, and Aug 27 in Japan for ascending passes.",
        "The SW inlet ROI is an approximate box inferred from a user map circle, not surveyed photo geometry.",
        "GSLC is beta0 single-look complex power; GCOV is terrain-flattened gamma0. Absolute dB levels are not interchangeable.",
        "GSLC display uses a 3x3 median solely for visualization and change summaries; raw medians are also recorded.",
        "Backscatter changes alone do not prove water to land transitions.",
        "Areas outside OSM are candidate analysis zones, not confirmed pond extent."
    ], "roi_wgs84": ROI.bounds, "zone_geometry_area_m2": zone_areas,
        "optical_reference": optical_reference, "records": records, "changes": changes}
    (output / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(changes, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gslc", type=Path, required=True)
    parser.add_argument("--gslc-june", type=Path, required=True)
    parser.add_argument("--gcov", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    compare(parser.parse_args())
