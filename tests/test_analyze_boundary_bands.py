import pytest
from shapely.geometry import box

import numpy as np

from scripts.analyze_boundary_bands import distance_bands, nanmedian_filter


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
