from shapely.geometry import LinearRing, Polygon

from scripts.search_nisar import polygon_coordinates, result_to_row


class FakeResult(dict):
    def data_links(self):
        return ["https://example.test/NISAR_TEST.h5"]


def test_polygon_coordinates_returns_closed_ring():
    geometry = Polygon([(131.5, 33.3), (131.7, 33.3), (131.7, 33.5), (131.5, 33.5)])
    coordinates = polygon_coordinates(geometry)

    assert len(coordinates) >= 4
    assert coordinates[0] == coordinates[-1]
    assert all(len(point) == 2 for point in coordinates)
    assert LinearRing(coordinates).is_ccw


def test_result_to_row_extracts_frequency_a_metadata():
    result = FakeResult(
        meta={"concept-id": "G123"},
        umm={
            "GranuleUR": "NISAR_TEST",
            "TemporalExtent": {
                "RangeDateTime": {
                    "BeginningDateTime": "2026-06-26T10:02:19Z",
                    "EndingDateTime": "2026-06-26T10:02:54Z",
                }
            },
            "AdditionalAttributes": [
                {"Name": "ASCENDING_DESCENDING", "Values": ["DESCENDING"]},
                {"Name": "FREQUENCY_A_POLARIZATION_CONCAT", "Values": ["HH+HV"]},
                {"Name": "FREQUENCY_A_RANGE_BANDWIDTH", "Values": ["40"]},
            ],
        },
    )

    row = result_to_row(result)

    assert row["granule_id"] == "NISAR_TEST"
    assert row["orbit_direction"] == "DESCENDING"
    assert row["frequency_a_polarization"] == "HH+HV"
    assert row["frequency_a_bandwidth_mhz"] == "40"
    assert row["download_url"].endswith(".h5")
