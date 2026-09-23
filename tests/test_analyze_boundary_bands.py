import pytest
from shapely.geometry import box

import numpy as np
import pandas as pd

from scripts.analyze_boundary_bands import (
    distance_bands,
    connected_to_seed,
    nanmedian_filter,
    select_composite_groups,
)


def test_distance_bands_are_non_overlapping_and_cover_buffer():
    geometry = box(0, 0, 200, 200)
    bands = distance_bands(geometry)

    assert bands["inside_core"].area == pytest.approx(14400)
    assert bands["inside_20_40"].area == pytest.approx(11200)
    assert bands["inside_0_20"].area == pytest.approx(14400)
    assert bands["outside_0_20"].area == pytest.approx(17254.6, rel=0.01)

    keys = list(bands)
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            assert bands[first].intersection(bands[second]).area == pytest.approx(0)


def test_nanmedian_filter_removes_isolated_speckle():
    values = np.ones((5, 5))
    values[2, 2] = 100

    filtered = nanmedian_filter(values, 3)

    assert filtered[2, 2] == 1


def test_select_composite_groups_uses_rain_thresholds():
    observations = pd.DataFrame(
        {
            "date": ["2026-06-28", "2026-07-10", "2026-07-22", "2026-08-15", "2026-08-27"],
            "rain_7d_mm": [257.0, 109.8, 17.6, 9.2, 2.8],
        }
    )

    wet, dry = select_composite_groups(observations, 100.0, 20.0)

    assert list(wet["date"]) == ["2026-06-28", "2026-07-10"]
    assert list(dry["date"]) == ["2026-07-22", "2026-08-15", "2026-08-27"]


def test_connected_to_seed_removes_isolated_component():
    candidate = np.zeros((6, 6), dtype=bool)
    candidate[1, 1:4] = True
    candidate[4, 4:6] = True
    seed = np.zeros_like(candidate)
    seed[1, 1] = True

    connected, sizes = connected_to_seed(candidate, seed, minimum_pixels=3)

    assert sizes == [3]
    assert connected[1, 1:4].all()
    assert not connected[4, 4:6].any()
