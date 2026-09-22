from pathlib import Path

import pandas as pd

from scripts.fetch_nisar import (
    build_gdalwarp_command,
    dataset_source,
    load_catalog,
)


def test_dataset_source_switches_driver_by_gdal_version():
    url = "https://example.test/product.h5"
    assert dataset_source(url, "HHHH", (3, 13, 0)).startswith('HDF5:')
    assert dataset_source(url, "HHHH", (3, 12, 2)).startswith('NETCDF:')


def test_load_catalog_filters_orbit_and_limit(tmp_path):
    path = tmp_path / "catalog.csv"
    pd.DataFrame(
        [
            {"granule_id": "A", "begin_time": "2026-01-01T00:00:00Z", "orbit_direction": "ASCENDING", "download_url": "a"},
            {"granule_id": "B", "begin_time": "2026-01-02T00:00:00Z", "orbit_direction": "DESCENDING", "download_url": "b"},
        ]
    ).to_csv(path, index=False)

    result = load_catalog(path, "DESCENDING", None, None, 1)

    assert list(result["granule_id"]) == ["B"]


def test_gdalwarp_command_contains_remote_and_cutline_options():
    command = build_gdalwarp_command(
        'HDF5:"/vsicurl/https://example.test/a.h5"://science/x',
        Path("out.tif"),
        Path("pond_buffers.geojson"),
        Path("cookies.txt"),
    )

    assert command[0] == "gdalwarp"
    assert "-crop_to_cutline" in command
    assert "-cutline_srs" not in command
    assert "pond_buffers.geojson" in command
    assert "GDAL_HTTP_NETRC" in command
    assert "COMPRESS=DEFLATE" in command
