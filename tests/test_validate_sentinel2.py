import numpy as np
import pandas as pd

from scripts.validate_sentinel2 import (
    choose_nearest_observation,
    normalized_difference,
    stac_search_payload,
    valid_scl_mask,
)


def test_normalized_difference_handles_zero_denominator():
    result = normalized_difference(
        np.array([3.0, 0.0, 4.0]), np.array([1.0, 0.0, -4.0])
    )
    assert result[0] == 0.5
    assert np.isnan(result[1])
    assert np.isnan(result[2])


def test_valid_scl_mask_removes_cloud_shadow_and_cloud():
    scl = np.array([2, 3, 6, 8, 9, 10, 11])
    assert valid_scl_mask(scl).tolist() == [True, False, True, False, False, False, False]


def test_choose_nearest_observation_uses_clear_unused_item():
    observations = pd.DataFrame(
        [
            {
                "item_id": "cloudy",
                "observation_date": pd.Timestamp("2026-08-14"),
                "quality_flag": "cloud_or_shadow",
                "seed_valid": True,
            },
            {
                "item_id": "clear",
                "observation_date": pd.Timestamp("2026-08-18"),
                "quality_flag": "ok",
                "seed_valid": True,
            },
        ]
    )
    result = choose_nearest_observation(
        pd.Timestamp("2026-08-14"), observations, maximum_days=10
    )
    assert result["item_id"] == "clear"
    assert choose_nearest_observation(
        pd.Timestamp("2026-08-14"), observations, maximum_days=10, used_ids={"clear"}
    ) is None


def test_stac_search_payload_uses_pond_bounds_and_cloud_limit():
    payload = stac_search_payload((131.6, 33.3, 131.7, 33.4), "2026-06-17", "2026-09-23", 50)
    assert payload["collections"] == ["sentinel-2-l2a"]
    assert payload["bbox"] == [131.6, 33.3, 131.7, 33.4]
    assert payload["datetime"] == "2026-06-17T00:00:00Z/2026-09-23T23:59:59Z"
    assert payload["query"]["eo:cloud_cover"]["lt"] == 50
