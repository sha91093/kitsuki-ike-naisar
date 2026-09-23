import numpy as np
import pandas as pd

from scripts.estimate_open_water import (
    adaptive_minimum_pixels,
    boundary_pixels,
    connected_to_seed,
    otsu_threshold,
    pair_observations,
)


def test_otsu_threshold_separates_two_modes():
    values = np.r_[np.full(100, -20.0), np.full(100, -5.0)]
    threshold = otsu_threshold(values)
    assert -20 < threshold < -5


def test_pair_observations_matches_nearest_unused_opposite_orbit():
    frame = pd.DataFrame(
        [
            {"granule_id": "D1", "begin_time": "2026-01-01T10:00:00Z", "orbit_direction": "DESCENDING"},
            {"granule_id": "A1", "begin_time": "2026-01-02T20:00:00Z", "orbit_direction": "ASCENDING"},
            {"granule_id": "A2", "begin_time": "2026-01-10T20:00:00Z", "orbit_direction": "ASCENDING"},
        ]
    )
    pairs = pair_observations(frame, maximum_hours=36)
    assert [(pair[0]["granule_id"], pair[1]["granule_id"]) for pair in pairs] == [("D1", "A1")]


def test_four_neighbor_connection_does_not_leak_diagonally():
    candidate = np.eye(3, dtype=bool)
    seed = np.zeros_like(candidate)
    seed[0, 0] = True
    result = connected_to_seed(candidate, seed, minimum_pixels=1, connectivity=4)
    assert result.sum() == 1


def test_boundary_pixels_returns_inner_edge():
    mask = np.ones((3, 3), dtype=bool)
    edge = boundary_pixels(mask)
    assert edge.sum() == 8
    assert not edge[1, 1]


def test_adaptive_minimum_pixels_is_limited_to_one_through_three():
    assert adaptive_minimum_pixels(1000, 100) == 1
    assert adaptive_minimum_pixels(40000, 100) == 2
    assert adaptive_minimum_pixels(100000, 100) == 3


def test_low_orbit_agreement_ratio_definition():
    confirmed_pixels = 14
    uncertain_pixels = 411
    ratio = confirmed_pixels / (confirmed_pixels + uncertain_pixels)
    assert round(ratio, 4) == 0.0329
